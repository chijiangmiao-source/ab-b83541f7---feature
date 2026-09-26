import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ioco import SpecError, check_ioco, parse_spec


def make_spec(locations, initial, inputs, transitions, side="old"):
    return parse_spec(
        {"locations": locations, "initial": initial,
         "inputs": inputs, "transitions": transitions},
        side,
    )


class TestConformance(unittest.TestCase):
    def test_renamed_locations_consistent(self):
        """仅位置名称不同、输入输出与静止行为相同 -> 一致。"""
        old = make_spec("Idle Busy Done", "Idle", "open close",
                        "Idle -> Busy : ?open\nBusy -> Done : !ack\nDone -> Idle : ?close")
        cand = make_spec("L0 L1 L2", "L0", "open close",
                         "L0 -> L1 : ?open\nL1 -> L2 : !ack\nL2 -> L0 : ?close")
        self.assertTrue(check_ioco(old, cand).consistent)

    def test_same_names_still_judged_by_behavior(self):
        """位置名称相同但行为不同仍判不一致（不按状态名对应）。"""
        old = make_spec("A B", "A", "cmd", "A -> B : ?cmd")
        cand = make_spec("A B", "A", "cmd", "A -> B : ?cmd\nB -> B : !alarm")
        self.assertFalse(check_ioco(old, cand).consistent)

    def test_alarm_after_silence(self):
        """旧规程命令后只能静止，候选经静默迁移后报警 -> 不一致。"""
        old = make_spec("S0 S1", "S0", "cmd", "S0 -> S1 : ?cmd")
        cand = make_spec("A B C", "A", "cmd",
                         "A -> B : ?cmd\nB -> C : tau\nC -> C : !alarm")
        res = check_ioco(old, cand)
        self.assertFalse(res.consistent)
        j = res.to_json()
        self.assertEqual(j["history"], ["?cmd", "!alarm"])
        self.assertEqual(j["offendingOutput"], "!alarm")
        self.assertEqual(j["oldOutputs"], ["δ"])
        self.assertIn("!alarm", j["candidateOutputs"])
        # 两侧逐步闭包状态集
        self.assertEqual(j["steps"][0]["label"], None)
        self.assertEqual(j["steps"][0]["candidate"], ["A"])
        self.assertEqual(j["steps"][0]["old"], ["S0"])
        self.assertEqual(j["steps"][1]["label"], "?cmd")
        self.assertEqual(j["steps"][1]["candidate"], ["B", "C"])
        self.assertEqual(j["steps"][1]["old"], ["S1"])

    def test_unallowed_confirmation_after_tau(self):
        """候选经静默后给出未允许确认 -> 不一致。"""
        old = make_spec("S0 S1 S2", "S0", "cmd",
                        "S0 -> S1 : ?cmd\nS1 -> S2 : !ok")
        cand = make_spec("A B C D", "A", "cmd",
                         "A -> B : ?cmd\nB -> C : tau\nC -> D : !confirm")
        j = check_ioco(old, cand).to_json()
        self.assertEqual(j["verdict"], "inconsistent")
        self.assertEqual(j["history"], ["?cmd", "!confirm"])
        self.assertEqual(j["oldOutputs"], ["!ok"])

    def test_candidate_output_allowed_consistent(self):
        old = make_spec("S0 S1 S2", "S0", "cmd",
                        "S0 -> S1 : ?cmd\nS1 -> S2 : !alarm")
        cand = make_spec("X Y Z", "X", "cmd",
                         "X -> Y : ?cmd\nY -> Z : !alarm")
        self.assertTrue(check_ioco(old, cand).consistent)

    def test_both_quiescent_consistent(self):
        old = make_spec("S0 S1", "S0", "cmd", "S0 -> S1 : ?cmd")
        cand = make_spec("A B", "A", "cmd", "A -> B : ?cmd")
        self.assertTrue(check_ioco(old, cand).consistent)

    def test_candidate_delta_where_old_outputs(self):
        """候选静止而旧规程仍有输出 -> 不一致，违规输出为 δ。"""
        old = make_spec("S0 S1", "S0", "cmd",
                        "S0 -> S1 : ?cmd\nS1 -> S1 : !beep")
        cand = make_spec("A B", "A", "cmd", "A -> B : ?cmd")
        j = check_ioco(old, cand).to_json()
        self.assertEqual(j["verdict"], "inconsistent")
        self.assertEqual(j["history"], ["?cmd", "δ"])
        self.assertEqual(j["offendingOutput"], "δ")

    def test_explicit_delta_matches_derived(self):
        """显式标记为静止的输出与派生静止行为相同 -> 一致。"""
        old = make_spec("S0 S1", "S0", "cmd", "S0 -> S1 : ?cmd")
        cand = make_spec("A B", "A", "cmd", "A -> B : ?cmd\nB -> B : delta")
        self.assertTrue(check_ioco(old, cand).consistent)

    def test_explicit_delta_successor(self):
        """显式静止迁移的目标位置在 δ 之后继续参与判定。"""
        old = make_spec("S0 S1 S2", "S0", "go",
                        "S0 -> S1 : ?go\nS1 -> S2 : delta\nS2 -> S2 : !ping")
        cand = make_spec("A B C", "A", "go",
                         "A -> B : ?go\nB -> C : delta\nC -> C : !ping")
        self.assertTrue(check_ioco(old, cand).consistent)

    def test_follow_old_outputs_to_deeper_violation(self):
        """沿旧规程允许的输出继续推进，发现更深处的违规。"""
        old = make_spec("S0 S1 S2", "S0", "cmd",
                        "S0 -> S1 : ?cmd\nS1 -> S2 : !ack")
        cand = make_spec("A B C", "A", "cmd",
                         "A -> B : ?cmd\nB -> C : !ack\nC -> C : !extra")
        j = check_ioco(old, cand).to_json()
        self.assertEqual(j["history"], ["?cmd", "!ack", "!extra"])

    def test_shortest_history_ascii_order(self):
        """同一分歧点的多个违规输出取 ASCII 最小者。"""
        old = make_spec("S0 S1", "S0", "cmd", "S0 -> S1 : ?cmd")
        cand = make_spec("A B", "A", "cmd",
                         "A -> B : ?cmd\nB -> B : !zz\nB -> B : !aa")
        j = check_ioco(old, cand).to_json()
        self.assertEqual(j["history"], ["?cmd", "!aa"])

    def test_shorter_violation_preferred(self):
        """存在更长的违规历史时，仍报告最短者。"""
        old = make_spec("S0 S1 S2", "S0", "a b",
                        "S0 -> S1 : ?a\nS1 -> S2 : ?b")
        cand = make_spec("A B C", "A", "a b",
                         "A -> B : ?a\nB -> C : ?b\nB -> B : !early\nC -> C : !late")
        j = check_ioco(old, cand).to_json()
        self.assertEqual(j["history"], ["?a", "!early"])

    def test_old_side_tau_closure(self):
        """旧规程的静默迁移同样纳入闭包。"""
        old = make_spec("S0 S1 S2", "S0", "cmd",
                        "S0 -> S1 : tau\nS1 -> S2 : !ready")
        cand = make_spec("A B", "A", "cmd", "A -> B : !ready")
        self.assertTrue(check_ioco(old, cand).consistent)

    def test_candidate_refuses_extra_input_still_consistent(self):
        """候选未实现旧规程的某个输入：对该历史不做约束（ioco 输入使能假设）。"""
        old = make_spec("S0 S1", "S0", "cmd reset",
                        "S0 -> S1 : ?cmd\nS1 -> S0 : ?reset")
        cand = make_spec("A B", "A", "cmd", "A -> B : ?cmd")
        self.assertTrue(check_ioco(old, cand).consistent)

    def test_nondeterministic_old_spec(self):
        """旧规程非确定：候选输出须落在旧规程允许集合内。"""
        old = make_spec("S0 S1 S2", "S0", "cmd",
                        "S0 -> S1 : ?cmd\nS0 -> S2 : ?cmd\nS1 -> S1 : !x\nS2 -> S2 : !y")
        good = make_spec("A B", "A", "cmd", "A -> B : ?cmd\nB -> B : !y")
        bad = make_spec("A B", "A", "cmd", "A -> B : ?cmd\nB -> B : !z")
        self.assertTrue(check_ioco(old, good).consistent)
        j = check_ioco(old, bad).to_json()
        self.assertEqual(j["history"], ["?cmd", "!z"])
        self.assertEqual(j["oldOutputs"], ["!x", "!y"])


class TestValidation(unittest.TestCase):
    def errors_of(self, raw):
        try:
            parse_spec(raw, "old")
        except SpecError as e:
            return e.errors
        self.fail("应当抛出 SpecError")

    def test_duplicate_locations(self):
        errs = self.errors_of({"locations": "S0 S1 S0", "initial": "S0",
                               "inputs": "", "transitions": ""})
        self.assertTrue(any("同一标签下重复状态声明" in e["message"] for e in errs))

    def test_missing_initial(self):
        errs = self.errors_of({"locations": "S0", "initial": "",
                               "inputs": "", "transitions": ""})
        self.assertTrue(any("缺少初始位置" in e["message"] for e in errs))

    def test_initial_not_declared(self):
        errs = self.errors_of({"locations": "S0", "initial": "S9",
                               "inputs": "", "transitions": ""})
        self.assertTrue(any("初始位置" in e["message"] for e in errs))

    def test_empty_locations(self):
        errs = self.errors_of({"locations": "", "initial": "S0",
                               "inputs": "", "transitions": ""})
        self.assertTrue(any("位置列表为空" in e["message"] for e in errs))

    def test_dangling_transition_located(self):
        errs = self.errors_of({"locations": "S0 S1", "initial": "S0",
                               "inputs": "cmd",
                               "transitions": "S0 -> S1 : ?cmd\nS1 -> S9 : ?cmd"})
        dangling = [e for e in errs if "悬空迁移" in e["message"]]
        self.assertEqual(len(dangling), 1)
        self.assertEqual(dangling[0]["line"], 2)
        self.assertEqual(dangling[0]["field"], "transitions")

    def test_illegal_labels(self):
        errs = self.errors_of({"locations": "S0 S1", "initial": "S0",
                               "inputs": "cmd",
                               "transitions": "S0 -> S1 : @@\nS0 -> S1 : ?nope\nS0 -> S1 : !"})
        msgs = [e["message"] for e in errs]
        self.assertTrue(any("非法标签" in m and "须为" in m for m in msgs))
        self.assertTrue(any("未在输入命令中声明" in m for m in msgs))
        self.assertTrue(any("输出名缺失" in m for m in msgs))
        self.assertTrue(any(e.get("line") == 1 for e in errs))
        self.assertTrue(any(e.get("line") == 3 for e in errs))

    def test_unparseable_line(self):
        errs = self.errors_of({"locations": "S0 S1", "initial": "S0",
                               "inputs": "cmd", "transitions": "S0 S1 ?cmd"})
        self.assertTrue(any("非法标签或写法" in e["message"] for e in errs))

    def test_reserved_names(self):
        errs = self.errors_of({"locations": "S0 tau", "initial": "S0",
                               "inputs": "", "transitions": ""})
        self.assertTrue(any("保留" in e["message"] for e in errs))

    def test_duplicate_inputs(self):
        errs = self.errors_of({"locations": "S0", "initial": "S0",
                               "inputs": "cmd cmd", "transitions": ""})
        self.assertTrue(any("重复声明的输入命令" in e["message"] for e in errs))

    def test_valid_spec_parses(self):
        spec = make_spec("S0 S1", "S0", "cmd", "S0 -> S1 : ?cmd\nS1 -> S1 : !ok\nS1 -> S0 : tau")
        self.assertEqual(spec.initial, "S0")
        self.assertEqual(len(spec.transitions), 3)


if __name__ == "__main__":
    unittest.main()
