"""离线一致性测试套件（W-method）生成与现场记录回放。

生成器先把旧规程含内部静默（tau）与可观察静止（δ）的语义确定化为一台
“命令 → 观察”确定机：机器状态为规程位置的稳定闭包（tau 闭包后仅允许
δ 可观察），每条边为“闭包允许的输入命令 + 该命令后唯一的可观察结果”。
仅在每个闭包允许时发出命令；若旧规程在同一命令后存在多个可观察结果
（或无可观察结果、闭包在轮次之间不稳定），拒绝生成而非任选一条。

对可生成的规程，按 W-method 构造有限套件：状态覆盖（P）、迁移覆盖
（经 K = m - n + 1 层中间序列并入）与区分后缀（W），其中 n 为确定化并
化简后的稳定状态数，m 为工程师填写的现场控制器最大稳定位置数。套件
保证：任何不超过 m 个稳定状态的确定实现，通过全部用例当且仅当它在
该范围内与规程一致。不使用固定深度枚举、随机命令或位置名称替代。

完备性说明：上述保证仅在 W-method 可构造时成立——确定化机器中各闭包
允许的命令集合须处处一致（此时等价于输入完备机，经典 W-method 适用），
或机器无循环（可执行命令树有限，覆盖即完备）。若循环内外允许的命令
集合不一致，确定实现可在不同轮次合并规程角色、把越界行为藏到
n·m 深度，任何多项式规模的 W-method 套件都无法暴露，故拒绝生成，
绝不给出猜测结论。

回放：将逐轮“发送命令、收到输出或静止”的现场记录与指定用例逐步核对，
报告首个失败轮次；静默后报警（观察越界）、终止用例后继续输入、未知
标签与过期测试标识均被清楚拒绝。
"""

from __future__ import annotations

import hashlib
import json
from collections import deque

from ioco import (
    DELTA,
    KIND_DELTA,
    KIND_INPUT,
    KIND_OUTPUT,
    ResourceLimit,
    _NAME_RE,
    _out_set,
    _step,
    _tau_closure,
)

MAX_MACHINE_STATES = 500   # 确定化机器稳定闭包数上限（资源保护）
MAX_MIDDLES = 20000        # 单个覆盖序列后的中间序列数上限
MAX_CASES = 2000           # 套件用例数上限
MAX_STEPS = 40000          # 套件全部用例总步数上限
MAX_ROUNDS = 5000          # 单条现场记录轮数上限
MAX_BOUND = 100            # 最大稳定位置数上界

_DELTA_WORDS = frozenset({"delta", "quiescent", "静止", DELTA})


class GenerateError(Exception):
    """规程无法确定化或无法生成套件；errors 为可定位的错误字典列表。"""

    def __init__(self, errors):
        super().__init__("无法生成测试套件")
        self.errors = list(errors)


class RecordError(Exception):
    """现场记录或回放请求非法；errors 为可定位的错误字典列表。"""

    def __init__(self, errors):
        super().__init__("现场记录非法")
        self.errors = list(errors)


def _err(field, message, line=None, side="old"):
    return {"side": side, "field": field, "line": line, "message": message}


class Machine:
    """“命令 → 观察”确定机：状态为稳定闭包编号，边为 (观察, 下一状态)。

    trans[(q, i)] = (obs, q')：在状态 q 发出命令 i 后，唯一可观察结果为
    obs（"!输出" 或 δ），随后机器处于稳定状态 q'。初始状态恒为 0。
    """

    __slots__ = ("nstates", "enabled", "trans")

    def __init__(self, nstates, enabled, trans):
        self.nstates = nstates
        self.enabled = enabled   # list[tuple[str, ...]]：各状态允许的命令（升序）
        self.trans = trans       # dict[(int, str)] = (str, int)


# ------------------------------------------------------------- 确定化

def build_machine(spec):
    """把旧规程确定化为“命令 → 观察”机；不可确定化时抛出 GenerateError。"""
    index = {}
    closures = []
    queue = deque()
    trans = {}
    enabled = {}

    def add(closure):
        if closure not in index:
            if len(closures) >= MAX_MACHINE_STATES:
                raise ResourceLimit(
                    f"确定化机器稳定闭包数超过上限 {MAX_MACHINE_STATES}，资源内无法生成")
            index[closure] = len(closures)
            closures.append(closure)
            queue.append(closure)
        return index[closure]

    add(_tau_closure(spec, {spec.initial}))
    any_input = False
    while queue:
        closure = queue.popleft()
        sid = index[closure]
        outs = _out_set(spec, closure)
        if outs != {DELTA}:
            shown = "、".join(sorted(outs)) if outs else "（无可观察结果，疑似静默发散）"
            raise GenerateError([_err(
                "transitions",
                f"闭包 {{ {'、'.join(sorted(closure))} }} 不是稳定状态：可观察结果为 {shown}；"
                "逐轮“命令-观察”模型要求两轮之间仅可观察静止 δ，拒绝生成")])
        inputs = sorted({t.label for loc in closure
                         for t in spec.out_map.get(loc, ())
                         if t.kind == KIND_INPUT})
        enabled[sid] = tuple(inputs)
        any_input = any_input or bool(inputs)
        for name in inputs:
            after_cmd = _step(spec, closure, KIND_INPUT, name)
            obs = _out_set(spec, after_cmd)
            if len(obs) != 1:
                if obs:
                    detail = "、".join(sorted(obs))
                    message = (f"命令 ?{name} 后可观察结果不唯一：{detail}；"
                               "拒绝生成（不能任选一条），请先消除旧规程的非确定行为")
                else:
                    message = (f"命令 ?{name} 后无可观察结果（疑似静默发散）；"
                               "拒绝生成，请先消除旧规程的内部静默循环")
                raise GenerateError([_err("transitions", message)])
            symbol = next(iter(obs))
            if symbol == DELTA:
                nxt = _step(spec, after_cmd, KIND_DELTA, DELTA)
            else:
                nxt = _step(spec, after_cmd, KIND_OUTPUT, symbol[1:])
            trans[(sid, name)] = (symbol, add(nxt))
    if not any_input:
        raise GenerateError([_err(
            "inputs", "规程没有任何闭包允许的输入命令，无法构造逐轮“命令-观察”套件")])
    n = len(closures)
    return Machine(n, [enabled[s] for s in range(n)], trans)


# ------------------------------------------------------------- 化简与区分后缀

def _initial_blocks(machine):
    """按（允许的命令集、各命令的观察）划分的初始分块。"""
    sigs = {}
    block_of = {}
    for q in range(machine.nstates):
        sig = (machine.enabled[q],
               tuple(machine.trans[(q, i)][0] for i in machine.enabled[q]))
        if sig not in sigs:
            sigs[sig] = len(sigs)
        block_of[q] = sigs[sig]
    return block_of


def _refine(machine, block_of):
    """按（当前分块、各命令的观察与后继分块）细化一次。"""
    sigs = {}
    new = {}
    for q in range(machine.nstates):
        sig = (block_of[q],
               tuple((i, machine.trans[(q, i)][0], block_of[machine.trans[(q, i)][1]])
                     for i in machine.enabled[q]))
        if sig not in sigs:
            sigs[sig] = len(sigs)
        new[q] = sigs[sig]
    return new


def _refine_to_fixpoint(machine, block_of):
    while True:
        new = _refine(machine, block_of)
        if len(set(new.values())) == len(set(block_of.values())):
            return new
        block_of = new


def _renumber_from_initial(machine, initial):
    """自 initial 广度优先重编号，使初始状态为 0（编号确定）。"""
    order = []
    seen = {initial}
    queue = deque([initial])
    while queue:
        q = queue.popleft()
        order.append(q)
        for i in machine.enabled[q]:
            r = machine.trans[(q, i)][1]
            if r not in seen:
                seen.add(r)
                queue.append(r)
    remap = {old: new for new, old in enumerate(order)}
    enabled = [machine.enabled[old] for old in order]
    trans = {}
    for old in order:
        for i in machine.enabled[old]:
            obs, nxt = machine.trans[(old, i)]
            trans[(remap[old], i)] = (obs, remap[nxt])
    return Machine(len(order), enabled, trans)


def minimize(machine):
    """行为等价（允许命令、观察与后继均一致）化简；返回化简后的确定机。"""
    blocks = _refine_to_fixpoint(machine, _initial_blocks(machine))
    reps = {}
    for q in range(machine.nstates):
        reps.setdefault(blocks[q], q)
    order = sorted(reps.values())
    block_to_qid = {}
    for new_id, rep in enumerate(order):
        block_to_qid[blocks[rep]] = new_id
    enabled = []
    trans = {}
    for new_id, rep in enumerate(order):
        enabled.append(machine.enabled[rep])
        for i in machine.enabled[rep]:
            obs, nxt = machine.trans[(rep, i)]
            trans[(new_id, i)] = (obs, block_to_qid[blocks[nxt]])
    quotient = Machine(len(order), enabled, trans)
    return _renumber_from_initial(quotient, block_to_qid[blocks[0]])


def _first_difference(machine, q1, q2):
    """q1 与 q2 在允许命令或即时观察上的首个不同输入（ASCII 序）。"""
    e1, e2 = set(machine.enabled[q1]), set(machine.enabled[q2])
    for i in sorted(e1 | e2):
        if i not in e1 or i not in e2:
            return i
        if machine.trans[(q1, i)][0] != machine.trans[(q2, i)][0]:
            return i
    raise AssertionError("状态签名相同，无可区分输入")


def _characterization_set(machine):
    """区分后缀集 W：对化简后机器的每对状态给出可区分的输入序列。"""
    n = machine.nstates
    if n <= 1:
        return []
    block_of = _initial_blocks(machine)
    w = {}
    for q1 in range(n):
        for q2 in range(q1 + 1, n):
            if block_of[q1] != block_of[q2]:
                w[(q1, q2)] = (_first_difference(machine, q1, q2),)
    while True:
        new = _refine(machine, block_of)
        if len(set(new.values())) == len(set(block_of.values())):
            break
        for q1 in range(n):
            for q2 in range(q1 + 1, n):
                if (q1, q2) in w or new[q1] == new[q2]:
                    continue
                # 新分开的对：某命令的后继在上一轮已被分开
                for i in machine.enabled[q1]:
                    r1 = machine.trans[(q1, i)][1]
                    r2 = machine.trans[(q2, i)][1]
                    if block_of[r1] != block_of[r2]:
                        key = (r1, r2) if r1 < r2 else (r2, r1)
                        w[(q1, q2)] = (i,) + w[key]
                        break
        block_of = new
    if len(set(block_of.values())) != n:
        raise GenerateError([_err("transitions", "内部错误：化简后的规程仍存在不可区分状态")])
    return sorted(set(w.values()))


# ------------------------------------------------------------- 套件构造

def _has_cycle(machine):
    """确定化机器是否含循环（沿允许的命令可达自身）。"""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = [WHITE] * machine.nstates

    def visit(start):
        stack = [(start, iter(machine.enabled[start]))]
        color[start] = GRAY
        while stack:
            q, it = stack[-1]
            advanced = False
            for i in it:
                r = machine.trans[(q, i)][1]
                if color[r] == GRAY:
                    return True
                if color[r] == WHITE:
                    color[r] = GRAY
                    stack.append((r, iter(machine.enabled[r])))
                    advanced = True
                    break
            if not advanced:
                color[q] = BLACK
                stack.pop()
        return False

    for q in range(machine.nstates):
        if color[q] == WHITE and visit(q):
            return True
    return False


def _check_wmethod_ready(machine):
    """确认 W-method 保证可构造：各闭包允许的命令集合处处一致，或机器无循环。

    循环内外允许的命令集合不一致时，确定实现可合并规程角色并把越界行为
    藏到 n·m 深度，多项式规模的 W-method 套件无法保证暴露，故拒绝生成。
    """
    enabled_sets = {machine.enabled[q] for q in range(machine.nstates)}
    if len(enabled_sets) <= 1:
        return
    if not _has_cycle(machine):
        return
    raise GenerateError([_err(
        "transitions",
        "各闭包允许的命令集合不一致且行为含循环：任何不超过上界的确定实现"
        "可借此把越界行为藏到任意套件之外，W-method 无法给出该范围内的一致"
        "保证，拒绝生成（请使每个闭包允许的命令集合一致，或消除循环）")])


def _state_cover(machine):
    """状态覆盖 P：自初始状态广度优先的最短命令序列（顺序确定）。"""
    cover = {0: ()}
    queue = deque([0])
    while queue:
        q = queue.popleft()
        for i in machine.enabled[q]:
            r = machine.trans[(q, i)][1]
            if r not in cover:
                cover[r] = cover[q] + (i,)
                queue.append(r)
    return cover


def _middle_sequences(machine, start, depth):
    """自 start 起长度不超过 depth 的全部可执行命令序列（含空序列）。"""
    seqs = [()]
    frontier = [((), start)]
    for _ in range(depth):
        nxt = []
        for seq, q in frontier:
            for i in machine.enabled[q]:
                extended = seq + (i,)
                seqs.append(extended)
                nxt.append((extended, machine.trans[(q, i)][1]))
        frontier = nxt
        if len(seqs) > MAX_MIDDLES:
            raise ResourceLimit(
                f"中间序列规模超过上限 {MAX_MIDDLES}，资源内无法生成套件")
    return seqs


def _after(machine, state, seq):
    q = state
    for i in seq:
        q = machine.trans[(q, i)][1]
    return q


def _executable(machine, state, seq):
    q = state
    for i in seq:
        if (q, i) not in machine.trans:
            return False
        q = machine.trans[(q, i)][1]
    return True


def _suite_id(spec, max_states, cases):
    """套件标识：由规程、上界与用例内容完全确定的稳定摘要。"""
    payload = {
        "locations": sorted(spec.locations),
        "initial": spec.initial,
        "inputs": sorted(spec.inputs),
        "transitions": sorted((t.src, t.dst, t.kind, t.label) for t in spec.transitions),
        "maxStates": max_states,
        "cases": [[step["command"] for step in case["steps"]] for case in cases],
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return "S-" + hashlib.sha256(blob).hexdigest()[:12]


class Suite:
    """一份生成的测试套件：稳定编号的用例、每步允许观察与区分后缀。"""

    def __init__(self, suite_id, inputs, states, bound, cases, distinguishing):
        self.id = suite_id
        self.inputs = frozenset(inputs)
        self.states = states
        self.bound = bound
        self.cases = cases
        self.distinguishing = [list(w) for w in distinguishing]
        self._case_map = {c["id"]: c for c in cases}

    def case(self, case_id):
        return self._case_map.get(case_id)

    def to_json(self):
        return {
            "suite": self.id,
            "states": self.states,
            "bound": self.bound,
            "inputs": sorted(self.inputs),
            "distinguishing": self.distinguishing,
            "caseCount": len(self.cases),
            "cases": self.cases,
        }


def generate_suite(spec, max_states):
    """按 W-method 为旧规程生成有限离线一致性测试套件。

    max_states 为工程师填写的现场控制器最大稳定位置数 m；n 为规程确定化
    并化简后的稳定状态数。套件 = 状态覆盖 · I^{≤m-n+1} · (区分后缀 ∪ {ε})，
    仅保留每个闭包允许的可执行序列；空后缀 ε 保证迁移覆盖上的每条迁移
    都作为用例出现。任何不超过 m 个稳定状态的确定实现通过全部用例，
    当且仅当它在该范围内与规程一致。
    """
    machine = minimize(build_machine(spec))
    _check_wmethod_ready(machine)
    n = machine.nstates
    if max_states < n:
        raise GenerateError([_err(
            "maxStates",
            f"最大稳定位置数 {max_states} 小于规程确定化后的稳定状态数 {n}；"
            "该范围内不存在与规程一致的确定实现，请增大上界", side="-")])
    depth = max_states - n + 1
    cover = _state_cover(machine)
    w_set = _characterization_set(machine)
    # 后缀 = 区分后缀 W 与空后缀 ε：ε 保证状态覆盖 · 中间序列上的每条迁移
    # （迁移覆盖）都作为用例出现，即使 W 在其后继闭包不可执行。
    suffixes = sorted(set(w_set) | {()})
    tests = set()
    for seq_c in sorted(cover.values(), key=lambda s: (len(s), s)):
        base = _after(machine, 0, seq_c)
        for mid in _middle_sequences(machine, base, depth):
            reached = _after(machine, base, mid)
            for w in suffixes:
                if _executable(machine, reached, w):
                    tests.add(seq_c + mid + w)
                    if len(tests) > MAX_CASES:
                        raise ResourceLimit(
                            f"套件用例数超过上限 {MAX_CASES}，资源内无法生成")
    tests.discard(())
    ordered = sorted(tests, key=lambda s: (len(s), s))
    cases = []
    total = 0
    for k, seq in enumerate(ordered, start=1):
        steps = []
        q = 0
        for i in seq:
            obs, q = machine.trans[(q, i)]
            steps.append({"command": i, "allowed": [obs]})
        total += len(steps)
        if total > MAX_STEPS:
            raise ResourceLimit(f"套件总步数超过上限 {MAX_STEPS}，资源内无法生成")
        cases.append({"id": f"T{k:03d}", "steps": steps})
    return Suite(_suite_id(spec, max_states, cases), spec.inputs,
                 n, max_states, cases, w_set)


# ------------------------------------------------------------- 现场记录回放

def parse_record(text, inputs):
    """把现场记录文本解析为逐轮 (命令, 观察)；非法时抛出 RecordError。

    每行一轮：`命令 观察`（空白分隔；# 开头为注释，空行跳过）。
    命令为已声明的输入命令（可带 ? 前缀）；观察为 !输出 或 δ/delta（静止）。
    """
    if not isinstance(text, str):
        raise RecordError([_err("record", "字段类型错误：应为文本，每行一轮", side="-")])
    rounds = []
    errors = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) != 2:
            errors.append(_err("record",
                               f"非法记录行 {line!r}：应为 ‘命令 观察’ 两段", lineno, "-"))
            continue
        cmd_raw, obs_raw = parts
        cmd = cmd_raw[1:] if cmd_raw.startswith("?") else cmd_raw
        if not _NAME_RE.match(cmd) or cmd not in inputs:
            errors.append(_err("record",
                               f"未知标签：命令 {cmd_raw!r} 未在输入命令中声明", lineno, "-"))
            continue
        if obs_raw.lower() in _DELTA_WORDS:
            obs = DELTA
        elif obs_raw.startswith("!") and _NAME_RE.match(obs_raw[1:]):
            obs = obs_raw
        else:
            errors.append(_err("record",
                               f"未知标签：观察 {obs_raw!r} 应为 !输出 或 δ（静止）", lineno, "-"))
            continue
        rounds.append((cmd, obs))
        if len(rounds) > MAX_ROUNDS:
            errors.append(_err("record", f"记录轮数超过上限 {MAX_ROUNDS}", lineno, "-"))
            break
    if errors:
        raise RecordError(errors)
    return rounds


def replay_rounds(suite, case_id, rounds):
    """把逐轮记录与用例逐步核对，返回首个失败轮次或通过结论。"""
    case = suite.case(case_id)
    if case is None:
        raise RecordError([_err(
            "case", f"未知用例编号 {case_id!r}：不在套件 {suite.id} 中", side="-")])
    steps = case["steps"]
    for k, (cmd, obs) in enumerate(rounds, start=1):
        if k > len(steps):
            return {
                "verdict": "failed", "case": case_id,
                "checkedRounds": len(steps), "totalRounds": len(steps),
                "firstFailure": {
                    "round": k, "reason": "after-termination",
                    "message": f"用例已终止（共 {len(steps)} 步），记录仍继续输入 ?{cmd}",
                    "command": cmd, "observation": obs, "allowed": [],
                },
            }
        step = steps[k - 1]
        if cmd != step["command"]:
            return {
                "verdict": "failed", "case": case_id,
                "checkedRounds": k - 1, "totalRounds": len(steps),
                "firstFailure": {
                    "round": k, "reason": "command-mismatch",
                    "message": f"第 {k} 轮命令 ?{cmd} 与用例步骤 ?{step['command']} 不符",
                    "command": cmd, "expectedCommand": step["command"],
                    "observation": obs, "allowed": step["allowed"],
                },
            }
        if obs not in step["allowed"]:
            return {
                "verdict": "failed", "case": case_id,
                "checkedRounds": k - 1, "totalRounds": len(steps),
                "firstFailure": {
                    "round": k, "reason": "out-of-bounds",
                    "message": f"第 {k} 轮观察 {obs} 越界：该步仅允许 "
                               f"{'、'.join(step['allowed'])}",
                    "command": cmd, "observation": obs, "allowed": step["allowed"],
                },
            }
    if len(rounds) < len(steps):
        return {
            "verdict": "incomplete", "case": case_id,
            "checkedRounds": len(rounds), "totalRounds": len(steps),
            "firstFailure": None,
        }
    return {
        "verdict": "passed", "case": case_id,
        "checkedRounds": len(steps), "totalRounds": len(steps),
        "firstFailure": None,
    }
