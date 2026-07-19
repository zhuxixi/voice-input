# 录音归档设计文档 — issue #1

- 日期: 2026-07-19
- 状态: 设计待审
- 关联: GitHub issue #1、`voice-ptt.py`、roadmap「自定义词汇」(评测数据地基)

## 1. 目标

把每次语音输入的**原始音频 + 转写结果**成对存档,用于:
1. 回溯(历史可重建)
2. 未来 A/B 评测识别增强效果(裸 Whisper vs +prompt vs +纠错)
3. 数据驱动挖误识别模式(从真实 raw 文本提取 corrections 表)

## 2. 非目标(YAGNI)

- ❌ 不做自动清理(个人用量小,全量保留)
- ❌ 不引入 SQLite(留作升级路径;jsonl 顺序扫描够用)
- ❌ 不做检索/统计 UI(评测脚本直接读 jsonl)

## 3. 存储结构

```
~/.local/share/voice-input/recordings/
  ├─ index.jsonl                     # 每行一条记录的元数据(append-only)
  └─ 2026-07-19_000153/              # 每条录音一目录,时间戳命名
       ├─ audio.wav                  # 原始录音(可回放重识别)
       ├─ raw.txt                    # Whisper 原始转写
       └─ final.txt                  # 最终输出(当前 = raw;未来接纠错后可能不同)
```

`index.jsonl` 每行一个 JSON 对象:

```json
{"ts":"2026-07-19T00:01:53","dir":"2026-07-19_000153","audio":"audio.wav","raw":"我用 zima 做审查","final":"我用 zima 做审查","enhanced":false}
```

字段:
- `ts`: ISO 时间戳
- `dir`: 录音目录名(相对 recordings/)
- `audio`: 音频文件名
- `raw`: Whisper 原始转写
- `final`: 最终输出(预留,当前 = raw)
- `enhanced`: 是否启用增强(当前固定 false,未来 A&B 接入后 true)

> `config_snapshot` 暂不存——现在没有可配置的增强;A&B 落地后再加,避免空字段。

## 4. 组件设计(archive.py 纯函数模块 + voice-ptt.py 接入)

### `archive_recording(wav_path, raw_text, final_text, archive_dir=ARCHIVE_DIR) -> str`

- 生成时间戳目录名(`YYYY-MM-DD_HHMMSS`,fs-safe),ISO 时间戳写 JSON;**原子** `os.makedirs`(catch `FileExistsError` 递增 `_<seq>` 后缀,避免 TOCTOU)
- **move** wav 到目录(而非复制,避免 /tmp 残留 + 省一次 IO)
- 写 `raw.txt`、`final.txt`
- 追加一行到 `index.jsonl`(append, `"a"`),`flush` + `fsync` 保落盘
- **原子性**:move + 两个写 + index 写包进 try;失败 `shutil.rmtree(rec_dir)` 回滚后 re-raise(不留孤儿目录)
- 返回归档目录路径(str);任何异常抛出(由调用方捕获)

### 模块拆分

`archive.py` 是**纯函数模块**(仅依赖标准库 `os`/`shutil`/`json`/`datetime`),无 GTK/pynput 副作用,可独立单元测试。`voice-ptt.py` 仅 `from archive import archive_recording, ARCHIVE_ENABLED` 接入。这样拆是因为 `voice-ptt.py` 顶部即 `import gi` / `from pynput import keyboard`,直接 import 会触发 GTK 初始化,测试环境(无 display)无法加载。

### 开关

```python
ARCHIVE_DIR = os.path.expanduser("~/.local/share/voice-input/recordings")
ARCHIVE_ENABLED = os.environ.get("VOICE_INPUT_ARCHIVE", "1") != "0"
```

默认开;`VOICE_INPUT_ARCHIVE=0` 关。可在 `voice-ptt.sh` 或 `.desktop` 里设。

## 5. 接入点(stop_recording 改造)

现有(第 125-134 行):
```python
try:
    m = load_model()
    segments, info = m.transcribe(WAVFILE, language="zh")
    text = "".join(s.text for s in segments).strip()
except Exception as e:
    print(f"[voice-input] Error: {e}", file=sys.stderr)
    text = ""
finally:
    if os.path.exists(WAVFILE):
        os.unlink(WAVFILE)
```

改造后:
```python
text = ""  # 预置:transcribe 抛 BaseException(KeyboardInterrupt)时 finally 不 NameError
try:
    m = load_model()
    segments, info = m.transcribe(WAVFILE, language="zh")
    text = "".join(s.text for s in segments).strip()
except Exception as e:
    print(f"[voice-input] Error: {e}", file=sys.stderr)
    text = ""
finally:
    # 归档(置于 finally 套件内,独立 try,失败不影响转写/粘贴)
    if ARCHIVE_ENABLED and text and os.path.exists(WAVFILE):
        try:
            archive_recording(WAVFILE, text, text)   # move wav + 写 raw/final/jsonl
        except Exception as ae:
            print(f"[voice-input] archive failed: {ae}", file=sys.stderr)
    if os.path.exists(WAVFILE):   # 异常路径或归档失败 → wav 还在 → 清理
        os.unlink(WAVFILE)
```

关键点:
- `text = ""` 预置:`transcribe` 若抛 `BaseException`(如 `KeyboardInterrupt`,不被 `except Exception` 捕获)时 `text` 已定义,finally 中 `if ... and text ...` 走 falsy 分支跳过归档,直接兜底 `unlink`,不 NameError
- 正常路径:`archive_recording` 把 wav **move** 走 → `WAVFILE` 不存在 → 跳过 unlink
- 异常路径(transcribe 失败或归档失败):wav 还在 → unlink(保持原"用完即清"语义,/tmp 不残留)
- 归档失败**绝不阻断**转写结果粘贴(独立 try + text 已就绪)

## 6. 错误处理与降级

| 故障 | 行为 |
|---|---|
| recordings/ 不可建/不可写 | archive 抛异常 → 捕获 → stderr 警告 → 转写正常粘贴 |
| 磁盘满 | 同上 |
| wav move 失败 | 同上;finally 兜底 unlink |
| index.jsonl 写失败 | 同上;但此时 wav 可能已 move(可接受,丢索引不丢音频) |

核心原则:**归档是增强,任何归档问题不得阻断原本的转写+粘贴**。

## 7. 测试

### 7.1 单元(新增 `test_archive.py`)

- `archive_recording` 用临时 wav → 生成目录含 audio.wav/raw.txt/final.txt
- index.jsonl 追加一行,JSON 可解析,字段齐全
- raw/final 内容正确
- 同一函数连续调用两次 → 两条 jsonl 行 + 两个目录
- 目录不可写 → 抛异常(由调用方捕获,不崩)

### 7.2 集成

- 实录一段(含一个专有名词),验证 `recordings/` 下出现目录 + index.jsonl 有行 + audio.wav 可播放
- `VOICE_INPUT_ARCHIVE=0` 启动 → 不归档,/tmp 仍清理
- 归档目录设只读 → 转写仍正常粘贴(stderr 有 archive failed)

## 8. 文件改动清单

| 文件 | 动作 |
|---|---|
| `archive.py` | 新建:纯函数归档模块(`archive_recording` + `ARCHIVE_DIR` / `ARCHIVE_ENABLED` 常量,标准库依赖,可独立单元测试) |
| `voice-ptt.py` | 改:接入归档(`from archive import archive_recording, ARCHIVE_ENABLED` + `stop_recording()` 调用) |
| `test_archive.py` | 新建:单元测试 |

## 9. 与 roadmap / A&B 的关系

- 归档(issue #1)= 评测数据采集层
- roadmap「自定义词汇」(= A&B initial_prompt + 纠错)= 被评测对象
- 先有归档,A&B 落地后即可对同一批 wav 做裸 vs 增强 A/B 对比;并从 raw 文本挖真实误识别 → corrections 表
- `enhanced` 字段 + `final` 字段为 A&B 预留:接入后 `final=apply_corrections(raw)`,`enhanced=true`
