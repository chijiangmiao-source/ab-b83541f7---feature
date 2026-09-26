#!/usr/bin/env python3
"""静止输出复核与离线测试套件服务：

- GET  /            复核页
- GET  /healthz     健康检查
- POST /api/check   双规程 ioco 复核（原接口，继续可用）
- POST /api/suite   由旧规程生成 W-method 离线一致性测试套件
- POST /api/replay  回放逐轮现场记录，报告首个失败轮次

仅使用 Python 标准库；端口经环境变量 PORT 配置（默认 8080）。
"""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ioco import ResourceLimit, SpecError, _err, check_ioco, parse_spec
from wmethod import MAX_REPLAY_ROUNDS, GenError, generate_suite, replay

ROOT = Path(__file__).resolve().parent
PAGE = (ROOT / "index.html").read_bytes()
JSON_CT = "application/json; charset=utf-8"

# 当前有效套件（单槽位）：生成新套件后旧标识即过期
_SUITE_LOCK = threading.Lock()
_CURRENT_SUITE = {"id": None, "suite": None}


def _json_bytes(obj):
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


def _error_obj(message):
    return {"ok": False, "errors": [{"side": "-", "field": "-", "line": None, "message": message}]}


class Handler(BaseHTTPRequestHandler):
    server_version = "ioco-review/2.0"
    protocol_version = "HTTP/1.1"

    def _send(self, code, body, content_type):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, code, obj):
        self._send(code, _json_bytes(obj), JSON_CT)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send(200, PAGE, "text/html; charset=utf-8")
        elif path == "/healthz":
            self._send_json(200, {"status": "ok"})
        else:
            self._send_json(404, _error_obj("接口不存在"))

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path not in ("/api/check", "/api/suite", "/api/replay"):
            self._send_json(404, _error_obj("接口不存在"))
            return
        body = self._read_body()
        if body is None:
            return  # 错误响应已发送
        if path == "/api/check":
            self._handle_check(body)
        elif path == "/api/suite":
            self._handle_suite(body)
        else:
            self._handle_replay(body)

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(400, _error_obj("请求体不是合法 JSON"))
            return None
        if not isinstance(body, dict):
            self._send_json(400, _error_obj("请求体应为 JSON 对象"))
            return None
        return body

    def _handle_check(self, body):
        errors = []
        specs = {}
        for key, side in (("old", "old"), ("candidate", "candidate")):
            try:
                specs[side] = parse_spec(body.get(key), side)
            except SpecError as exc:
                errors.extend(exc.errors)
        if errors:
            self._send_json(422, {"ok": False, "errors": errors})
            return
        try:
            result = check_ioco(specs["old"], specs["candidate"])
        except ResourceLimit as exc:
            self._send_json(503, _error_obj(str(exc)))
            return
        self._send_json(200, {"ok": True, "result": result.to_json()})

    def _handle_suite(self, body):
        try:
            spec = parse_spec(body.get("old"), "old")
        except SpecError as exc:
            self._send_json(422, {"ok": False, "errors": exc.errors})
            return
        try:
            suite = generate_suite(spec, body.get("maxStates"))
        except GenError as exc:
            self._send_json(422, {"ok": False, "errors": exc.errors})
            return
        except ResourceLimit as exc:
            self._send_json(503, _error_obj(str(exc)))
            return
        with _SUITE_LOCK:
            _CURRENT_SUITE["id"] = suite["suiteId"]
            _CURRENT_SUITE["suite"] = suite
        self._send_json(200, {"ok": True, "suite": suite})

    def _handle_replay(self, body):
        with _SUITE_LOCK:
            current_id = _CURRENT_SUITE["id"]
            current = _CURRENT_SUITE["suite"]
        suite_id = body.get("suiteId")
        if current is None or not isinstance(suite_id, str) or suite_id != current_id:
            self._send_json(409, {"ok": False, "errors": [_err(
                "-", "suiteId",
                "过期测试标识：测试套件尚未生成、已更新或服务已重启，"
                "请重新生成套件后再回放")]})
            return
        case_id = body.get("caseId")
        if not isinstance(case_id, str):
            self._send_json(422, {"ok": False, "errors": [_err(
                "-", "caseId", "缺少测试用例编号 caseId（文本）")]})
            return
        rounds = body.get("rounds")
        if not isinstance(rounds, list) or len(rounds) > MAX_REPLAY_ROUNDS:
            self._send_json(422, {"ok": False, "errors": [_err(
                "-", "rounds", f"现场记录应为轮次数组（不超过 {MAX_REPLAY_ROUNDS} 轮）")]})
            return
        result, errors = replay(current, case_id, rounds)
        if errors is not None:
            self._send_json(422, {"ok": False, "errors": errors})
            return
        self._send_json(200, {"ok": True, "replay": result})

    def log_message(self, fmt, *args):  # 保持日志安静
        pass


def main():
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.daemon_threads = True
    print(f"ioco 复核服务监听端口 {port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
