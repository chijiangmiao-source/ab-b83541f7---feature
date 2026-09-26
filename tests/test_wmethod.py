import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ioco import parse_spec
from wmethod import GenError, generate_suite, replay

# 命令后唯一观察为 !p / !q 的两状态机（i 自循环于第二状态）
PQ_SPEC = ("A0 A1 B0 B1", "A0", "i",
           "A0 -> A1 : ?i\nA1 -> B0 : !p\nB0 -> B1 : ?i\nB1 -> B0 : !q")

# 静默闭包后静止（cmd 分支），!alarm 在 reset 分支允许
SUITE_SPEC = ("S0 S1 S2 S3 S4", "S0", "cmd reset",
              "S0 -> S1 : ?cmd\nS1 -> S2 : tau\nS0 -> S3 : ?reset\nS3 -> S4 : !alarm")


def gen(locations, initial, inputs, transitions, max_states):
    spec = parse_spec({"locations": locations, "initial": initial,
                       "inputs": inputs, "transitions": transitions}, "old")
    return generate_suite(spec, max_states)


def case_rounds(suite):
    """{用例编号: [(命令, 允许观察元组), ...]}"""
    return {c["id"]: [(r["cmd"], tuple(r["allowed"])) for r in c["rounds"]]
            for c in suite["cases"]}


def run_impl(suite, impl, start):
    """用确定实现 impl（{(状态, 命令): (观察, 后继)}）跑全部用例；
    返回首个失败 (用例编号, 轮次)，全部通过返回 None。"""
    for case in suite["cases"]:
        s = start
        for k, step in enumerate(case["rounds"], 1):
            obs, s = impl[(s, step["cmd"])]
            if obs not in step["allowed"]:
                return (case["id"], k)
    return None


class TestDeterminize(unittest.TestCase):
    def test_quiescence_after_tau_closure(self):
        """静默闭包后的静止：tau 闭包内派生 δ，用例观察为 δ。"""
        suite = gen("S0 S1 S2", "S0", "cmd", "S0 -> S1 : ?cmd\nS1 -> S2 : tau", 2)
        self.assertEqual(suite["stateCount"], 2)
        self.assertEqual(case_rounds(suite), {"TC-1": [("cmd", ("δ",))]})

    def test_command_requires_every_location_in_closure(self):
        """仅在每个闭包位置都允许时才发出命令：tau 源位置不允许 -> 不发出。"""
        suite = gen("S0 S1 S2", "S0", "cmd", "S0 -> S1 : tau\nS1 -> S2 : ?cmd", 3)
        self.assertEqual(suite["stateCount"], 1)
        self.assertEqual(suite["caseCount"], 0)
        self.assertIn("note", suite)

    def test_command_allowed_by_all_locations(self):
        """闭包内每个位置（含静默迁移源）都允许该命令 -> 正常发出。"""
        suite = gen("S0 S1 S2 S3", "S0", "cmd",
                    "S0 -> S1 : tau\nS0 -> S2 : ?cmd\nS1 -> S3 : ?cmd", 2)
        self.assertEqual(case_rounds(suite), {"TC-1": [("cmd", ("δ",))]})

    def test_nondeterministic_outputs_rejected(self):
        """同一命令后多个可观察结果：拒绝生成而不任选一条。"""
        with self.assertRaises(GenError) as ctx:
            gen("S0 S1 S2 S3", "S0", "cmd",
                "S0 -> S1 : ?cmd\nS1 -> S2 : !x\nS1 -> S3 : !y", 4)
        self.assertTrue(any("多个可观察结果" in e["message"]
                            for e in ctx.exception.errors))

    def test_nondeterministic_delta_and_output_rejected(self):
        """同一命令后静止与输出并存：拒绝生成。"""
        with self.assertRaises(GenError) as ctx:
            gen("S0 S1 S2 S3", "S0", "cmd",
                "S0 -> S1 : ?cmd\nS1 -> S2 : delta\nS1 -> S3 : !x", 4)
        self.assertTrue(any("多个可观察结果" in e["message"]
                            for e in ctx.exception.errors))

    def test_output_before_command_rejected(self):
        """命令轮次之外可能产生输出（状态不稳定）：拒绝生成。"""
        with self.assertRaises(GenError) as ctx:
            gen("S0 S1", "S0", "cmd", "S0 -> S1 : !ready", 2)
        self.assertTrue(any("命令轮次之外" in e["message"]
                            for e in ctx.exception.errors))

    def test_tau_loop_without_observation_rejected(self):
        """命令后陷入内部静默循环、无可观察结果：拒绝生成。"""
        with self.assertRaises(GenError) as ctx:
            gen("S0 S1 S2", "S0", "cmd",
                "S0 -> S1 : ?cmd\nS1 -> S2 : tau\nS2 -> S1 : tau", 3)
        self.assertTrue(any("不存在可观察结果" in e["message"]
                            for e in ctx.exception.errors))

    def test_bound_below_state_count_rejected(self):
        """上界小于确定化稳定位置数：拒绝生成。"""
        with self.assertRaises(GenError) as ctx:
            gen("S0 S1 S2", "S0", "cmd", "S0 -> S1 : ?cmd\nS1 -> S2 : tau", 1)
        self.assertTrue(any("上界" in e["message"] for e in ctx.exception.errors))

    def test_invalid_bound_rejected(self):
        for bad in (0, -1, "4", None, True):
            with self.assertRaises(GenError):
                gen("S0", "S0", "", "", bad)


class TestWMethod(unittest.TestCase):
    def test_state_transition_cover_and_suffixes(self):
        """套件由状态覆盖、迁移覆盖与区分后缀构成。"""
        suite = gen(*PQ_SPEC, 2)
        self.assertEqual(suite["stateCount"], 2)
        self.assertEqual(suite["distinguishing"], [["i"]])
        self.assertEqual(len(suite["transitions"]), 2)
        # 两条迁移均被唯一（最长）用例覆盖：q0 -i-> q1 -i-> q1 -i->
        seqs = {tuple(r[0] for r in rounds) for rounds in case_rounds(suite).values()}
        self.assertEqual(seqs, {("i", "i", "i")})
        obs_seq = [r[1] for r in case_rounds(suite)["TC-1"]]
        self.assertEqual(obs_seq, [("!p",), ("!q",), ("!q",)])

    def test_conformant_impl_passes_and_deviation_caught(self):
        """确定实现：行为一致方可通过全部用例；越界即被某用例捕获。"""
        suite = gen(*PQ_SPEC, 2)
        good = {("n0", "i"): ("!p", "n1"), ("n1", "i"): ("!q", "n1")}
        self.assertIsNone(run_impl(suite, good, "n0"))
        bad = {("n0", "i"): ("!p", "n1"), ("n1", "i"): ("!r", "n1")}
        self.assertEqual(run_impl(suite, bad, "n0"), ("TC-1", 2))

    def test_extra_state_within_bound_caught(self):
        """上界每多一个稳定位置，套件加深一层（非固定深度枚举）。"""
        suite4 = gen(*PQ_SPEC, 4)
        lengths = {len(rounds) for rounds in case_rounds(suite4).values()}
        self.assertEqual(lengths, {5})  # n + (m − n) + |W| = 2 + 2 + 1
        # 四状态实现：前三轮与规约一致，第四轮才分歧
        impl = {("n0", "i"): ("!p", "n1"), ("n1", "i"): ("!q", "n2"),
                ("n2", "i"): ("!q", "n3"), ("n3", "i"): ("!r", "n3")}
        self.assertEqual(run_impl(suite4, impl, "n0"), ("TC-1", 4))
        # m=3 的套件（最长 4 轮）同样捕获；m=2 的套件（最长 3 轮）够不到
        self.assertEqual(run_impl(gen(*PQ_SPEC, 3), impl, "n0"), ("TC-1", 4))
        suite2 = gen(*PQ_SPEC, 2)
        self.assertIsNone(run_impl(suite2, impl, "n0"))

    def test_distinguishing_suffix_needed(self):
        """两状态仅对命令 b 观察不同：W 含 (b,)，混淆状态的实现被捕获。"""
        suite = gen("S0 S1 S2 S3 S4 S5", "S0", "a b",
                    "S0 -> S1 : ?a\nS1 -> S2 : !x\n"
                    "S2 -> S3 : ?a\nS3 -> S2 : !x\n"
                    "S2 -> S4 : ?b\nS4 -> S0 : !y\n"
                    "S0 -> S5 : ?b\nS5 -> S0 : !x", 2)
        self.assertEqual(suite["distinguishing"], [["b"]])
        good = {("q0", "a"): ("!x", "q1"), ("q0", "b"): ("!x", "q0"),
                ("q1", "a"): ("!x", "q1"), ("q1", "b"): ("!y", "q0")}
        self.assertIsNone(run_impl(suite, good, "q0"))
        bad = dict(good)
        bad[("q1", "b")] = ("!x", "q0")
        self.assertIsNotNone(run_impl(suite, bad, "q0"))

    def test_stable_numbering_and_renaming(self):
        """相同规程两次生成编号一致；仅位置改名不影响用例内容。"""
        a = gen("S0 S1 S2", "S0", "cmd", "S0 -> S1 : ?cmd\nS1 -> S2 : tau", 2)
        b = gen("S0 S1 S2", "S0", "cmd", "S0 -> S1 : ?cmd\nS1 -> S2 : tau", 2)
        self.assertEqual(a["suiteId"], b["suiteId"])
        renamed = gen("X0 X1 X2", "X0", "cmd", "X0 -> X1 : ?cmd\nX1 -> X2 : tau", 2)
        self.assertEqual(case_rounds(a), case_rounds(renamed))

    def test_equivalent_states_merged(self):
        """行为等价的闭包合并为一个稳定位置。"""
        suite = gen(*SUITE_SPEC, 2)
        self.assertEqual(suite["stateCount"], 2)
        self.assertEqual(case_rounds(suite),
                         {"TC-1": [("cmd", ("δ",))],
                          "TC-2": [("reset", ("!alarm",))]})


class TestReplay(unittest.TestCase):
    def setUp(self):
        self.suite = gen(*SUITE_SPEC, 2)

    def test_pass_quiescent_after_silence(self):
        """静默闭包后的静止：记录与允许观察一致 -> 通过。"""
        res, err = replay(self.suite, "TC-1", [{"cmd": "?cmd", "obs": "δ"}])
        self.assertIsNone(err)
        self.assertEqual(res["verdict"], "pass")
        self.assertEqual(res["roundsChecked"], 1)

    def test_fail_alarm_after_silence(self):
        """应静止却报警：判越界并给出首个失败轮次。"""
        res, err = replay(self.suite, "TC-1", [{"cmd": "cmd", "obs": "!alarm"}])
        self.assertIsNone(err)
        self.assertEqual(res["verdict"], "fail")
        self.assertEqual(res["firstFailure"]["round"], 1)
        self.assertEqual(res["firstFailure"]["expected"], ["δ"])
        self.assertEqual(res["firstFailure"]["observed"], "!alarm")
        self.assertIn("静默后报警", res["firstFailure"]["message"])

    def test_second_case_pass(self):
        res, err = replay(self.suite, "TC-2", [{"cmd": "reset", "obs": "!alarm"}])
        self.assertIsNone(err)
        self.assertEqual(res["verdict"], "pass")

    def test_unknown_command_label_rejected(self):
        res, err = replay(self.suite, "TC-1", [{"cmd": "frobnicate", "obs": "δ"}])
        self.assertIsNone(res)
        self.assertTrue(any("未知标签" in e["message"] for e in err))

    def test_unknown_observation_label_rejected(self):
        res, err = replay(self.suite, "TC-1", [{"cmd": "cmd", "obs": "!frobnicate"}])
        self.assertIsNone(res)
        self.assertTrue(any("未知标签" in e["message"] for e in err))

    def test_input_after_termination_rejected(self):
        res, err = replay(self.suite, "TC-1",
                          [{"cmd": "cmd", "obs": "δ"}, {"cmd": "cmd", "obs": "δ"}])
        self.assertIsNone(res)
        self.assertTrue(any("终止用例后继续输入" in e["message"] for e in err))
        self.assertEqual(err[0]["line"], 2)

    def test_command_mismatch_rejected(self):
        res, err = replay(self.suite, "TC-1", [{"cmd": "reset", "obs": "δ"}])
        self.assertIsNone(res)
        self.assertTrue(any("与用例不符" in e["message"] for e in err))

    def test_unknown_case_rejected(self):
        res, err = replay(self.suite, "TC-9", [])
        self.assertIsNone(res)
        self.assertTrue(any("未知测试用例编号" in e["message"] for e in err))

    def test_incomplete_record(self):
        suite = gen(*PQ_SPEC, 2)  # 唯一用例共 3 轮
        res, err = replay(suite, "TC-1", [{"cmd": "i", "obs": "!p"}])
        self.assertIsNone(err)
        self.assertEqual(res["verdict"], "incomplete")
        self.assertEqual(res["roundsChecked"], 1)
        self.assertEqual(res["totalRounds"], 3)


if __name__ == "__main__":
    unittest.main()
