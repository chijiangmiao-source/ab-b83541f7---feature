import itertools
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ioco import check_ioco, parse_spec
from wmethod import (
    GenerateError,
    RecordError,
    generate_suite,
    parse_record,
    replay_rounds,
)


def make_spec(locations, initial, inputs, transitions, side="old"):
    return parse_spec(
        {"locations": locations, "initial": initial,
         "inputs": inputs, "transitions": transitions},
        side,
    )


def gen(locations, initial, inputs, transitions, max_states):
    return generate_suite(
        make_spec(locations, initial, inputs, transitions), max_states)


def gen_errors(locations, initial, inputs, transitions, max_states):
    try:
        gen(locations, initial, inputs, transitions, max_states)
    except GenerateError as exc:
        return exc.errors
    raise AssertionError("应当拒绝生成")


def case_steps(suite, case_id):
    return [dict(step) for step in suite.case(case_id)["steps"]]


class TestDeterminization(unittest.TestCase):
    def test_quiescence_after_tau_closure(self):
        """静默闭包后静止：?cmd 经 tau 闭包后仅可观察 δ。"""
        suite = gen("S0 S1 S2", "S0", "cmd",
                    "S0 -> S1 : ?cmd\nS1 -> S2 : tau", 2)
        self.assertEqual(suite.states, 2)
        self.assertEqual(len(suite.cases), 1)
        self.assertEqual(case_steps(suite, "T001"),
                         [{"command": "cmd", "allowed": ["δ"]}])

    def test_explicit_delta_self_loop(self):
        """显式标记为静止的输出与派生静止同样参与确定化。"""
        suite = gen("S0 S1", "S0", "cmd",
                    "S0 -> S1 : ?cmd\nS1 -> S1 : delta", 2)
        self.assertEqual(case_steps(suite, "T001"),
                         [{"command": "cmd", "allowed": ["δ"]}])

    def test_delta_then_input_continues(self):
        """静止后仍可按闭包允许的命令继续。"""
        suite = gen("S0 S1 S2", "S0", "cmd reset",
                    "S0 -> S1 : ?cmd\nS1 -> S2 : ?reset", 3)
        cases = {c["id"]: [s["command"] for s in c["steps"]] for c in suite.cases}
        self.assertEqual(cases, {"T001": ["cmd"], "T002": ["cmd", "reset"]})
        self.assertEqual(case_steps(suite, "T002"),
                         [{"command": "cmd", "allowed": ["δ"]},
                          {"command": "reset", "allowed": ["δ"]}])

    def test_output_response_machine(self):
        """命令后唯一输出 !x，观察后进入稳定状态；各闭包允许命令集合一致。"""
        suite = gen("A B C E", "A", "a b",
                    "A -> B : ?a\nB -> C : !x\nA -> A : ?b\n"
                    "C -> A : ?a\nC -> E : ?b\nE -> C : !x", 2)
        self.assertEqual(suite.states, 2)
        self.assertEqual(suite.distinguishing, [["a"]])
        cases = {c["id"]: [s["command"] for s in c["steps"]] for c in suite.cases}
        self.assertEqual(cases, {"T001": ["a"], "T002": ["b"], "T003": ["a", "a"],
                                 "T004": ["a", "b"], "T005": ["b", "a"],
                                 "T006": ["a", "a", "a"], "T007": ["a", "b", "a"]})
        self.assertEqual(case_steps(suite, "T001"), [{"command": "a", "allowed": ["!x"]}])
        self.assertEqual(case_steps(suite, "T005"),
                         [{"command": "b", "allowed": ["δ"]},
                          {"command": "a", "allowed": ["!x"]}])

    def test_suite_id_and_numbering_stable(self):
        """同一规程与上界生成的套件标识与用例编号稳定不变。"""
        args = ("P0 P1 P2 P3", "P0", "a",
                "P0 -> P1 : ?a\nP1 -> P2 : ?a\nP2 -> P3 : !x\nP3 -> P1 : ?a", 3)
        first, second = gen(*args), gen(*args)
        self.assertEqual(first.id, second.id)
        self.assertTrue(first.id.startswith("S-"))
        self.assertEqual(first.to_json(), second.to_json())
        self.assertEqual([c["id"] for c in first.cases],
                         ["T%03d" % k for k in range(1, len(first.cases) + 1)])

    def test_minimization_merges_equivalent_states(self):
        """行为等价的位置在确定化后合并（n 为化简后的稳定状态数）。"""
        suite = gen("P0 P1 P2 P3", "P0", "a",
                    "P0 -> P1 : ?a\nP1 -> P2 : ?a\nP2 -> P3 : !x\nP3 -> P1 : ?a", 2)
        self.assertEqual(suite.states, 2)


class TestGenerationRefusal(unittest.TestCase):
    def test_multiple_outputs_after_same_command(self):
        """同一命令后多个可观察输出：拒绝生成而不能任选一条。"""
        errs = gen_errors("S0 S1 S2", "S0", "cmd",
                          "S0 -> S1 : ?cmd\nS0 -> S2 : ?cmd\n"
                          "S1 -> S1 : !x\nS2 -> S2 : !y", 3)
        self.assertTrue(any("不唯一" in e["message"] for e in errs))

    def test_output_and_delta_after_same_command(self):
        """同一命令后既可输出又可静止：同样拒绝。"""
        errs = gen_errors("S0 S1 S2", "S0", "cmd",
                          "S0 -> S1 : ?cmd\nS0 -> S2 : ?cmd\nS1 -> S1 : !x", 3)
        self.assertTrue(any("不唯一" in e["message"] for e in errs))

    def test_unstable_state_between_rounds(self):
        """轮次之间仍有待发输出（非稳定状态）：拒绝。"""
        errs = gen_errors("S0 S1 S2 S3", "S0", "cmd",
                          "S0 -> S1 : ?cmd\nS1 -> S2 : !ack\nS2 -> S3 : !ack2", 4)
        self.assertTrue(any("不是稳定状态" in e["message"] for e in errs))

    def test_tau_divergence_refused(self):
        """内部静默循环导致无可观察结果：拒绝。"""
        errs = gen_errors("S0 S1 S2", "S0", "cmd",
                          "S0 -> S1 : ?cmd\nS1 -> S2 : tau\nS2 -> S1 : tau", 3)
        self.assertTrue(any("无可观察结果" in e["message"] for e in errs))

    def test_no_enabled_input_refused(self):
        errs = gen_errors("S0 S1", "S0", "cmd", "", 2)
        self.assertTrue(any("没有任何闭包允许的输入命令" in e["message"] for e in errs))

    def test_bound_below_state_count_refused(self):
        errs = gen_errors("S0 S1", "S0", "cmd", "S0 -> S1 : ?cmd", 1)
        self.assertTrue(any("小于" in e["message"] for e in errs))

    def test_cyclic_inconsistent_enabled_sets_refused(self):
        """循环内外允许的命令集合不一致：W-method 保证不可构造，拒绝。"""
        errs = gen_errors("S0 S1 S2", "S0", "a b",
                          "S0 -> S1 : ?a\nS1 -> S2 : !x\nS2 -> S0 : ?b", 2)
        self.assertTrue(any("命令集合不一致" in e["message"] for e in errs))

    def test_acyclic_inconsistent_enabled_sets_allowed(self):
        """无循环时可执行命令树有限，允许命令集合不一致也可生成。"""
        suite = gen("L0 L1 L2", "L0", "a", "L0 -> L1 : ?a\nL1 -> L2 : ?a", 3)
        cases = {c["id"]: [s["command"] for s in c["steps"]] for c in suite.cases}
        self.assertEqual(cases, {"T001": ["a"], "T002": ["a", "a"]})


class TestWMethodGuarantee(unittest.TestCase):
    """穷举不超过上界的全部确定实现：通过套件当且仅当 ioco 一致。"""

    @staticmethod
    def mealy_to_spec(states, inputs):
        """确定 Mealy 实现（每状态每输入 -> (观察, 次态)）转为规程文本。"""
        locations, transitions = [], []
        for s, acts in enumerate(states):
            locations.append(f"p{s}")
            for cmd in sorted(acts):
                symbol, nxt_state = acts[cmd]
                if symbol == "δ":
                    transitions.append(f"p{s} -> p{nxt_state} : ?{cmd}")
                else:
                    mid = f"m{s}_{cmd}"
                    locations.append(mid)
                    transitions.append(f"p{s} -> {mid} : ?{cmd}")
                    transitions.append(f"{mid} -> p{nxt_state} : {symbol}")
        return make_spec(" ".join(locations), "p0", " ".join(inputs),
                         "\n".join(transitions))

    @staticmethod
    def passes(suite, states):
        for case in suite.cases:
            q = 0
            for step in case["steps"]:
                obs, q = states[q][step["command"]]
                if obs != step["allowed"][0]:
                    return False
        return True

    def enumerate_impls(self, inputs, outputs, max_states):
        for k in range(1, max_states + 1):
            cells = [(s, i) for s in range(k) for i in inputs]
            options = [(obs, nxt) for obs in outputs for nxt in range(k)]
            for choice in itertools.product(options, repeat=len(cells)):
                states = [dict() for _ in range(k)]
                for (s, i), pair in zip(cells, choice):
                    states[s][i] = pair
                yield states

    def check_guarantee(self, old_args, max_states, inputs, outputs):
        old = make_spec(*old_args)
        suite = generate_suite(old, max_states)
        seen = 0
        for states in self.enumerate_impls(inputs, outputs, max_states):
            impl = self.mealy_to_spec(states, inputs)
            consistent = check_ioco(old, impl).consistent
            passed = self.passes(suite, states)
            self.assertEqual(passed, consistent,
                             f"实现 {states}：套件判定与 ioco 判定不符")
            seen += 1
        return seen

    def test_guarantee_single_input_with_extra_state(self):
        """单输入、上界 m = n + 1：穷举 234 台确定实现逐一核对。"""
        seen = self.check_guarantee(
            ("P0 P1 P2 P3", "P0", "a",
             "P0 -> P1 : ?a\nP1 -> P2 : ?a\nP2 -> P3 : !x\nP3 -> P1 : ?a"),
            3, ["a"], ["δ", "!x"])
        self.assertEqual(seen, 2 + 16 + 216)

    def test_guarantee_two_inputs_equal_enabled_sets(self):
        """双输入、各闭包允许命令集合一致、m = n：穷举 260 台实现。"""
        seen = self.check_guarantee(
            ("A B C E", "A", "a b",
             "A -> B : ?a\nB -> C : !x\nA -> A : ?b\n"
             "C -> A : ?a\nC -> E : ?b\nE -> C : !x"),
            2, ["a", "b"], ["δ", "!x"])
        self.assertEqual(seen, 4 + 256)

    def test_guarantee_acyclic_chain_with_dead_end(self):
        """无循环链（允许命令集合不一致、含终止状态）、m = n：穷举核对。"""
        seen = self.check_guarantee(
            ("L0 L1 L2", "L0", "a", "L0 -> L1 : ?a\nL1 -> L2 : ?a"),
            3, ["a"], ["δ", "!x"])
        self.assertEqual(seen, 2 + 16 + 216)

    def test_guarantee_acyclic_diamond_transition_cover(self):
        """无循环菱形（迁移覆盖须经空后缀纳入）、m = n + 1：穷举 46916 台实现。"""
        seen = self.check_guarantee(
            ("L0 M L1", "L0", "a b", "L0 -> L1 : ?a\nL0 -> M : ?b\nM -> L1 : !x"),
            3, ["a", "b"], ["δ", "!x"])
        self.assertEqual(seen, 4 + 256 + 46656)

    def test_guarantee_silence_closure_spec(self):
        """静默闭包后静止的验收规程、m = n + 1：穷举核对。"""
        seen = self.check_guarantee(
            ("S0 S1 S2", "S0", "cmd", "S0 -> S1 : ?cmd\nS1 -> S2 : tau"),
            3, ["cmd"], ["δ", "!alarm"])
        self.assertEqual(seen, 2 + 16 + 216)


class TestRecordParsing(unittest.TestCase):
    def parse(self, text):
        return parse_record(text, {"cmd", "reset"})

    def test_rounds_and_comments(self):
        rounds = self.parse("# 现场记录\n\ncmd δ\nreset !ack\n")
        self.assertEqual(rounds, [("cmd", "δ"), ("reset", "!ack")])

    def test_command_prefix_and_delta_words(self):
        self.assertEqual(self.parse("?cmd delta"), [("cmd", "δ")])
        self.assertEqual(self.parse("cmd 静止"), [("cmd", "δ")])

    def record_errors(self, text):
        try:
            self.parse(text)
        except RecordError as exc:
            return exc.errors
        raise AssertionError("应当抛出 RecordError")

    def test_unknown_command_label(self):
        errs = self.record_errors("nope δ")
        self.assertTrue(any("未知标签" in e["message"] for e in errs))
        self.assertEqual(errs[0]["line"], 1)

    def test_unknown_observation_label(self):
        errs = self.record_errors("cmd siren\ncmd ?reset")
        self.assertTrue(all("未知标签" in e["message"] for e in errs))
        self.assertEqual([e["line"] for e in errs], [1, 2])

    def test_malformed_line(self):
        errs = self.record_errors("cmd")
        self.assertTrue(any("两段" in e["message"] for e in errs))


class TestReplay(unittest.TestCase):
    def setUp(self):
        self.suite = gen("S0 S1 S2", "S0", "cmd",
                         "S0 -> S1 : ?cmd\nS1 -> S2 : tau", 2)

    def replay(self, record, case="T001"):
        rounds = parse_record(record, self.suite.inputs)
        return replay_rounds(self.suite, case, rounds)

    def test_quiescence_passes(self):
        """静默闭包后的静止：回放通过。"""
        result = self.replay("cmd δ")
        self.assertEqual(result["verdict"], "passed")
        self.assertEqual(result["checkedRounds"], 1)
        self.assertIsNone(result["firstFailure"])

    def test_alarm_after_silence_fails(self):
        """静默后报警：首个失败轮次为第 1 轮，允许观察为 δ。"""
        result = self.replay("cmd !alarm")
        self.assertEqual(result["verdict"], "failed")
        failure = result["firstFailure"]
        self.assertEqual(failure["round"], 1)
        self.assertEqual(failure["reason"], "out-of-bounds")
        self.assertEqual(failure["observation"], "!alarm")
        self.assertEqual(failure["allowed"], ["δ"])

    def test_input_after_termination_fails(self):
        """终止用例后继续输入：首个失败轮次为终止后的第一轮。"""
        result = self.replay("cmd δ\ncmd δ")
        self.assertEqual(result["verdict"], "failed")
        failure = result["firstFailure"]
        self.assertEqual(failure["round"], 2)
        self.assertEqual(failure["reason"], "after-termination")

    def test_command_mismatch_fails(self):
        suite = gen("S0 S1 S2", "S0", "cmd reset",
                    "S0 -> S1 : ?cmd\nS1 -> S2 : ?reset", 3)
        rounds = parse_record("cmd δ\ncmd δ", suite.inputs)
        result = replay_rounds(suite, "T002", rounds)
        self.assertEqual(result["verdict"], "failed")
        failure = result["firstFailure"]
        self.assertEqual(failure["reason"], "command-mismatch")
        self.assertEqual(failure["expectedCommand"], "reset")

    def test_incomplete_record(self):
        suite = gen("S0 S1 S2", "S0", "cmd reset",
                    "S0 -> S1 : ?cmd\nS1 -> S2 : ?reset", 3)
        rounds = parse_record("cmd δ", suite.inputs)
        result = replay_rounds(suite, "T002", rounds)
        self.assertEqual(result["verdict"], "incomplete")
        self.assertEqual(result["checkedRounds"], 1)
        self.assertEqual(result["totalRounds"], 2)

    def test_unknown_case_rejected(self):
        try:
            replay_rounds(self.suite, "T999", [])
        except RecordError as exc:
            self.assertTrue(any("未知用例编号" in e["message"] for e in exc.errors))
            return
        self.fail("应当抛出 RecordError")


if __name__ == "__main__":
    unittest.main()
