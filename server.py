#!/usr/bin/env python3
"""静止输出复核与离线测试套件服务。

GET  /              复核页（双规程复核 + 套件生成与回放）
GET  /healthz       健康检查
POST /api/check     双规程 ioco 复核
POST /api/generate  由旧规程与最大稳定位置数生成 W-method 测试套件
POST /api/replay    回放逐轮现场记录，报告首个失败轮次

仅使用 Python 标准库；端口经环境变量 PORT 配置（默认 8080）。
"""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ioco import ResourceLimit, SpecError, check_ioco, parse_spec
from wmethod import (
    MAX_BOUND,
    GenerateError,
    RecordError,
    generate_suite,
    parse_record,
    replay_rounds,
)

ROOT = Path(__file__).resolve().parent
PAGE = (ROOT / "index.html").read_bytes()
JSON_CT = "application/json; charset=utf-8"


def _json_bytes(obj):
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


def _error_obj(message):
    return {"ok": False, "errors": [{"side": "-", "field": "-", "line": None, "message": message}]}


class SuiteStore:
    """当前有效的测试套件：重新生成即替换，旧标识随之过期。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._suite = None

    def put(self, suite):
        with self._lock:
            self._suite = suite

    def get(self, suite_id):
        with self._lock:
            if self._suite is not None and self._suite.id == suite_id:
                return self._suite
            return None


SUITES = SuiteStore()


class Handler(BaseHTTPRequestHandler):
    server_version = "ioco-review/1.1"
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
        if path not in ("/api/check", "/api/generate", "/api/replay"):
            self._send_json(404, _error_obj("接口不存在"))
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(400, _error_obj("请求体不是合法 JSON"))
            return
        if path == "/api/check":
            self._handle_check(body)
        elif path == "/api/generate":
            self._handle_generate(body)
        else:
            self._handle_replay(body)

    def _handle_check(self, body):
        if not isinstance(body, dict):
            self._send_json(400, _error_obj("请求体应为 JSON 对象，含 old 与 candidate 两份规程"))
            return
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

    def _handle_generate(self, body):
        if not isinstance(body, dict):
            self._send_json(400, _error_obj("请求体应为 JSON 对象，含 old 规程与 maxStates 上界"))
            return
        try:
            old = parse_spec(body.get("old"), "old")
        except SpecError as exc:
            self._send_json(422, {"ok": False, "errors": exc.errors})
            return
        max_states = _parse_bound(body.get("maxStates"))
        if max_states is None:
            self._send_json(422, {"ok": False, "errors": [
                {"side": "-", "field": "maxStates", "line": None,
                 "message": f"最大稳定位置数须为 1..{MAX_BOUND} 的整数"}]})
            return
        try:
            suite = generate_suite(old, max_states)
        except GenerateError as exc:
            self._send_json(422, {"ok": False, "errors": exc.errors})
            return
        except ResourceLimit as exc:
            self._send_json(503, _error_obj(str(exc)))
            return
        SUITES.put(suite)
        self._send_json(200, {"ok": True, "result": suite.to_json()})

    def _handle_replay(self, body):
        if not isinstance(body, dict):
            self._send_json(400, _error_obj("请求体应为 JSON 对象，含 suite、case 与 record"))
            return
        suite_id = body.get("suite")
        if not isinstance(suite_id, str) or not suite_id.strip():
            self._send_json(422, {"ok": False, "errors": [
                {"side": "-", "field": "suite", "line": None,
                 "message": "缺少测试标识：请先生成套件"}]})
            return
        suite = SUITES.get(suite_id.strip())
        if suite is None:
            self._send_json(409, {"ok": False, "errors": [
                {"side": "-", "field": "suite", "line": None,
                 "message": f"过期测试标识 {suite_id.strip()!r}：与当前套件不符或已失效，"
                            "请重新生成套件后再回放；旧回放结果已清除"}]})
            return
        case_id = body.get("case")
        if not isinstance(case_id, str) or not case_id.strip():
            self._send_json(422, {"ok": False, "errors": [
                {"side": "-", "field": "case", "line": None,
                 "message": "缺少用例编号"}]})
            return
        try:
            rounds = parse_record(body.get("record", ""), suite.inputs)
            result = replay_rounds(suite, case_id.strip(), rounds)
        except RecordError as exc:
            self._send_json(422, {"ok": False, "errors": exc.errors})
            return
        self._send_json(200, {"ok": True, "result": result})

    def log_message(self, fmt, *args):  # 保持日志安静
        pass


def _parse_bound(value):
    """把最大稳定位置数录入解析为合法整数；非法时返回 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if not isinstance(value, int):
        return None
    return value if 1 <= value <= MAX_BOUND else None


def main():
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.daemon_threads = True
    print(f"ioco 复核与测试套件服务监听端口 {port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
