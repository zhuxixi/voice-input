# voice-input

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/platform-Linux%20%28X11%29-lightgrey.svg)](#依赖)
[![CUDA](https://img.shields.io/badge/engine-CUDA%20%2B%20faster--whisper-76b900.svg)](#关于模型)

[English](README.md) | **简体中文**

Linux 语音输入工具 — 按住快捷键录音，松开即转写并输入到当前窗口。

基于 [faster-whisper](https://github.com/SYSTRAN/faster-whisper)（CTranslate2）+ CUDA GPU 加速，**完全本地离线**转写，数据不出本机。

## 目录

- [功能](#功能)
- [依赖](#依赖)
- [安装](#安装)
- [使用](#使用)
- [配置](#配置)
- [工作原理](#工作原理)
- [文件说明](#文件说明)
- [关于模型](#关于模型)
- [资源占用](#资源占用)
- [硬件参考](#硬件参考)
- [Roadmap](#roadmap)
- [测试](#测试)
- [License](#license)

## 功能

- **按住录音**：按住右 Command（Alt_R）键开始录音，松开自动转写
- **即时上屏**：转写结果自动粘贴到录音时的活跃窗口
- **屏幕提示**：录音时显示红色 "● REC"，完成后显示蓝色 "DONE"
- **GPU 加速**：CUDA + faster-whisper large-v3，模型常驻显存，松手后转写几乎瞬时
- **中文及中英混合**：转写固定为 `language="zh"`，支持中文与中英混合口语
- **自定义词汇（热词）**：通过 `terms.json` 引导专有名词识别；配置损坏自动降级，不阻断转写
- **自动暂停媒体**：录音时自动暂停正在外放的音乐/视频（Chrome 等支持 MPRIS 的播放器），结束后自动恢复，零新依赖（走系统 D-Bus）
- **录音归档**：每次录音的音频 + 转写结果归档到本地 `~/.local/share/voice-input/recordings/`，供回溯与评测，可关闭
- **开机自启**：支持 GNOME autostart

## 依赖

- Linux X11（已测试 Ubuntu 24.04）
- NVIDIA GPU + CUDA
- Python 3.12
- ALSA（`arecord`）+ PipeWire（采集走系统默认音源）
- `xdotool`, `xsel`
- GTK3（`gi`，系统 Python）
- faster-whisper + ctranslate2、pynput（装在 venv）

## 安装

```bash
# 1. 克隆
git clone https://github.com/zhuxixi/voice-input.git
cd voice-input

# 2. 创建 venv 并安装依赖
python3 -m venv venv
./venv/bin/pip install faster-whisper pynput nvidia-cublas-cu12 nvidia-cudnn-cu12 nvidia-cuda-nvrtc-cu12

# 3. 下载模型
./download-model.sh
```

> 脚本运行时自动推导自身位置——克隆到任意目录即可运行。

## 使用

```bash
# 先测试麦克风
./test-mic.sh

# 前台启动（Ctrl+C 退出）
./voice-ptt.sh

# 后台运行
nohup ./voice-ptt.sh &
```

启动后先预加载模型（几秒），随后按住 **右 Command 键**（Mac 键盘；PC 键盘对应 **右 Alt 键**）说话，松开即转写并输入。

### 开机自启

```bash
mkdir -p ~/.config/autostart
cat > ~/.config/autostart/voice-input.desktop << 'EOF'
[Desktop Entry]
Type=Application
Name=Voice Input
Comment=按住右 Command 键录音，松开转写并输入
Exec=/path/to/voice-input/voice-ptt.sh
Hidden=false
NoDisplay=false
X-GNOME-Autostart-enabled=true
EOF
```

将 `Exec` 路径改为实际安装路径。

## 配置

所有配置项、默认值与修改方式：

| 配置 | 默认值 | 作用 | 修改方式 |
|---|---|---|---|
| `VOICE_INPUT_ARCHIVE` | `1`（开） | 每次录音归档（音频 + 转写文本）到 `~/.local/share/voice-input/recordings/`（每条一目录 + `index.jsonl` 索引） | `VOICE_INPUT_ARCHIVE=0 ./voice-ptt.sh` |
| `VOICE_INPUT_PAUSE_MEDIA` | `1`（开） | 录音时自动暂停 MPRIS 播放器（Chrome 等），松手后恢复 | `VOICE_INPUT_PAUSE_MEDIA=0 ./voice-ptt.sh` |
| `~/.config/voice-input/terms.json` | （无） | 自定义词汇热词，见下 | 编辑该文件 |

### 自定义词汇（热词）

`~/.config/voice-input/terms.json`：

```json
{
  "terms": ["Claude", "git", "diff", "pull request"],
  "hotwords": null
}
```

- **`terms`**：词表列表，拼入 Whisper `initial_prompt`（取前 30 条），引导识别你的专有名词（项目名、术语等）
- **`hotwords`**：`null`、列表或空格分隔字符串，透传给 faster-whisper 的 `hotwords` 参数
- **健壮性**：文件缺失、格式损坏或非 UTF-8 都不会中断转写——自动降级为无热词转写并打印告警

## 工作原理

1. **按下按键**：记住当前活跃窗口，暂停 MPRIS 媒体，`arecord` 开始录音（16 kHz 单声道）
2. **松开按键**：停止录音，恢复被暂停的媒体，模型转写（中文，large-v3 float16 CUDA）
3. **上屏**：文本写入剪贴板（`xsel`），粘贴回原窗口（`xdotool key ctrl+shift+v`）
4. **收尾**：归档录音与转写（如开启）

### 音频采集

录音走 ALSA `default` 设备（由 PipeWire 提供服务），麦克风与其他应用共享（如 GNOME 声音设置的电平表），而不是独占直访硬件——直访 `hw:*` 曾在别的程序占用设备时触发 EBUSY 静默失败（#6）。切换麦克风改系统默认音源即可：

```bash
pactl set-default-source <源名>   # 或：wpctl set-default <id>
```

`arecord -l` 仅用于排障查看原始设备，不再用于选择采集设备。

## 文件说明

| 文件 | 说明 |
|---|---|
| `voice-ptt.sh` | 启动脚本（设置 CUDA 库路径，系统 Python + venv 包） |
| `voice-ptt.py` | 主程序（按住录音、GTK 浮层提示、转写与上屏） |
| `terms.py` | 热词支持：加载 `terms.json`，构造 `initial_prompt` + transcribe 参数 |
| `archive.py` | 录音归档（音频 + 转写文本存到 `~/.local/share/voice-input/recordings/`，含 `index.jsonl` 索引） |
| `media_pause.py` | 录音时自动暂停/恢复 MPRIS 媒体（Chrome 等），走系统 D-Bus，零依赖 |
| `voice-toggle.sh` | 切换模式脚本（按一下开始，再按一下停止并输入） |
| `test-mic.sh` | 麦克风测试 |
| `download-model.sh` / `download-model.py` | 模型下载（hf-mirror.com 镜像 + DoH DNS 修复，绕过 DNS 污染） |
| `test_terms.py` / `test_archive.py` / `test_media_pause.py` | 单元测试（标准库 unittest） |
| `docs/` | 设计文档（以中文为主；较新的 `superpowers/` 计划为英文） |

## 关于模型

使用的是 **OpenAI Whisper large-v3** 模型，由 [Systran](https://github.com/SYSTRAN)（法国，1968 年成立的 NLP 公司）通过 [CTranslate2](https://github.com/OpenNMT/CTranslate2) 格式转换并维护（即 faster-whisper 项目）。

- **模型训练数据**：68 万小时标注音频，多语种
- **架构**：Transformer Seq2Seq（自回归）
- **推理引擎**：CTranslate2，比原版 openai/whisper 快 4 倍，显存省 38%
- **模型大小**：2.9 GB；float16 CUDA 实际常驻显存约 3.9 GB

### Whisper 模型家族

| 模型 | 参数量 | 显存 | 相对速度 | 精度 |
|---|---|---|---|---|
| tiny | 39M | ~1 GB | ~10x | 基础 |
| base | 74M | ~1 GB | ~7x | 一般 |
| small | 244M | ~2 GB | ~4x | 较好 |
| medium | 769M | ~5 GB | ~2x | 很好 |
| **large-v3** | **1550M** | **~10 GB** | **1x** | **最佳** |
| turbo | 809M | ~6 GB | ~8x | 接近 large |

本项目使用 large-v3 + float16（实际显存 ~3.9 GB）。

## 资源占用

在双 RTX 2080 Ti (22.5GB) 上的实测数据：

| 项目 | 占用 |
|---|---|
| Whisper 模型（常驻） | ~3946 MB |
| Xorg + GNOME | ~558 MB |
| 合计 GPU 0 | ~9286 MB / 22528 MB (41%) |

模型预加载常驻显存，每次按键松开后转写几乎瞬时完成（无需重新加载模型）。

## 硬件参考

作者的环境，供参考：

- **麦克风**：闪克 Maono PD200X USB 动圈麦克风
  - USB 接口内置 ADC，模拟路径短，抗主板 EMI 干扰
  - 动圈麦克风天然抑制环境噪声，适合桌面语音输入
  - Linux 下 ALSA UAC2 即插即用
- 采集走 PipeWire 默认音源（见[音频采集](#音频采集)）

## Roadmap

- [x] **自定义词汇**：initial_prompt 软热词已实现（#4）。后续可选：已知误识别的后处理纠错
- **标点符号**：Whisper 中文标点支持不稳定。可选方案：[FunASR](https://github.com/modelscope/FunASR) ct-punc 后处理，或 SenseVoice-Small（内置标点，见下）
- **SenseVoice-Small 升级**：[阿里达摩院 SenseVoice-Small](https://github.com/FunAudioLLM/SenseVoice) 是首选替代方案：

  | | faster-whisper large-v3 (当前) | SenseVoice-Small |
  |---|---|---|
  | 架构 | 自回归 | **非自回归**（并行） |
  | 参数量 | 1550M | ~234M |
  | 显存 | ~3.9 GB | **~1.5 GB** |
  | 推理速度 | ~1281ms/10s | **~70ms/10s (15x 更快)** |
  | 中文准确率 | 好 | **更好** |
  | 标点 | 不稳定 | **内置** |
  | 模型大小 | 2.9 GB | 827 MB |
  | 语言 | 99+ | 50+（中日韩粤英为主） |

- **Windows 适配**（#9）：轻量平台判断，换一套外围实现

## 测试

```bash
python3 -m unittest test_terms test_archive test_media_pause -v
```

测试仅用标准库，不依赖 GPU。

## License

MIT — 见 [LICENSE](LICENSE)。
