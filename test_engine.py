import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import engine


def make_snapshot_base(root: str, model: str, with_model_bin: bool = True) -> str:
    """tmpdir 造 HF hub 快照布局: <root>/...<model>/snapshots/downloaded/model.bin。

    返回 snapshots 目录(即 resolve_model_path 的 base),供注入。
    """
    base = os.path.join(root, f"models--Systran--faster-whisper-{model}", "snapshots")
    downloaded = os.path.join(base, "downloaded")
    os.makedirs(downloaded)
    if with_model_bin:
        open(os.path.join(downloaded, "model.bin"), "wb").close()
    return base


class StubFactory:
    """记录构造调用的假 WhisperModel 工厂(A4/A5 用,不碰 faster_whisper)。"""

    def __init__(self, fail_on_device=None):
        self.calls = []
        self.fail_on_device = fail_on_device

    def __call__(self, path, device, compute_type):
        self.calls.append({"path": path, "device": device, "compute_type": compute_type})
        if device == self.fail_on_device:
            raise RuntimeError(f"simulated failure on device={device}")
        return f"model<{device}>"


class TestDefaultContract(unittest.TestCase):
    """A1 默认契约:不设环境变量 = HEAD 40f35bb 的历史行为(7700K 零配置不变)。"""

    def test_default_engine_is_cuda(self):
        self.assertEqual(engine.engine_name({}), "cuda")

    def test_default_model_is_large_v3(self):
        self.assertEqual(engine.model_name({}), "large-v3")

    def test_default_snapshots_base_equals_legacy_constant(self):
        # 与 HEAD voice-ptt.py 的 SNAPSHOTS_DIR 常量逐字相等(默认路径字节级不变)
        legacy = os.path.expanduser(
            "~/.cache/huggingface/hub/models--Systran--faster-whisper-large-v3/snapshots"
        )
        self.assertEqual(engine.snapshots_base("large-v3"), legacy)

    def test_valid_engines_tuple(self):
        self.assertEqual(engine.VALID_ENGINES, ("cuda", "cpu", "auto", "npu"))


class TestResolveModelPath(unittest.TestCase):
    """A2 路径泛化:按模型名找快照,找不到给可行动错误。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_finds_downloaded_layout(self):
        base = make_snapshot_base(self.root, "small")
        self.assertEqual(
            engine.resolve_model_path(model="small", base=base),
            os.path.join(base, "downloaded"),
        )

    def test_finds_hashed_layout(self):
        # huggingface_hub 哈希目录布局同样能找到
        base = os.path.join(self.root, "models--Systran--faster-whisper-large-v3", "snapshots")
        hashed = os.path.join(base, "edaa852e1234567890abcdef1234567890abcdef")
        os.makedirs(hashed)
        open(os.path.join(hashed, "model.bin"), "wb").close()
        self.assertEqual(
            engine.resolve_model_path(model="large-v3", base=base), hashed
        )

    def test_picks_first_sorted_dir_with_model_bin(self):
        # sorted 扫描:无 model.bin 的目录跳过,不误选
        base = os.path.join(self.root, "b", "snapshots")
        for name in ("aaa-empty", "zzz-empty"):
            os.makedirs(os.path.join(base, name))
        good = os.path.join(base, "mmm-model")
        os.makedirs(good)
        open(os.path.join(good, "model.bin"), "wb").close()
        self.assertEqual(
            engine.resolve_model_path(model="b", base=base), good
        )

    def test_missing_model_bin_raises_actionable_error(self):
        base = make_snapshot_base(self.root, "small", with_model_bin=False)
        with self.assertRaises(RuntimeError) as ctx:
            engine.resolve_model_path(model="small", base=base)
        msg = str(ctx.exception)
        self.assertIn("download-model.sh", msg)
        self.assertIn("small", msg)

    def test_nonexistent_base_raises_actionable_error(self):
        with self.assertRaises(RuntimeError) as ctx:
            engine.resolve_model_path(
                model="small", base=os.path.join(self.root, "no-such-dir")
            )
        self.assertIn("download-model.sh", str(ctx.exception))

    def test_defaults_read_env(self):
        # model=None 时从 os.environ 读 VOICE_INPUT_MODEL(mock 隔离,不污染进程)
        with mock.patch.dict(os.environ, {"VOICE_INPUT_MODEL": "small"}):
            with mock.patch.object(
                engine, "snapshots_base", return_value=os.path.join(self.root, "empty")
            ):
                with self.assertRaises(RuntimeError):
                    engine.resolve_model_path()  # base 不存在 → 抛,证明 env 生效


class TestEngineValidation(unittest.TestCase):
    """A3 校验:非法值 fail fast;npu 占位。"""

    def test_unknown_engine_raises_listing_valid_values(self):
        with self.assertRaises(ValueError) as ctx:
            engine.engine_name({"VOICE_INPUT_ENGINE": "gpu"})
        msg = str(ctx.exception)
        for v in engine.VALID_ENGINES:
            self.assertIn(v, msg)

    def test_engine_name_accepts_all_valid_values(self):
        for v in engine.VALID_ENGINES:
            self.assertEqual(
                engine.engine_name({"VOICE_INPUT_ENGINE": v}), v
            )

    def test_npu_build_raises_not_implemented(self):
        with self.assertRaises(NotImplementedError) as ctx:
            engine.build_model(engine="npu", model="x")
        self.assertIn("#19", str(ctx.exception))

    def test_build_model_direct_invalid_engine_raises(self):
        # 绕过 engine_name 显式传非法值 → 同样 fail fast
        with self.assertRaises(ValueError):
            engine.build_model(engine="tensor", model="x")


class TestBuildModelKwargs(unittest.TestCase):
    """A4 构造契约:各引擎的构造参数(含 cuda 逐字契约)。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = make_snapshot_base(self._tmp.name, "small")

    def tearDown(self):
        self._tmp.cleanup()

    def _build(self, engine_value):
        stub = StubFactory()
        with mock.patch.object(engine, "snapshots_base", return_value=self.base):
            result = engine.build_model(
                engine=engine_value, model="small", whisper_factory=stub
            )
        return stub, result

    def test_cuda_kwargs_are_verbatim_legacy(self):
        # #16 契约:与 HEAD load_model 原文逐字一致 —— device="cuda", compute_type="float16"
        stub, result = self._build("cuda")
        self.assertEqual(result, "model<cuda>")
        self.assertEqual(len(stub.calls), 1)
        self.assertEqual(
            stub.calls[0],
            {
                "path": os.path.join(self.base, "downloaded"),
                "device": "cuda",
                "compute_type": "float16",
            },
        )

    def test_cpu_kwargs(self):
        stub, result = self._build("cpu")
        self.assertEqual(result, "model<cpu>")
        self.assertEqual(
            stub.calls[0],
            {
                "path": os.path.join(self.base, "downloaded"),
                "device": "cpu",
                "compute_type": "int8",
            },
        )


class TestAutoFallback(unittest.TestCase):
    """A5 auto 降级:cuda 失败 → warn + cpu int8(仅显式选用)。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = make_snapshot_base(self._tmp.name, "small")

    def tearDown(self):
        self._tmp.cleanup()

    def test_cuda_failure_falls_back_to_cpu(self):
        stub = StubFactory(fail_on_device="cuda")
        warnings = []
        with mock.patch.object(engine, "snapshots_base", return_value=self.base):
            result = engine.build_model(
                engine="auto", model="small",
                whisper_factory=stub, warn=warnings.append,
            )
        self.assertEqual(result, "model<cpu>")  # 降级构造成功
        self.assertEqual(len(warnings), 1)      # warn 恰好一次
        self.assertIn("auto", warnings[0])
        self.assertIn("cuda", warnings[0])
        # 先 cuda(float16) 后 cpu(int8),顺序与参数都对
        self.assertEqual(
            [c["device"] for c in stub.calls], ["cuda", "cpu"]
        )
        self.assertEqual(stub.calls[1]["compute_type"], "int8")

    def test_cuda_success_no_fallback_no_warn(self):
        stub = StubFactory()
        warnings = []
        with mock.patch.object(engine, "snapshots_base", return_value=self.base):
            result = engine.build_model(
                engine="auto", model="small",
                whisper_factory=stub, warn=warnings.append,
            )
        self.assertEqual(result, "model<cuda>")
        self.assertEqual(warnings, [])
        self.assertEqual(len(stub.calls), 1)


class TestModulePurity(unittest.TestCase):
    """模块纯度:import engine 不得拉起 faster_whisper(无 GPU 机器可跑单测)。"""

    def test_import_does_not_pull_faster_whisper(self):
        # 子进程干净 import,避免本进程其它测试已导入造成误判
        code = (
            "import sys, engine; "
            "sys.exit(0 if 'faster_whisper' not in sys.modules else 1)"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            capture_output=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())


if __name__ == "__main__":
    unittest.main()
