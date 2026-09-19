"""bench/npu-bench.py 纯函数层单测(#17 A2)。

文件名含连字符不能 `import bench.npu_bench`,用 importlib 按路径加载
(与其它 test_* 一致:stdlib unittest,无 pytest)。run_bench 是真集成
(真实模型+设备),不做 mock——mock openvino_genai 什么都证明不了(spec
可测性拆分),交给 Task 3/4 的实跑覆盖。
"""

import importlib.util
import os
import subprocess
import sys
import unittest

_REPO = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.join(_REPO, "bench", "npu-bench.py")


def load_bench_module():
    spec = importlib.util.spec_from_file_location("npu_bench", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bench = load_bench_module()


class TestParseArgs(unittest.TestCase):
    """A2:参数解析与 --device 归一化。"""

    BASE = ["--model-dir", "/m", "--wav", "/w"]

    def test_defaults(self):
        ns = bench.parse_args(self.BASE + ["--device", "cpu"])
        self.assertEqual(ns.runs, 3)
        self.assertEqual(ns.timeout_s, 120)
        self.assertIsNone(ns.max_new_tokens)

    def test_device_normalized_case_insensitive(self):
        for raw, want in (("cpu", "CPU"), ("CPU", "CPU"), ("npu", "NPU"), (" NPU ", "NPU")):
            ns = bench.parse_args(self.BASE + ["--device", raw])
            self.assertEqual(ns.device, want)

    def test_invalid_device_exits_2(self):
        with self.assertRaises(SystemExit) as ctx:
            bench.parse_args(self.BASE + ["--device", "gpu"])
        self.assertEqual(ctx.exception.code, 2)

    def test_missing_required_exits_2(self):
        with self.assertRaises(SystemExit):
            bench.parse_args(self.BASE)  # 缺 --device

    def test_npu_platform_default_none_and_parsed(self):
        base = ["--model-dir", "d", "--device", "npu", "--wav", "w.wav"]
        self.assertIsNone(bench.parse_args(base).npu_platform)
        ns = bench.parse_args(base + ["--npu-platform", "NPU4000"])
        self.assertEqual(ns.npu_platform, "NPU4000")


class TestSetNpuLibraryPath(unittest.TestCase):
    """A2:NPU 库路径前插(注入式,幂等,不动 os.environ)。"""

    def test_empty_env(self):
        env = {}
        got = bench.set_npu_library_path(env)
        self.assertEqual(got, bench.NPU_LIB_DIR)
        self.assertEqual(env["LD_LIBRARY_PATH"], bench.NPU_LIB_DIR)

    def test_prepends_and_keeps_old(self):
        env = {"LD_LIBRARY_PATH": "/opt/x"}
        got = bench.set_npu_library_path(env)
        self.assertEqual(got, f"{bench.NPU_LIB_DIR}:/opt/x")

    def test_idempotent_no_dup(self):
        env = {"LD_LIBRARY_PATH": "/a"}
        bench.set_npu_library_path(env)
        bench.set_npu_library_path(env)
        self.assertEqual(env["LD_LIBRARY_PATH"], f"{bench.NPU_LIB_DIR}:/a")

    def test_does_not_touch_os_environ(self):
        before = dict(os.environ)
        bench.set_npu_library_path({"LD_LIBRARY_PATH": "/tmp/x"})
        self.assertEqual(os.environ, before)


class TestFormatReport(unittest.TestCase):
    """A2:报告格式化——spec 契约 11 个字段全部出现。"""

    METRICS = {
        "device": "NPU",
        "model_dir": "/models/whisper-small-int8-ov",
        "wav": "/tmp/a6-trimmed.wav",
        "model_load_s": 4.567,
        "first_transcribe_s": 12.34,
        "warm_runs": 3,
        "warm_mean_s": 1.234,
        "warm_min_s": 1.1,
        "warm_max_s": 1.4,
        "text_first": "今天测试语音输入引擎重购",
        "any_run_timeout": False,
    }

    def test_all_fields_present(self):
        out = bench.format_report(self.METRICS)
        # 输出字段名:dict 键 warm_mean/min/max_s 按契约输出为 mean/min/max_s
        out_names = [k + "=" for k in self.METRICS]
        out_names += ["mean_s=", "min_s=", "max_s="]
        for wrong in ("warm_mean_s=", "warm_min_s=", "warm_max_s="):
            out_names.remove(wrong)  # 键名不直接出现在输出里,上面已补契约名
        for name in out_names:
            self.assertIn(name, out)

    def test_exact_lines(self):
        out = bench.format_report(self.METRICS)
        self.assertIn("model_load_s=4.567 first_transcribe_s=12.340", out)
        self.assertIn("warm_runs=3 mean_s=1.234 min_s=1.100 max_s=1.400", out)
        self.assertIn("text_first=今天测试语音输入引擎重购", out)
        self.assertIn("any_run_timeout=false", out)

    def test_timeout_true_rendered(self):
        m = dict(self.METRICS, any_run_timeout=True)
        self.assertIn("any_run_timeout=true", bench.format_report(m))


class TestPurity(unittest.TestCase):
    """A2 纯度契约:加载模块不拉起 openvino;--help 不触发集成路径。"""

    def test_module_import_does_not_pull_openvino(self):
        code = (
            "import importlib.util, sys; "
            "spec = importlib.util.spec_from_file_location('npu_bench', "
            f"{_SCRIPT!r}); "
            "mod = importlib.util.module_from_spec(spec); "
            "spec.loader.exec_module(mod); "
            "sys.exit(0 if ('openvino' not in sys.modules and "
            "'openvino_genai' not in sys.modules) else 1)"
        )
        proc = subprocess.run([sys.executable, "-c", code],
                              cwd=_REPO, capture_output=True)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())

    def test_help_exits_zero(self):
        proc = subprocess.run(
            [sys.executable, _SCRIPT, "--help"],
            cwd=_REPO, capture_output=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())

    def test_missing_wav_exits_2(self):
        proc = subprocess.run(
            [sys.executable, _SCRIPT, "--model-dir", "/m",
             "--device", "cpu", "--wav", "/no/such.wav"],
            cwd=_REPO, capture_output=True,
        )
        self.assertEqual(proc.returncode, 2)



class TestNeedsReexec(unittest.TestCase):
    """A2:re-exec 判定纯函数(进程内改 env 对 ld.so 无效的兜底机制)。"""

    def test_needs_when_missing(self):
        self.assertTrue(bench.needs_reexec_for_npu({}))
        self.assertTrue(bench.needs_reexec_for_npu({"LD_LIBRARY_PATH": "/usr/lib"}))

    def test_no_reexec_when_present(self):
        env = {"LD_LIBRARY_PATH": bench.NPU_LIB_DIR + ":/usr/lib"}
        self.assertFalse(bench.needs_reexec_for_npu(env))

    def test_no_reexec_loop_with_sentinel(self):
        self.assertFalse(bench.needs_reexec_for_npu({"_NPU_BENCH_REEXEC": "1"}))


if __name__ == "__main__":
    unittest.main()
