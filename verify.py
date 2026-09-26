#!/usr/bin/env python3
"""verify 服务：依次执行代码测试、构建检查、HTTP 冒烟（核对静默后报警反例），
完成即退出并报告退出码（0 通过 / 1 失败）。"""

import json
import os
import py_compile
import sys
import time
import traceback
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# 静默后报警反例：旧规程命令后只能静止，候选经静默迁移后报警
COUNTEREXAMPLE = {
    "old": {
        "locations": "S0 S1",
        "initial": "S0",
        "inputs": "cmd",
        "transitions": "S0 -> S1 : ?cmd",
    },
    "candidate": {
        "locations": "A B C",
        "initial": "A",
        "inputs": "cmd",
        "transitions": "A -> B : ?cmd\nB -> C : tau\nC -> C : !alarm",
    },
}
EXPECTED_HISTORY = ["?cmd", "!alarm"]


def step_code_tests():
    """代码测试：运行单元测试套件（含静默后报警反例）。"""
    suite = unittest.defaultTestLoader.discover(os.path.join(ROOT, "tests"))
    result = unittest.TextTestRunner(stream=sys.stdout, verbosity=1).run(suite)
    return result.wasSuccessful()


def step_build_check():
    """构建检查：源码编译、关键文件齐备、模块可导入，并在引擎级核对反例。"""
    ok = True
    for rel in ("ioco.py", "server.py", "verify.py", os.path.join("tests", "test_ioco.py")):
        path = os.path.join(ROOT, rel)
        if not os.path.isfile(path):
            print(f"[verify] 构建检查：缺少文件 {rel}")
            ok = False
            continue
        py_compile.compile(path, doraise=True)
    for rel in ("index.html", "Dockerfile", "compose.yaml"):
        if not os.path.isfile(os.path.join(ROOT, rel)):
            print(f"[verify] 构建检查：缺少文件 {rel}")
            ok = False
    if not ok:
        return False
    import ioco
    import server  # noqa: F401  导入即检查可装配性

    old = ioco.parse_spec(COUNTEREXAMPLE["old"], "old")
    cand = ioco.parse_spec(COUNTEREXAMPLE["candidate"], "candidate")
    result = ioco.check_ioco(old, cand).to_json()
    if result["verdict"] != "inconsistent" or result["history"] != EXPECTED_HISTORY:
        print(f"[verify] 构建检查：静默后报警反例引擎核对失败：{result}")
        return False
    print("[verify] 构建检查：源码编译、模块导入正常，静默后报警反例引擎核对通过")
    return True


def _request(method, url, payload=None):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def step_http_smoke():
    """HTTP 冒烟：健康检查、复核页、静默后报警反例、一致对照、非法录入反馈。"""
    base = os.environ.get("WEB_URL", "http://127.0.0.1:8080").rstrip("/")
    deadline = time.time() + 90
    while True:
        try:
            status, _ = _request("GET", base + "/healthz")
            if status == 200:
                break
        except urllib.error.URLError:
            pass
        if time.time() > deadline:
            print(f"[verify] HTTP 冒烟：{base} 健康检查超时")
            return False
        time.sleep(1)

    status, html = _request("GET", base + "/")
    if status != 200 or "旧规程" not in html or "候选规程" not in html:
        print("[verify] HTTP 冒烟：复核页未正确返回")
        return False

    status, body = _request("POST", base + "/api/check", COUNTEREXAMPLE)
    data = json.loads(body)
    if not data.get("ok"):
        print(f"[verify] HTTP 冒烟：反例提交被拒：{data}")
        return False
    result = data["result"]
    steps = result.get("steps", [])
    checks = [
        result.get("verdict") == "inconsistent",
        result.get("history") == EXPECTED_HISTORY,
        result.get("offendingOutput") == "!alarm",
        result.get("oldOutputs") == ["δ"],
        "!alarm" in result.get("candidateOutputs", []),
        len(steps) == 2,
        steps[1].get("candidate") == ["B", "C"],
        steps[1].get("old") == ["S1"],
    ]
    if not all(checks):
        print("[verify] HTTP 冒烟：静默后报警反例结论不符："
              + json.dumps(result, ensure_ascii=False))
        return False

    renamed = {
        "old": {"locations": "Idle Busy Done", "initial": "Idle", "inputs": "open close",
                "transitions": "Idle -> Busy : ?open\nBusy -> Done : !ack\nDone -> Idle : ?close"},
        "candidate": {"locations": "L0 L1 L2", "initial": "L0", "inputs": "open close",
                      "transitions": "L0 -> L1 : ?open\nL1 -> L2 : !ack\nL2 -> L0 : ?close"},
    }
    status, body = _request("POST", base + "/api/check", renamed)
    data = json.loads(body)
    if not data.get("ok") or data["result"].get("verdict") != "consistent":
        print(f"[verify] HTTP 冒烟：仅位置改名对照应判一致：{data}")
        return False

    invalid = {
        "old": {"locations": "S0 S0", "initial": "", "inputs": "cmd",
                "transitions": "S0 -> S9 : ?cmd\nS0 -> S1 : @@"},
        "candidate": COUNTEREXAMPLE["candidate"],
    }
    status, body = _request("POST", base + "/api/check", invalid)
    data = json.loads(body)
    if data.get("ok") is not False or not data.get("errors"):
        print(f"[verify] HTTP 冒烟：非法录入未被拒绝：{data}")
        return False
    messages = " ".join(e.get("message", "") for e in data["errors"])
    for keyword in ("重复", "初始位置", "悬空", "非法标签"):
        if keyword not in messages:
            print(f"[verify] HTTP 冒烟：错误反馈缺少“{keyword}”定位：{data['errors']}")
            return False
    print("[verify] HTTP 冒烟：健康检查、复核页、静默后报警反例、一致对照、非法录入反馈均通过")
    return True


def main():
    steps = [
        ("代码测试", step_code_tests),
        ("构建检查", step_build_check),
        ("HTTP 冒烟", step_http_smoke),
    ]
    ok = True
    for name, fn in steps:
        print(f"[verify] ==== {name} ====", flush=True)
        try:
            passed = bool(fn())
        except Exception:
            traceback.print_exc()
            passed = False
        print(f"[verify] {name}：{'通过' if passed else '失败'}", flush=True)
        ok = ok and passed
    code = 0 if ok else 1
    print(f"[verify] 完成，退出码: {code}", flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
