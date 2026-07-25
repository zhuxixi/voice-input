# voice-input

Linux 语音输入工具 — 按住快捷键录音，松开即转写并输入到当前窗口。

基于 [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (CTranslate2) + CUDA GPU 加速，实时语音转文字。

## 功能

- **按住录音**：按住右 Command（Alt_R）键开始录音，松开自动转写
- **即时上屏**：转写结果自动粘贴到当前活跃窗口
- **屏幕提示**：录音时显示红色 "● REC"，完成后显示蓝色 "DONE"
- **GPU 加速**：使用 CUDA + faster-whisper large-v3 模型，转写速度快
- **中英混合**：支持中文、英文及混合语音识别
- **自动暂停媒体**：录音时自动暂停正在外放的音乐/视频（Chrome 等支持 MPRIS 的播放器），结束后自动恢复
- **开机自启**：支持 GNOME autostart，登录即运行

## 依赖

- Linux (X11, 已测试 Ubuntu 24.04)
- NVIDIA GPU + CUDA
- Python 3.12
- ALSA (`arecord`)
- `xdotool`, `xsel`
- GTK3 (`gi`)
- faster-whisper + ctranslate2
- pynput

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

## 使用

```bash
# 测试麦克风
./test-mic.sh

# 启动语音输入
./voice-ptt.sh

# 后台运行
nohup ./voice-ptt.sh &
```

启动后按住 **右 Command 键**（Mac 键盘）录音，松开自动转写并输入。

### 录音归档

默认每次录音的音频 + 转写结果会归档到 `~/.local/share/voice-input/recordings/`（每条一目录 + `index.jsonl` 索引），供回溯与评测。关闭：`VOICE_INPUT_ARCHIVE=0 ./voice-ptt.sh`。

### 媒体自动暂停

录音开始时自动暂停正在外放的媒体（Chrome 等通过 MPRIS 暴露的播放器），避免外放声音被麦克风收进去干扰识别；录音结束（松手后）自动恢复播放。零新依赖（走系统 D-Bus）。关闭：`VOICE_INPUT_PAUSE_MEDIA=0 ./voice-ptt.sh`。

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

## 文件说明

| 文件 | 说明 |
|---|---|
| `voice-ptt.sh` | 启动脚本（设置 CUDA 环境变量） |
| `voice-ptt.py` | 主程序（按住录音模式，GTK 浮层提示） |
| `archive.py` | 录音归档（音频 + 转写文本存到 `~/.local/share/voice-input/recordings/`） |
| `media_pause.py` | 录音时自动暂停/恢复 MPRIS 媒体（Chrome 等），走系统 D-Bus，零依赖 |
| `voice-toggle.sh` | 切换模式脚本（按一下开始/停止） |
| `test-mic.sh` | 麦克风测试 |
| `download-model.sh` | 模型下载（hf-mirror.com，中国网络友好） |
| `download-model.py` | 模型下载（DoH DNS 修复，绕过 DNS 污染） |

## 关于模型

使用的是 **OpenAI Whisper large-v3** 模型，由 [Systran](https://github.com/SYSTRAN)（法国，1968 年成立的 NLP 公司）通过 [CTranslate2](https://github.com/OpenNMT/CTranslate2) 格式转换并维护（即 faster-whisper 项目）。

- **模型训练数据**：68 万小时标注音频，多语种
- **架构**：Transformer Seq2Seq（自回归）
- **推理引擎**：CTranslate2，比原版 openai/whisper 快 4 倍，显存省 38%
- **模型大小**：2.9 GB

### Whisper 模型家族

| 模型 | 参数量 | 显存 | 相对速度 | 精度 |
|---|---|---|---|---|
| tiny | 39M | ~1 GB | ~10x | 基础 |
| base | 74M | ~1 GB | ~7x | 一般 |
| small | 244M | ~2 GB | ~4x | 较好 |
| medium | 769M | ~5 GB | ~2x | 很好 |
| **large-v3** | **1550M** | **~10 GB** | **1x** | **最佳** |
| turbo | 809M | ~6 GB | ~8x | 接近 large |

当前使用 large-v3 + float16，实际显存占用约 3.9 GB。

## 资源占用

在双 RTX 2080 Ti (22.5GB) 上的实测数据：

| 项目 | 占用 |
|---|---|
| Whisper 模型（常驻） | ~3946 MB |
| Xorg + GNOME | ~558 MB |
| 合计 GPU 0 | ~9286 MB / 22528 MB (41%) |

模型预加载常驻显存，每次按键松开后转写几乎瞬时完成（无需重新加载模型）。

## 硬件

- **麦克风**：闪克 Maono PD200X USB 动圈麦克风
  - USB 接口内置 ADC，模拟路径短，抗主板 EMI 干扰
  - 动圈麦克风天然抑制环境噪声，适合桌面语音输入
  - Linux 下 ALSA UAC2 即插即用
- **声卡设备**：`hw:3`（USB 音频，ALSA 自动识别）

根据实际设备修改脚本中的 `-D hw:3` 参数。用 `arecord -l` 查看可用设备。

## Roadmap

以下为未来可能改进的方向，当前体验已经很好，按需推进。

### 标点符号

Whisper 对中文标点支持不稳定。可选方案：

- **FunASR ct-punc**：标点恢复模型，作为后处理步骤插入
- **SenseVoice-Small**：内置标点，无需额外模型（见下方升级方案）

### 自定义词汇

口语中的专有名词（Claude Code、diff、git、PR 等）识别不够准确。可选方案：

- **Whisper `initial_prompt`**：传入关键词提示，引导识别
- **SenseVoice hotword**：神经网路热词增强
- **FunASR 自定义词典**：导入专属词汇表

### SenseVoice-Small 升级

[阿里达摩院 SenseVoice-Small](https://github.com/FunAudioLLM/SenseVoice) 是首选替代方案：

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

切换条件：如果未来 Whisper 中文识别不够用，或者需要标点支持。

## License

MIT
