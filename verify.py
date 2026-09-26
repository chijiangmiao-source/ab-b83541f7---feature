#!/usr/bin/env python3
"""verify 服务：依次执行代码测试、构建检查、HTTP 冒烟，完成即退出并报告退出码
（0 通过 / 1 失败）。覆盖：静默后报警反例、一致对照、非法录入反馈、
静默闭包后的静止通过、越界报警失败、非确定旧规程拒绝生成及 HTTP 回放结果。"""

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

# 套件生成与回放：cmd 分支经静默闭包后静止；!alarm 仅在 reset 分支允许
SUITE_SPEC = {
    "old": {
        "locations": "S0 S1 S2 S3 S4",
        "initial": "S0",
        "inputs": "cmd reset",
        "transitions": "S0 -> S1 : ?cmd\nS1 -> S2 : tau\nS0 -> S3 : ?reset\nS3 -> S4 : !alarm",
    },
    "maxStates": 2,
}
EXPECTED_CASES = {"TC-1": [["cmd", ["δ"]]], "TC-2": [["reset", ["!alarm"]]]}

# 非确定旧规程：同一命令后存在多个可观察结果，须拒绝生成
NONDET_SUITE_SPEC = {
    "old": {
        "locations": "S0 S1 S2 S3",
        "initial": "S0",
        "inputs": "cmd",
        "transitions": "S0 -> S1 : ?cmd\nS1 -> S2 : !x\nS1 -> S3 : !y",
    },
    "maxStates": 4,
}


def step_code_tests():
    """代码测试：运行单元测试套件（含静默后报警反例与 W-method 套件）。"""
    suite = unittest.defaultTestLoader.discover(os.path.join(ROOT, "tests"))
    result = unittest.TextTestRunner(stream=sys.stdout, verbosity=1).run(suite)
    return result.wasSuccessful()


def step_build_check():
    """构建检查：源码编译、关键文件齐备、模块可导入，并在引擎级核对反例与套件。"""
    ok = True
    for rel in ("ioco.py", "wmethod.py", "server.py", "verify.py",
                os.path.join("tests", "test_ioco.py"),
                os.path.join("tests", "test_wmethod.py")):
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

    # 引擎级：套件生成（状态覆盖、迁移覆盖、区分后缀）
    suite = wmethod.generate_suite(ioco.parse_spec(SUITE_SPEC["old"], "old"),
                                   SUITE_SPEC["maxStates"])
    rounds = {c["id"]: [[r["cmd"], r["allowed"]] for r in c["rounds"]]
              for c in suite["cases"]}
    if rounds != EXPECTED_CASES or suite["stateCount"] != 2:
        print(f"[verify] 构建检查：套件生成不符：{rounds}")
        return False
    # 引擎级：静默闭包后的静止通过
    res, err = wmethod.replay(suite, "TC-1", [{"cmd": "cmd", "obs": "δ"}])
    if err or res["verdict"] != "pass":
        print(f"[verify] 构建检查：静默闭包后的静止应判通过：{res} {err}")
        return False
    # 引擎级：越界报警失败（首个失败轮次为第 1 轮）
    res, err = wmethod.replay(suite, "TC-1", [{"cmd": "cmd", "obs": "!alarm"}])
    if err or res["verdict"] != "fail" or res["firstFailure"]["round"] != 1:
        print(f"[verify] 构建检查：越界报警应判失败：{res} {err}")
        return False
    # 引擎级：非确定旧规程拒绝生成
    try:
        wmethod.generate_suite(ioco.parse_spec(NONDET_SUITE_SPEC["old"], "old"),
                               NONDET_SUITE_SPEC["maxStates"])
        print("[verify] 构建检查：非确定旧规程未被拒绝")
        return False
    except wmethod.GenError as exc:
        if not any("多个可观察结果" in e["message"] for e in exc.errors):
            print(f"[verify] 构建检查：拒绝理由不符：{exc.errors}")
            return False
    print("[verify] 构建检查：源码编译、模块导入正常，反例与套件引擎核对通过")
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
    """HTTP 冒烟：健康检查、复核页、反例与对照、非法录入、套件生成与回放。"""
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

    # ---- 套件生成与 HTTP 回放 ----
    status, body = _request("POST", base + "/api/suite", SUITE_SPEC)
    data = json.loads(body)
    if status != 200 or not data.get("ok"):
        print(f"[verify] HTTP 冒烟：套件生成被拒：{data}")
        return False
    suite = data["suite"]
    rounds = {c["id"]: [[r["cmd"], r["allowed"]] for r in c["rounds"]]
              for c in suite["cases"]}
    if rounds != EXPECTED_CASES or not suite.get("suiteId"):
        print("[verify] HTTP 冒烟：套件内容不符："
              + json.dumps(rounds, ensure_ascii=False))
        return False
    sid = suite["suiteId"]

    # HTTP 回放：静默闭包后的静止通过
    status, body = _request("POST", base + "/api/replay",
                            {"suiteId": sid, "caseId": "TC-1",
                             "rounds": [{"cmd": "?cmd", "obs": "δ"}]})
    data = json.loads(body)
    if status != 200 or not data.get("ok") \
            or data["replay"].get("verdict") != "pass":
        print(f"[verify] HTTP 冒烟：静默闭包后的静止应判通过：{data}")
        return False

    # HTTP 回放：越界报警失败，首个失败轮次为第 1 轮
    status, body = _request("POST", base + "/api/replay",
                            {"suiteId": sid, "caseId": "TC-1",
                             "rounds": [{"cmd": "cmd", "obs": "!alarm"}]})
    data = json.loads(body)
    failure = (data.get("replay") or {}).get("firstFailure") or {}
    if status != 200 or not data.get("ok") \
            or data["replay"].get("verdict") != "fail" \
            or failure.get("round") != 1 or failure.get("expected") != ["δ"]:
        print(f"[verify] HTTP 冒烟：越界报警应判失败：{data}")
        return False

    # 非确定旧规程拒绝生成
    status, body = _request("POST", base + "/api/suite", NONDET_SUITE_SPEC)
    data = json.loads(body)
    messages = " ".join(e.get("message", "") for e in data.get("errors", []))
    if status != 422 or data.get("ok") is not False \
            or "多个可观察结果" not in messages:
        print(f"[verify] HTTP 冒烟：非确定旧规程未被拒绝：{status} {data}")
        return False

    # 终止用例后继续输入：清楚拒绝
    status, body = _request("POST", base + "/api/replay",
                            {"suiteId": sid, "caseId": "TC-1",
                             "rounds": [{"cmd": "cmd", "obs": "δ"},
                                        {"cmd": "cmd", "obs": "δ"}]})
    data = json.loads(body)
    messages = " ".join(e.get("message", "") for e in data.get("errors", []))
    if status != 422 or data.get("ok") is not False \
            or "终止用例后继续输入" not in messages:
        print(f"[verify] HTTP 冒烟：终止用例后继续输入未被拒绝：{status} {data}")
        return False

    # 未知标签：清楚拒绝
    status, body = _request("POST", base + "/api/replay",
                            {"suiteId": sid, "caseId": "TC-1",
                             "rounds": [{"cmd": "cmd", "obs": "!frobnicate"}]})
    data = json.loads(body)
    messages = " ".join(e.get("message", "") for e in data.get("errors", []))
    if status != 422 or data.get("ok") is not False or "未知标签" not in messages:
        print(f"[verify] HTTP 冒烟：未知标签未被拒绝：{status} {data}")
        return False

    # 过期测试标识：清楚拒绝
    status, body = _request("POST", base + "/api/replay",
                            {"suiteId": "suite-stale", "caseId": "TC-1",
                             "rounds": [{"cmd": "cmd", "obs": "δ"}]})
    data = json.loads(body)
    messages = " ".join(e.get("message", "") for e in data.get("errors", []))
    if status != 409 or data.get("ok") is not False \
            or "过期测试标识" not in messages:
        print(f"[verify] HTTP 冒烟：过期测试标识未被拒绝：{status} {data}")
        return False

    print("[verify] HTTP 冒烟：健康检查、复核页、反例与对照、非法录入、"
          "套件生成、静止通过、越界报警、非确定拒绝、回放拒绝项均通过")
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
