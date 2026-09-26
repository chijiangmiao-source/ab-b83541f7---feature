#!/usr/bin/env python3
"""静止输出复核服务：GET / 复核页，GET /healthz 健康检查，POST /api/check 复核接口。

仅使用 Python 标准库；端口经环境变量 PORT 配置（默认 8080）。
"""

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ioco import ResourceLimit, SpecError, check_ioco, parse_spec

ROOT = Path(__file__).resolve().parent
PAGE = (ROOT / "index.html").read_bytes()
JSON_CT = "application/json; charset=utf-8"


def _json_bytes(obj):
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


def _error_obj(message):
    return {"ok": False, "errors": [{"side": "-", "field": "-", "line": None, "message": message}]}


class Handler(BaseHTTPRequestHandler):
    server_version = "ioco-review/1.0"
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
        if path != "/api/check":
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
