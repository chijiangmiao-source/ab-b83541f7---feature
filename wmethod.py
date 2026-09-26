"""离线一致性测试套件：旧规程确定化、W-method 套件生成与现场记录回放。

生成流程：
1. 确定化：对旧规程（含内部静默 tau 与可观察静止 δ）做子集构造。机器状态为
   “稳定闭包”——闭包在命令轮次之外不产生任何输出；仅在闭包内每个位置都允许
   某命令时才发出该命令；每条命令之后必须恰有一个可观察结果（某个输出或静止 δ），
   若同一命令后存在多个可观察结果，则拒绝生成（不能任选一条）。
2. 最小化：按“使能命令集、各命令观察、后继等价”合并等价状态，得 n 个稳定位置。
3. W-method 套件：（状态覆盖 ∪ 迁移覆盖）· 长度 ≤（上界 m − n）的使能命令序列
   · 区分后缀 W；互为前缀的用例只保留最长者。任何不超过 m 个稳定位置的确定实现，
   通过全部用例当且仅当它在旧规程定义的命令序列范围内与旧规程一致。
   不做固定深度枚举、不使用随机命令、不以位置名称对应代替行为。

回放：逐轮核对“发送命令、收到输出或静止”的现场记录，报告首个失败轮次；
未知标签、终止用例后继续输入等直接拒绝。
"""

from __future__ import annotations

import hashlib
import json
from collections import deque

from ioco import (DELTA, KIND_DELTA, KIND_INPUT, KIND_OUTPUT, ResourceLimit,
                  _err, _out_set, _step, _tau_closure)

MAX_BOUND = 100            # 最大稳定位置数入参上界（资源保护）
MAX_DET_STATES = 2000      # 确定化闭包状态数上限（资源保护）
MAX_MIDDLE = 20000         # 中间序列枚举条数上限（资源保护）
MAX_CASES = 1000           # 套件用例数上限（资源保护）
MAX_REPLAY_ROUNDS = 10000  # 单次回放轮数上限（资源保护）


class GenError(Exception):
    """拒绝生成：旧规程不满足可生成条件；errors 为可定位错误字典列表。"""

    def __init__(self, errors):
        super().__init__("拒绝生成测试套件")
        self.errors = list(errors)


# ---------------------------------------------------------------- 确定化

def _enabled_inputs(spec, closure):
    """闭包可发出的命令：闭包内每个位置都允许该命令（仅在每个闭包允许时发出命令）。"""
    common = None
    for loc in closure:
        here = {t.label for t in spec.out_map.get(loc, ()) if t.kind == KIND_INPUT}
        common = here if common is None else common & here
        if not common:
            break
    return sorted(common or ())


def _determinize(spec):
    """子集构造稳定闭包机；返回 (closures, enabled, obs, nxt)。拒绝时抛 GenError。"""
    closures = []
    ids = {}
    enabled = {}
    obs = {}
    nxt = {}

    def add(closure):
        if closure in ids:
            return ids[closure]
        if len(closures) >= MAX_DET_STATES:
            raise ResourceLimit(f"确定化状态数超过上限 {MAX_DET_STATES}，资源内无法生成")
        bad = sorted(o for o in _out_set(spec, closure) if o != DELTA)
        if bad:
            raise GenError([_err(
                "old", "transitions",
                f"闭包 {{{', '.join(sorted(closure))}}} 在命令轮次之外可能产生输出 {bad[0]}"
                "（状态不稳定）：无法按“发送命令、收到输出或静止”轮次生成，拒绝生成")])
        ids[closure] = len(closures)
        closures.append(closure)
        return ids[closure]

    add(_tau_closure(spec, {spec.initial}))
    s = 0
    while s < len(closures):
        closure = closures[s]
        en = _enabled_inputs(spec, closure)
        enabled[s] = en
        for cmd in en:
            after = _step(spec, closure, KIND_INPUT, cmd)
            outs = _out_set(spec, after)
            if not outs:
                raise GenError([_err(
                    "old", "transitions",
                    f"闭包 {{{', '.join(sorted(closure))}}} 发出 ?{cmd} 后不存在可观察结果"
                    "（内部静默循环），无法回放观察，拒绝生成")])
            if len(outs) > 1:
                raise GenError([_err(
                    "old", "transitions",
                    f"旧规程非确定：闭包 {{{', '.join(sorted(closure))}}} 发出 ?{cmd} 后"
                    f"存在多个可观察结果（{'、'.join(sorted(outs))}），拒绝生成（不能任选一条）")])
            observation = next(iter(outs))
            if observation == DELTA:
                target = _step(spec, after, KIND_DELTA, DELTA)
            else:
                target = _step(spec, after, KIND_OUTPUT, observation[1:])
            obs[(s, cmd)] = observation
            nxt[(s, cmd)] = add(target)
        s += 1
    return closures, enabled, obs, nxt


# ---------------------------------------------------------------- 最小化

def _signature(enabled, obs, s):
    """局部签名：使能命令集及每个命令的唯一观察。"""
    return tuple((cmd, obs[(s, cmd)]) for cmd in enabled[s])


def _minimize(closures, enabled, obs, nxt):
    """按行为等价合并状态（划分细化）；返回商机，状态自初始闭包 BFS 重编号。"""
    n = len(closures)
    by_sig = {}
    for s in range(n):
        by_sig.setdefault(_signature(enabled, obs, s), []).append(s)
    part = list(by_sig.values())
    while True:
        block_of = {}
        for bi, block in enumerate(part):
            for s in block:
                block_of[s] = bi
        new_part = []
        for block in part:
            groups = {}
            for s in block:
                key = tuple(block_of[nxt[(s, cmd)]] for cmd in enabled[s])
                groups.setdefault(key, []).append(s)
            new_part.extend(groups.values())
        if len(new_part) == len(part):
            break
        part = new_part
    block_of = {}
    for bi, block in enumerate(part):
        for s in block:
            block_of[s] = bi
    rep = {bi: block[0] for bi, block in enumerate(part)}
    # 自初始块 BFS 重编号，保证编号稳定
    order = []
    seen = {block_of[0]}
    queue = deque([block_of[0]])
    while queue:
        b = queue.popleft()
        order.append(b)
        r = rep[b]
        for cmd in enabled[r]:
            nb = block_of[nxt[(r, cmd)]]
            if nb not in seen:
                seen.add(nb)
                queue.append(nb)
    new_id = {b: k for k, b in enumerate(order)}
    q_closures = [closures[rep[b]] for b in order]
    q_enabled = [tuple(enabled[rep[b]]) for b in order]
    q_obs = {}
    q_nxt = {}
    for b in order:
        r = rep[b]
        k = new_id[b]
        for cmd in enabled[r]:
            q_obs[(k, cmd)] = obs[(r, cmd)]
            q_nxt[(k, cmd)] = new_id[block_of[nxt[(r, cmd)]]]
    return q_closures, q_enabled, q_obs, q_nxt


# ---------------------------------------------------------------- 区分后缀

def _sig_diff(sig_a, sig_b):
    """两个局部签名首个不同的命令（使能性不同或观察不同）。"""
    da, db = dict(sig_a), dict(sig_b)
    for cmd in sorted(set(da) | set(db)):
        if da.get(cmd) != db.get(cmd):
            return cmd
    raise ValueError("签名一致，不存在差异命令")


def _characterization(enabled, obs, nxt, nstates):
    """区分后缀集 W：任意两个状态都可被 W 中某条（在该状态使能的）后缀区分。"""
    if nstates <= 1:
        return []
    sigs = [_signature(enabled, obs, s) for s in range(nstates)]
    rep_of = [None] * nstates   # 状态 -> 当前块代表（块内最小编号）
    bsplit = {}                 # (块代表A, 块代表B) -> 区分命令序列

    def key(a, b):
        return (a, b) if a < b else (b, a)

    by_sig = {}
    for s in range(nstates):
        by_sig.setdefault(sigs[s], []).append(s)
    for members in by_sig.values():
        r = min(members)
        for s in members:
            rep_of[s] = r
    reps = sorted(set(rep_of))
    for i, ra in enumerate(reps):
        for rb in reps[i + 1:]:
            bsplit[key(ra, rb)] = (_sig_diff(sigs[ra], sigs[rb]),)

    while True:
        split = None
        for r in sorted(set(rep_of)):
            members = [s for s in range(nstates) if rep_of[s] == r]
            if len(members) < 2:
                continue
            for cmd in enabled[members[0]]:  # 同块使能命令集一致
                if len({rep_of[nxt[(s, cmd)]] for s in members}) > 1:
                    split = (members, cmd)
                    break
            if split:
                break
        if split is None:
            break
        members, cmd = split
        block_rep = min(members)
        old_reps = sorted(set(rep_of))
        groups = {}
        for s in members:
            groups.setdefault(rep_of[nxt[(s, cmd)]], []).append(s)
        new_reps = []
        for succ_rep, members_g in sorted(groups.items()):
            nr = min(members_g)
            for s in members_g:
                rep_of[s] = nr
            new_reps.append((nr, succ_rep))
        # 新块两两之间：经 cmd 到后继块，再拼接后继块间的区分序列
        for i, (ra, sa) in enumerate(new_reps):
            for rb, sb in new_reps[i + 1:]:
                bsplit[key(ra, rb)] = (cmd,) + bsplit[key(sa, sb)]
        # 新块与其他旧块之间：继承原块的区分序列
        for nr, _ in new_reps:
            if nr == block_rep:
                continue
            for other in old_reps:
                if other != block_rep:
                    bsplit[key(nr, other)] = bsplit[key(block_rep, other)]

    final_reps = sorted(set(rep_of))
    suffixes = set()
    for i, ra in enumerate(final_reps):
        for rb in final_reps[i + 1:]:
            suffixes.add(bsplit[key(ra, rb)])
    return sorted(suffixes)


# ---------------------------------------------------------------- 套件构造

def _state_after(nxt, seq, start=0):
    s = start
    for cmd in seq:
        s = nxt[(s, cmd)]
    return s


def _enabled_at(enabled, nxt, s, seq):
    for cmd in seq:
        if cmd not in enabled[s]:
            return False
        s = nxt[(s, cmd)]
    return True


def _access_sequences(enabled, nxt, nstates):
    """状态覆盖：到每个状态的最短命令序列（BFS，按命令 ASCII 顺序）。"""
    access = [None] * nstates
    access[0] = ()
    queue = deque([0])
    while queue:
        s = queue.popleft()
        for cmd in enabled[s]:
            t = nxt[(s, cmd)]
            if access[t] is None:
                access[t] = access[s] + (cmd,)
                queue.append(t)
    return access


def _enabled_sequences(enabled, nxt, start, depth):
    """从 start 出发长度 ≤ depth 的使能命令序列（含空序列）。"""
    result = [()]
    frontier = [()]
    for _ in range(depth):
        nxt_level = []
        for seq in frontier:
            s = _state_after(nxt, seq, start)
            for cmd in enabled[s]:
                nxt_level.append(seq + (cmd,))
                if len(result) + len(nxt_level) > MAX_MIDDLE:
                    raise ResourceLimit(
                        f"中间序列枚举超过上限 {MAX_MIDDLE}，资源内无法生成")
        result.extend(nxt_level)
        if not nxt_level:
            break
        frontier = nxt_level
    return result


def _build_cases(enabled, nxt, nstates, max_states, access, suffixes):
    """W-method 套件：（状态覆盖 ∪ 迁移覆盖）· I^{≤m−n} · W（仅沿使能命令）。"""
    depth = max_states - nstates
    cover = set(access)
    for s in range(nstates):
        for cmd in enabled[s]:
            cover.add(access[s] + (cmd,))
    cases = set()

    def add_case(seq):
        if not seq:
            return
        if len(cases) >= MAX_CASES:
            raise ResourceLimit(f"测试用例数超过上限 {MAX_CASES}，资源内无法生成")
        cases.add(seq)

    for prefix in sorted(cover):
        s = _state_after(nxt, prefix)
        for mid in _enabled_sequences(enabled, nxt, s, depth):
            t = _state_after(nxt, mid, s)
            applicable = [w for w in suffixes if _enabled_at(enabled, nxt, t, w)]
            if applicable:
                for w in applicable:
                    add_case(prefix + mid + w)
            else:
                add_case(prefix + mid)
    # 互为前缀的用例只保留最长者（前缀各轮的允许观察已包含在最长者中）
    maximal = [c for c in cases
               if not any(len(d) > len(c) and d[:len(c)] == c for d in cases)]
    return sorted(maximal)


def generate_suite(spec, max_states):
    """由旧规程生成 W-method 离线一致性测试套件；拒绝时抛 GenError。"""
    if isinstance(max_states, bool) or not isinstance(max_states, int) \
            or not 1 <= max_states <= MAX_BOUND:
        raise GenError([_err("-", "maxStates",
                             f"最大稳定位置数须为 1..{MAX_BOUND} 的整数")])
    closures, enabled, obs, nxt = _determinize(spec)
    closures, enabled, obs, nxt = _minimize(closures, enabled, obs, nxt)
    nstates = len(closures)
    if max_states < nstates:
        raise GenError([_err(
            "old", "-",
            f"旧规程确定化后有 {nstates} 个稳定位置，超过所填上界 {max_states}："
            "该范围内不存在确定一致实现，请增大上界")])
    access = _access_sequences(enabled, nxt, nstates)
    suffixes = _characterization(enabled, obs, nxt, nstates)
    cases = _build_cases(enabled, nxt, nstates, max_states, access, suffixes)
    case_objs = []
    for idx, case in enumerate(cases, 1):
        rounds = []
        s = 0
        for cmd in case:
            rounds.append({"cmd": cmd, "allowed": [obs[(s, cmd)]]})
            s = nxt[(s, cmd)]
        case_objs.append({"id": f"TC-{idx}", "rounds": rounds})
    outputs = sorted("!" + t.label for t in spec.transitions
                     if t.kind == KIND_OUTPUT)
    suite = {
        "suiteId": "",
        "maxStates": max_states,
        "stateCount": nstates,
        "inputs": sorted(spec.inputs),
        "outputs": outputs,
        "states": [{"id": k, "access": list(access[k]),
                    "locations": sorted(closures[k])} for k in range(nstates)],
        "transitions": [{"from": s, "cmd": cmd, "obs": obs[(s, cmd)],
                         "to": nxt[(s, cmd)]}
                        for s in range(nstates) for cmd in enabled[s]],
        "distinguishing": [list(w) for w in suffixes],
        "cases": case_objs,
        "caseCount": len(case_objs),
    }
    if not case_objs:
        suite["note"] = "旧规程不存在每个闭包位置都允许的命令，套件为空"
    canonical = {
        "spec": {
            "locations": sorted(spec.locations),
            "initial": spec.initial,
            "inputs": sorted(spec.inputs),
            "transitions": sorted((t.src, t.dst, t.kind, t.label)
                                  for t in spec.transitions),
        },
        "maxStates": max_states,
        "cases": [[(r["cmd"], r["allowed"]) for r in c["rounds"]]
                  for c in case_objs],
    }
    digest = hashlib.sha256(
        json.dumps(canonical, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    suite["suiteId"] = "suite-" + digest.hexdigest()[:16]
    return suite


# ---------------------------------------------------------------- 回放

def _norm_cmd(token):
    """命令标签规范化：允许写作 ?cmd 或 cmd。"""
    token = token.strip()
    return token[1:] if token.startswith("?") else token


def _norm_obs(token):
    """观察标签规范化：δ / delta / 静止 视为静止，其余视为输出（! 可省略）。"""
    token = token.strip()
    if token.lower() in ("delta", DELTA, "静止"):
        return DELTA
    return token if token.startswith("!") else "!" + token


def replay(suite, case_id, rounds):
    """回放现场记录；返回 (结果字典, None) 或 (None, 错误列表)。"""
    cases = {c["id"]: c for c in suite["cases"]}
    case = cases.get(case_id)
    if case is None:
        return None, [_err("-", "caseId",
                           f"未知测试用例编号 {case_id!r}：不属于当前套件"
                           "（套件更新后旧编号即过期）")]
    steps = case["rounds"]
    inputs = set(suite["inputs"])
    outputs = set(suite["outputs"])
    for k in range(1, len(rounds) + 1):
        rnd = rounds[k - 1]
        if k > len(steps):
            return None, [_err("-", "rounds",
                               f"终止用例后继续输入：用例 {case['id']} 共 {len(steps)} 轮，"
                               f"第 {k} 轮仍收到命令，拒绝回放", k)]
        if not isinstance(rnd, dict):
            return None, [_err("-", "rounds",
                               f"第 {k} 轮格式错误：应为含 cmd 与 obs 的对象", k)]
        cmd_raw, obs_raw = rnd.get("cmd"), rnd.get("obs")
        if not isinstance(cmd_raw, str) or not isinstance(obs_raw, str):
            return None, [_err("-", "rounds",
                               f"第 {k} 轮格式错误：cmd 与 obs 均须为文本", k)]
        cmd = _norm_cmd(cmd_raw)
        if cmd not in inputs:
            return None, [_err("-", "rounds",
                               f"未知标签：命令 {cmd_raw!r} 未在旧规程输入命令中声明", k)]
        observation = _norm_obs(obs_raw)
        if observation != DELTA and observation not in outputs:
            return None, [_err("-", "rounds",
                               f"未知标签：观察 {obs_raw!r} 不是旧规程允许的输出或静止 δ", k)]
        step = steps[k - 1]
        if cmd != step["cmd"]:
            return None, [_err("-", "rounds",
                               f"第 {k} 轮命令与用例不符：用例应为 ?{step['cmd']}，"
                               f"记录为 ?{cmd}", k)]
        if observation not in step["allowed"]:
            expected = step["allowed"]
            if expected == [DELTA]:
                message = (f"第 {k} 轮越界（静默后报警）：发送 ?{cmd} 后旧规程仅允许"
                           f"静止 δ，实际收到 {observation}")
            else:
                message = (f"第 {k} 轮越界：发送 ?{cmd} 后允许观察 "
                           f"{'、'.join(expected)}，实际收到 {observation}")
            return {
                "verdict": "fail",
                "caseId": case["id"],
                "roundsChecked": k,
                "totalRounds": len(steps),
                "firstFailure": {"round": k, "cmd": cmd, "expected": expected,
                                 "observed": observation, "message": message},
            }, None
    checked = len(rounds)
    if checked < len(steps):
        return {"verdict": "incomplete", "caseId": case["id"],
                "roundsChecked": checked, "totalRounds": len(steps),
                "firstFailure": None}, None
    return {"verdict": "pass", "caseId": case["id"],
            "roundsChecked": checked, "totalRounds": len(steps),
            "firstFailure": None}, None
