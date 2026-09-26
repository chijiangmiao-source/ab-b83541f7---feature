#!/usr/bin/env python3
"""verify 服务：依次执行代码测试、构建检查、HTTP 冒烟（核对静默后报警反例）、
套件与回放验收（静默闭包后的静止通过、越界报警失败、非确定旧规程拒绝生成、
HTTP 回放结果），完成即退出并报告退出码（0 通过 / 1 失败）。"""

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

# 静默闭包后静止：?cmd 经 tau 闭包后仅可观察 δ（用于套件生成与回放验收）
SILENCE_SUITE_SPEC = {
    "locations": "S0 S1 S2",
    "initial": "S0",
    "inputs": "cmd",
    "transitions": "S0 -> S1 : ?cmd\nS1 -> S2 : tau",
}

# 非确定旧规程：同一命令后可观察 !x 或 !y，须拒绝生成
NONDET_SPEC = {
    "locations": "S0 S1 S2",
    "initial": "S0",
    "inputs": "cmd",
    "transitions": "S0 -> S1 : ?cmd\nS0 -> S2 : ?cmd\nS1 -> S1 : !x\nS2 -> S2 : !y",
}


def step_code_tests():
    """代码测试：运行单元测试套件（含静默后报警反例）。"""
    suite = unittest.defaultTestLoader.discover(os.path.join(ROOT, "tests"))
    result = unittest.TextTestRunner(stream=sys.stdout, verbosity=1).run(suite)
    return result.wasSuccessful()


def step_build_check():
    """构建检查：源码编译、关键文件齐备、模块可导入，并在引擎级核对反例。"""
    ok = True
    for rel in ("ioco.py", "wmethod.py", "server.py", "verify.py",
                os.path.join("tests", "test_ioco.py"), os.path.join("tests", "test_suite.py")):
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
    import wmethod

    old = ioco.parse_spec(COUNTEREXAMPLE["old"], "old")
    cand = ioco.parse_spec(COUNTEREXAMPLE["candidate"], "candidate")
    result = ioco.check_ioco(old, cand).to_json()
    if result["verdict"] != "inconsistent" or result["history"] != EXPECTED_HISTORY:
        print(f"[verify] 构建检查：静默后报警反例引擎核对失败：{result}")
        return False
    suite = wmethod.generate_suite(ioco.parse_spec(SILENCE_SUITE_SPEC, "old"), 2)
    if suite.states != 2 or len(suite.cases) != 1:
        print(f"[verify] 构建检查：静默闭包套件引擎核对失败：{suite.to_json()}")
        return False
    print("[verify] 构建检查：源码编译、模块导入正常，静默后报警反例与套件引擎核对通过")
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


def step_suite_http():
    """套件与回放：静默闭包后的静止通过、越界报警失败、终止后继续输入、
    未知标签、过期测试标识、非确定旧规程拒绝生成（均经 HTTP 接口核对）。"""
    base = os.environ.get("WEB_URL", "http://127.0.0.1:8080").rstrip("/")

    # 生成：静默闭包后静止的规程 -> 单用例套件
    status, body = _request("POST", base + "/api/generate",
                            {"old": SILENCE_SUITE_SPEC, "maxStates": 2})
    data = json.loads(body)
    if not data.get("ok"):
        print(f"[verify] 套件与回放：生成被拒：{data}")
        return False
    result = data["result"]
    suite_id = result.get("suite", "")
    checks = [
        status == 200,
        suite_id.startswith("S-"),
        result.get("states") == 2,
        result.get("bound") == 2,
        result.get("caseCount") == 1,
        result.get("cases") == [{"id": "T001",
                                 "steps": [{"command": "cmd", "allowed": ["δ"]}]}],
    ]
    if not all(checks):
        print("[verify] 套件与回放：生成结果不符："
              + json.dumps(result, ensure_ascii=False))
        return False

    # 编号稳定：同一规程与上界再次生成，测试标识与用例不变
    status, body = _request("POST", base + "/api/generate",
                            {"old": SILENCE_SUITE_SPEC, "maxStates": 2})
    again = json.loads(body)
    if not again.get("ok") or again["result"].get("suite") != suite_id \
            or again["result"].get("cases") != result.get("cases"):
        print("[verify] 套件与回放：重复生成的标识或用例编号不稳定")
        return False

    def replay(record, case="T001", suite=None):
        payload = {"suite": suite if suite is not None else suite_id,
                   "case": case, "record": record}
        status, body = _request("POST", base + "/api/replay", payload)
        return status, json.loads(body)

    # 静默闭包后的静止通过
    status, data = replay("cmd δ")
    if not data.get("ok") or data["result"].get("verdict") != "passed" \
            or data["result"].get("checkedRounds") != 1:
        print(f"[verify] 套件与回放：静默闭包后的静止应判通过：{data}")
        return False

    # 越界报警失败：首个失败轮次为第 1 轮，允许观察为 δ
    status, data = replay("cmd !alarm")
    failure = (data.get("result") or {}).get("firstFailure") or {}
    if not data.get("ok") or data["result"].get("verdict") != "failed" \
            or failure.get("round") != 1 \
            or failure.get("reason") != "out-of-bounds" \
            or failure.get("allowed") != ["δ"]:
        print(f"[verify] 套件与回放：越界报警应判失败：{data}")
        return False

    # 终止用例后继续输入
    status, data = replay("cmd δ\ncmd δ")
    failure = (data.get("result") or {}).get("firstFailure") or {}
    if not data.get("ok") or data["result"].get("verdict") != "failed" \
            or failure.get("round") != 2 \
            or failure.get("reason") != "after-termination":
        print(f"[verify] 套件与回放：终止后继续输入应判失败：{data}")
        return False

    # 未知标签（观察写法非法、命令未声明）：HTTP 422
    for record in ("cmd siren", "nope δ"):
        status, data = replay(record)
        messages = " ".join(e.get("message", "") for e in data.get("errors", []))
        if status != 422 or data.get("ok") is not False or "未知标签" not in messages:
            print(f"[verify] 套件与回放：未知标签未被清楚拒绝：{status} {data}")
            return False

    # 未知用例编号：HTTP 422
    status, data = replay("cmd δ", case="T999")
    if status != 422 or data.get("ok") is not False:
        print(f"[verify] 套件与回放：未知用例编号未被拒绝：{status} {data}")
        return False

    # 过期测试标识：HTTP 409
    status, data = replay("cmd δ", suite="S-000000000000")
    messages = " ".join(e.get("message", "") for e in data.get("errors", []))
    if status != 409 or data.get("ok") is not False or "过期测试标识" not in messages:
        print(f"[verify] 套件与回放：过期测试标识未被清楚拒绝：{status} {data}")
        return False

    # 非确定旧规程拒绝生成：HTTP 422
    status, body = _request("POST", base + "/api/generate",
                            {"old": NONDET_SPEC, "maxStates": 3})
    data = json.loads(body)
    messages = " ".join(e.get("message", "") for e in data.get("errors", []))
    if status != 422 or data.get("ok") is not False or "不唯一" not in messages:
        print(f"[verify] 套件与回放：非确定旧规程未被拒绝生成：{status} {data}")
        return False

    # 上界小于规程稳定状态数：HTTP 422
    status, body = _request("POST", base + "/api/generate",
                            {"old": SILENCE_SUITE_SPEC, "maxStates": 1})
    data = json.loads(body)
    if status != 422 or data.get("ok") is not False:
        print(f"[verify] 套件与回放：过小上界未被拒绝：{status} {data}")
        return False

    # 原有双规程复核接口在套件生成后仍可用
    status, body = _request("POST", base + "/api/check", COUNTEREXAMPLE)
    data = json.loads(body)
    if not data.get("ok") or data["result"].get("verdict") != "inconsistent":
        print(f"[verify] 套件与回放：双规程复核接口不可用：{data}")
        return False

    print("[verify] 套件与回放：生成（静默闭包）、编号稳定、静止通过、越界报警失败、"
          "终止后继续输入、未知标签、过期标识、非确定拒绝、复核接口均通过")
    return True


def main():
    steps = [
        ("代码测试", step_code_tests),
        ("构建检查", step_build_check),
        ("HTTP 冒烟", step_http_smoke),
        ("套件与回放", step_suite_http),
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
