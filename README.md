# voice-input

Linux 语音输入工具 — 按住快捷键录音，松开即转写并输入到当前窗口。

基于 [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (CTranslate2) + CUDA GPU 加速，实时语音转文字。

## 功能

- **按住录音**：按住右 Command（Alt_R）键开始录音，松开自动转写
- **即时上屏**：转写结果自动粘贴到当前活跃窗口
- **屏幕提示**：录音时显示红色 "● REC"，完成后显示蓝色 "DONE"
- **GPU 加速**：使用 CUDA + faster-whisper large-v3 模型，转写速度快
- **中英混合**：支持中文、英文及混合语音识别

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
```

启动后按住 **右 Command 键**（Mac 键盘）录音，松开自动转写并输入。

## 文件说明

| 文件 | 说明 |
|---|---|
| `voice-ptt.sh` | 启动脚本（设置环境变量） |
| `voice-ptt.py` | 主程序（按住录音模式） |
| `voice-toggle.sh` | 切换模式脚本（按一下开始/停止） |
| `test-mic.sh` | 麦克风测试 |
| `download-model.sh` | 模型下载（hf-mirror.com） |
| `download-model.py` | 模型下载（DoH DNS 修复） |

## 硬件

- 麦克风：闪克 Maono PD200X USB 动圈麦克风
- 声卡设备：`hw:3`（USB 音频，ALS 自动识别）

根据实际设备修改脚本中的 `-D hw:3` 参数。

## License

MIT
