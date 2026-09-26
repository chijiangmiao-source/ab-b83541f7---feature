"""束线联锁控制器规程升级的 ioco 复核引擎。

语义：输入输出一致性（ioco），纳入内部静默迁移（tau）与可观察静止（δ）。
判定方法：对“候选状态集 × 旧规程状态集”做共归纳式广度优先探索——
两侧均先求 tau 闭包，再沿旧规程的悬挂迹（输入、输出、δ）同步推进；
每到达一对状态集，检查候选在该历史后可能给出的每项输出（含 δ）
是否都被旧规程在同一历史允许。判定不使用状态名对应、
不做有限长度枚举，也不做随机回放。
"""

from __future__ import annotations

import re
from collections import deque

DELTA = "δ"  # 可观察静止
TAU = "τ"    # 内部静默

KIND_INPUT = "input"
KIND_OUTPUT = "output"
KIND_TAU = "tau"
KIND_DELTA = "delta"

_TAU_WORDS = frozenset({"tau", TAU})
_DELTA_WORDS = frozenset({"delta", DELTA, "quiescent", "静止"})

_NAME_RE = re.compile(r"^\w[\w-]*$")
_LINE_RE = re.compile(r"^\s*(\S+)\s*->\s*(\S+)\s*:\s*(\S+)\s*$")
_SPLIT_RE = re.compile(r"[,\s，、]+")

# 探索的状态集对上限（资源保护；超限将如实报告“无法判定”，绝不给出猜测结论）
MAX_PAIRS = 50000


class SpecError(Exception):
    """规程录入非法；errors 为可定位的错误字典列表。"""

    def __init__(self, errors):
        super().__init__("规程录入非法")
        self.errors = list(errors)


class ResourceLimit(Exception):
    """探索规模超出资源上限。"""


def _err(side, field, message, line=None):
    return {"side": side, "field": field, "line": line, "message": message}


class Transition:
    __slots__ = ("src", "dst", "kind", "label", "line")

    def __init__(self, src, dst, kind, label, line):
        self.src = src
        self.dst = dst
        self.kind = kind      # input / output / tau / delta
        self.label = label    # input/output 的名称；tau 为 ""；delta 为 DELTA
        self.line = line

    def __repr__(self):
        return f"Transition({self.src!r}, {self.dst!r}, {self.kind!r}, {self.label!r})"


class Spec:
    """一份规程：有限位置、初始位置、输入命令与带标签迁移。"""

    def __init__(self, locations, initial, inputs, transitions):
        self.locations = frozenset(locations)
        self.initial = initial
        self.inputs = frozenset(inputs)
        self.transitions = tuple(transitions)
        out_map = {loc: [] for loc in self.locations}
        for t in self.transitions:
            out_map[t.src].append(t)
        self.out_map = {k: tuple(v) for k, v in out_map.items()}


def sym_display(kind, label):
    """悬挂迹符号的展示形式：?命令 / !输出 / δ。"""
    if kind == KIND_INPUT:
        return "?" + label
    if kind == KIND_OUTPUT:
        return "!" + label
    if kind == KIND_DELTA:
        return DELTA
    raise ValueError(f"未知符号种类: {kind!r}")


def _reserved(name):
    return name.lower() in _TAU_WORDS or name.lower() in _DELTA_WORDS


def _parse_name_list(text, side, field, errors, dup_message):
    names = []
    if not isinstance(text, str):
        errors.append(_err(side, field, "字段类型错误：应为文本"))
        return names
    seen = set()
    for name in (p for p in _SPLIT_RE.split(text.strip()) if p):
        if not _NAME_RE.match(name):
            errors.append(_err(side, field, f"非法标签/名称 {name!r}：仅允许字母、数字、下划线与连字符"))
        elif _reserved(name):
            errors.append(_err(side, field, f"非法名称 {name!r}：tau/delta 为保留写法"))
        elif name in seen:
            errors.append(_err(side, field, dup_message.format(name)))
        else:
            seen.add(name)
            names.append(name)
    return names


def _parse_transitions(text, loc_set, input_set, side, errors):
    transitions = []
    if text is None:
        return transitions
    if not isinstance(text, str):
        errors.append(_err(side, "transitions", "字段类型错误：应为文本，每行一条迁移"))
        return transitions
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _LINE_RE.match(line)
        if not m:
            errors.append(_err(side, "transitions",
                               f"非法标签或写法 {line!r}：应为 ‘源 -> 目标 : 标签’", lineno))
            continue
        src, dst, label = m.groups()
        low = label.lower()
        if low in _TAU_WORDS:
            kind, name = KIND_TAU, ""
        elif low in _DELTA_WORDS:
            kind, name = KIND_DELTA, DELTA
        elif label.startswith("?"):
            name = label[1:]
            if not name or not _NAME_RE.match(name) or _reserved(name):
                errors.append(_err(side, "transitions",
                                   f"非法标签 {label!r}：输入命令名缺失或含非法字符", lineno))
                continue
            if name not in input_set:
                errors.append(_err(side, "transitions",
                                   f"非法标签 {label!r}：输入命令 {name!r} 未在输入命令中声明", lineno))
                continue
            kind = KIND_INPUT
        elif label.startswith("!"):
            name = label[1:]
            if not name or not _NAME_RE.match(name) or _reserved(name):
                errors.append(_err(side, "transitions",
                                   f"非法标签 {label!r}：输出名缺失或含非法字符", lineno))
                continue
            kind = KIND_OUTPUT
        else:
            errors.append(_err(side, "transitions",
                               f"非法标签 {label!r}：须为 ?命令、!输出、tau 或 delta", lineno))
            continue
        dangling = False
        for loc in (src, dst):
            if loc not in loc_set:
                errors.append(_err(side, "transitions",
                                   f"悬空迁移：位置 {loc!r} 未在位置列表中声明", lineno))
                dangling = True
        if not dangling:
            transitions.append(Transition(src, dst, kind, name, lineno))
    return transitions


def parse_spec(raw, side):
    """把网页录入的原始字段解析为 Spec；非法时抛出携带定位信息的 SpecError。"""
    if not isinstance(raw, dict):
        raise SpecError([_err(side, "-", "规程数据缺失或类型错误")])
    errors = []
    locations = _parse_name_list(raw.get("locations", ""), side, "locations",
                                 errors, "同一标签下重复状态声明：{0!r}")
    if not locations and not any(e["field"] == "locations" for e in errors):
        errors.append(_err(side, "locations", "位置列表为空：须声明有限位置"))
    loc_set = set(locations)
    initial = raw.get("initial", "")
    if not isinstance(initial, str):
        errors.append(_err(side, "initial", "字段类型错误：应为文本"))
        initial = ""
    initial = initial.strip()
    if not initial:
        errors.append(_err(side, "initial", "缺少初始位置"))
    elif initial not in loc_set:
        errors.append(_err(side, "initial", f"初始位置 {initial!r} 未在位置列表中声明"))
    inputs = _parse_name_list(raw.get("inputs", ""), side, "inputs",
                              errors, "重复声明的输入命令：{0!r}")
    transitions = _parse_transitions(raw.get("transitions", ""), loc_set, set(inputs), side, errors)
    if errors:
        raise SpecError(errors)
    return Spec(locations, initial, inputs, transitions)


# ---------------------------------------------------------------- ioco 判定

def _tau_closure(spec, states):
    """tau 闭包：经零次或多次内部静默迁移可达的全部位置。"""
    seen = set(states)
    stack = list(states)
    while stack:
        s = stack.pop()
        for t in spec.out_map.get(s, ()):
            if t.kind == KIND_TAU and t.dst not in seen:
                seen.add(t.dst)
                stack.append(t.dst)
    return frozenset(seen)


def _quiescent(spec, s):
    """派生静止：该位置没有任何输出、静默或显式静止迁移（输入不破坏静止）。"""
    for t in spec.out_map.get(s, ()):
        if t.kind in (KIND_OUTPUT, KIND_TAU, KIND_DELTA):
            return False
    return True


def _out_set(spec, states):
    """状态集当前可给出的输出集合（含可观察静止 δ）。"""
    outs = set()
    for s in states:
        for t in spec.out_map.get(s, ()):
            if t.kind == KIND_OUTPUT:
                outs.add(sym_display(KIND_OUTPUT, t.label))
            elif t.kind == KIND_DELTA:
                outs.add(DELTA)
        if _quiescent(spec, s):
            outs.add(DELTA)
    return outs


def _step(spec, states, kind, label):
    """沿一个悬挂迹符号推进并求 tau 闭包；δ 为派生静止的自循环加上显式静止迁移。"""
    nxt = set()
    if kind == KIND_DELTA:
        for s in states:
            if _quiescent(spec, s):
                nxt.add(s)
            for t in spec.out_map.get(s, ()):
                if t.kind == KIND_DELTA:
                    nxt.add(t.dst)
    else:
        for s in states:
            for t in spec.out_map.get(s, ()):
                if t.kind == kind and t.label == label:
                    nxt.add(t.dst)
    return _tau_closure(spec, nxt)


def _successor_symbols(spec, states):
    """旧规程在 states 可做的悬挂动作（输入/输出/δ），按 ASCII 顺序排列。"""
    syms = set()
    for s in states:
        for t in spec.out_map.get(s, ()):
            if t.kind in (KIND_INPUT, KIND_OUTPUT):
                syms.add((t.kind, t.label))
            elif t.kind == KIND_DELTA:
                syms.add((KIND_DELTA, DELTA))
        if _quiescent(spec, s):
            syms.add((KIND_DELTA, DELTA))
    return sorted(syms, key=lambda sym: sym_display(*sym))


class CheckResult:
    def __init__(self, consistent, explored, path=None, offending=None,
                 candidate_outputs=(), old_outputs=()):
        self.consistent = consistent
        self.explored = explored
        self.path = path or []  # [((候选闭包, 旧规程闭包), 到达该对的符号)]
        self.offending = offending
        self.candidate_outputs = list(candidate_outputs)
        self.old_outputs = list(old_outputs)

    def to_json(self):
        if self.consistent:
            return {"verdict": "consistent", "explored": self.explored}
        steps = []
        history = []
        for (cand_set, old_set), sym in self.path:
            disp = sym_display(*sym) if sym else None
            steps.append({
                "label": disp,
                "candidate": sorted(cand_set),
                "old": sorted(old_set),
            })
            if disp is not None:
                history.append(disp)
        history.append(self.offending)
        return {
            "verdict": "inconsistent",
            "explored": self.explored,
            "history": history,
            "steps": steps,
            "offendingOutput": self.offending,
            "candidateOutputs": self.candidate_outputs,
            "oldOutputs": self.old_outputs,
        }


def check_ioco(old, cand):
    """判定 cand 是否 ioco 符合 old。

    广度优先探索（候选闭包 × 旧规程闭包）对：每个对处核对
    out(候选) ⊆ out(旧规程)；否则按 ASCII 顺序取最小越界输出，
    所得历史即为按 ASCII 顺序确定的最短反例历史。
    """
    start = (_tau_closure(cand, {cand.initial}), _tau_closure(old, {old.initial}))
    parent = {start: None}  # pair -> (父 pair, 符号)
    queue = deque([start])
    explored = 0
    while queue:
        cand_set, old_set = queue.popleft()
        explored += 1
        out_c = _out_set(cand, cand_set)
        out_o = _out_set(old, old_set)
        bad = sorted(out_c - out_o)
        if bad:
            path = []
            node = (cand_set, old_set)
            while True:
                entry = parent[node]
                if entry is None:
                    path.append((node, None))
                    break
                prev, sym = entry
                path.append((node, sym))
                node = prev
            path.reverse()
            return CheckResult(False, explored, path=path, offending=bad[0],
                               candidate_outputs=sorted(out_c),
                               old_outputs=sorted(out_o))
        for sym in _successor_symbols(old, old_set):
            next_old = _step(old, old_set, *sym)
            if not next_old:
                continue
            next_cand = _step(cand, cand_set, *sym)
            if not next_cand:
                continue
            pair = (next_cand, next_old)
            if pair not in parent:
                if len(parent) >= MAX_PAIRS:
                    raise ResourceLimit(
                        f"状态集对探索超过上限 {MAX_PAIRS}，资源内无法判定")
                parent[pair] = ((cand_set, old_set), sym)
                queue.append(pair)
    return CheckResult(True, explored)
