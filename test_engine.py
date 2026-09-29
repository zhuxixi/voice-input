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


class TestConstructionKwargs(unittest.TestCase):
    """构造参数单一定义点(#16 CR 发现 4/6):download-model.py 与 build_model 共用。"""

    def test_cuda_kwargs_verbatim(self):
        self.assertEqual(
            engine.construction_kwargs("cuda"),
            {"device": "cuda", "compute_type": "float16"},
        )

    def test_cpu_kwargs(self):
        self.assertEqual(
            engine.construction_kwargs("cpu"),
            {"device": "cpu", "compute_type": "int8"},
        )

    def test_auto_first_attempt_is_cuda(self):
        self.assertEqual(
            engine.construction_kwargs("auto"), engine.construction_kwargs("cuda")
        )

    def test_npu_raises_not_implemented(self):
        with self.assertRaises(NotImplementedError) as ctx:
            engine.construction_kwargs("npu")
        self.assertIn("#19", str(ctx.exception))

    def test_invalid_raises_value_error(self):
        with self.assertRaises(ValueError):
            engine.construction_kwargs("tpu")


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


class TestLibPaths(unittest.TestCase):
    """#19 A4: 库路径单点计算 / prepend 幂等 / has_library_paths 谓词 / child_env。"""

    SITE = "/venv/lib/python3.12/site-packages"
    LEGACY_CUDA = [
        f"{SITE}/nvidia/cublas/lib",
        f"{SITE}/nvidia/cudnn/lib",
        f"{SITE}/nvidia/cuda_nvrtc/lib",
    ]

    def test_lib_paths_cuda_byte_equal_legacy(self):
        self.assertEqual(
            engine.required_lib_paths("cuda", self.SITE), self.LEGACY_CUDA)

    def test_lib_paths_auto_same_as_cuda(self):
        self.assertEqual(
            engine.required_lib_paths("auto", self.SITE), self.LEGACY_CUDA)

    def test_lib_paths_cpu_empty(self):
        self.assertEqual(engine.required_lib_paths("cpu"), [])

    def test_lib_paths_npu(self):
        self.assertEqual(
            engine.required_lib_paths("npu"), ["/usr/lib/x86_64-linux-gnu"])

    def test_lib_paths_cuda_requires_site(self):
        with self.assertRaises(ValueError):
            engine.required_lib_paths("cuda")

    def test_lib_paths_invalid_engine(self):
        with self.assertRaises(ValueError):
            engine.required_lib_paths("tpu", self.SITE)

    def test_prepend_empty_env(self):
        env = {}
        got = engine.prepend_library_path(env, self.LEGACY_CUDA)
        self.assertEqual(got, ":".join(self.LEGACY_CUDA))
        self.assertEqual(env["LD_LIBRARY_PATH"], got)

    def test_prepend_appends_existing_byte_equal_legacy(self):
        # HEAD 历史输出: f"{p1}:{p2}:{p3}:{old}" —— 逐字节一致
        env = {"LD_LIBRARY_PATH": "/opt/keep"}
        got = engine.prepend_library_path(env, self.LEGACY_CUDA)
        self.assertEqual(got, ":".join(self.LEGACY_CUDA) + ":/opt/keep")

    def test_prepend_idempotent(self):
        env = {"LD_LIBRARY_PATH": "/a"}
        engine.prepend_library_path(env, [engine.NPU_LIB_DIR])
        engine.prepend_library_path(env, [engine.NPU_LIB_DIR])
        self.assertEqual(env["LD_LIBRARY_PATH"], f"{engine.NPU_LIB_DIR}:/a")

    def test_prepend_empty_paths_noop(self):
        env = {"LD_LIBRARY_PATH": "/keep"}
        self.assertEqual(engine.prepend_library_path(env, []), "/keep")

    def test_has_library_paths_exact_component_match(self):
        # 兄弟目录 /usr/lib/x86_64-linux-gnu-extras 不得误判为已含
        env = {"LD_LIBRARY_PATH": "/usr/lib/x86_64-linux-gnu-extras"}
        self.assertFalse(engine.has_library_paths(env, [engine.NPU_LIB_DIR]))
        env2 = {"LD_LIBRARY_PATH": f"/opt/x:{engine.NPU_LIB_DIR}"}
        self.assertTrue(engine.has_library_paths(env2, [engine.NPU_LIB_DIR]))

    def test_child_env_npu_injects(self):
        out = engine.child_env({"VOICE_INPUT_ENGINE": "npu", "X": "1"})
        self.assertEqual(out["LD_LIBRARY_PATH"], engine.NPU_LIB_DIR)
        self.assertEqual(out["X"], "1")

    def test_child_env_npu_idempotent_when_present(self):
        out = engine.child_env({
            "VOICE_INPUT_ENGINE": "npu",
            "LD_LIBRARY_PATH": engine.NPU_LIB_DIR,
        })
        self.assertEqual(out["LD_LIBRARY_PATH"], engine.NPU_LIB_DIR)

    def test_child_env_cpu_passthrough_copy(self):
        src = {"VOICE_INPUT_ENGINE": "cpu", "LD_LIBRARY_PATH": "/opt/x"}
        out = engine.child_env(src)
        self.assertEqual(out, src)
        self.assertIsNot(out, src)

    def test_child_env_invalid_engine_passthrough(self):
        src = {"VOICE_INPUT_ENGINE": "bogus"}
        self.assertEqual(engine.child_env(src), src)


class TestOvResolve(unittest.TestCase):
    """#19 A2: default_model 矩阵 / ov 布局谓词 / 分布局错误信息。"""

    def test_default_model_matrix(self):
        self.assertEqual(engine.default_model("npu"), "small-int8-ov")
        self.assertEqual(engine.default_model("cuda"), "large-v3")
        self.assertEqual(engine.default_model("cpu"), "large-v3")
        self.assertEqual(engine.default_model("auto"), "large-v3")

    def test_default_model_invalid_engine(self):
        with self.assertRaises(ValueError):
            engine.default_model("bogus")

    def test_ov_snapshots_base_value(self):
        self.assertEqual(
            engine.ov_snapshots_base("small-int8-ov"),
            os.path.expanduser(
                "~/.cache/huggingface/hub/"
                "models--OpenVINO--whisper-small-int8-ov/snapshots"),
        )

    def test_resolve_ov_layout_found(self):
        with tempfile.TemporaryDirectory() as root:
            snap = os.path.join(root, "snapshots", "abc123")
            os.makedirs(snap)
            open(os.path.join(snap, "openvino_encoder_model.xml"), "wb").close()
            got = engine.resolve_model_path("small-int8-ov", base=os.path.join(root, "snapshots"), layout="ov")
            self.assertEqual(got, snap)

    def test_resolve_ov_layout_missing_actionable_error(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(RuntimeError) as ctx:
                engine.resolve_model_path("small-int8-ov", base=root, layout="ov")
            self.assertIn("huggingface-cli download OpenVINO/whisper-small-int8-ov", str(ctx.exception))

    def test_resolve_ct2_error_mentions_download_model_sh(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(RuntimeError) as ctx:
                engine.resolve_model_path("large-v3", base=root)
            self.assertIn("./download-model.sh large-v3", str(ctx.exception))

    def test_resolve_ov_layout_ignores_ct2_only_snapshot(self):
        # ct2 布局快照(model.bin)不应被 ov 谓词误认
        with tempfile.TemporaryDirectory() as root:
            snap = os.path.join(root, "snapshots", "dl")
            os.makedirs(snap)
            open(os.path.join(snap, "model.bin"), "wb").close()
            with self.assertRaises(RuntimeError):
                engine.resolve_model_path("small-int8-ov", base=os.path.join(root, "snapshots"), layout="ov")

    def test_resolve_unknown_layout(self):
        with self.assertRaises(ValueError):
            engine.resolve_model_path("x", base="/tmp", layout="onnx")


if __name__ == "__main__":
    unittest.main()
