# voice-input

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/platform-Linux%20%28X11%29-lightgrey.svg)](#requirements)
[![CUDA](https://img.shields.io/badge/engine-CUDA%20%2B%20faster--whisper-76b900.svg)](#model-notes)

**English** | [简体中文](README.zh-CN.md)

Push-to-talk voice input for Linux: hold a hotkey to record, release to transcribe
locally with [faster-whisper](https://github.com/SYSTRAN/faster-whisper) on your GPU,
and the text is typed into whatever window you were using. 100% offline — nothing
leaves your machine.

## Table of Contents

- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [Usage](#usage)
- [Configuration](#configuration)
- [How It Works](#how-it-works)
- [Project Layout](#project-layout)
- [Model Notes](#model-notes)
- [Resource Usage](#resource-usage)
- [Hardware Reference](#hardware-reference)
- [Roadmap](#roadmap)
- [Testing](#testing)
- [License](#license)

## Features

- **Push-to-talk**: hold the right Command key (Alt_R) to record; release to transcribe
- **Instant typing**: the transcript is pasted automatically into the window that was
  active when you started recording
- **On-screen overlay**: red "● REC" while recording, blue "DONE" when finished
- **GPU accelerated**: faster-whisper large-v3 on CUDA; the model stays resident in
  VRAM, so transcription after key release is nearly instantaneous
- **Chinese & mixed Chinese/English speech** (transcription is pinned to
  `language="zh"`)
- **Custom vocabulary (hotwords)**: bias recognition toward your jargon via a simple
  `terms.json`; a broken or missing file degrades gracefully and never blocks
  transcription
- **Auto media pause**: pauses playing media (Chrome and other MPRIS players) while
  you record so it doesn't leak into the mic, then resumes it afterwards — via system
  D-Bus, zero extra dependencies
- **Recording archive**: every recording (audio + transcript) is stored locally under
  `~/.local/share/voice-input/recordings/` for later review or building an eval set;
  can be disabled
- **Autostart**: works with GNOME autostart

## Requirements

- Linux with X11 (tested on Ubuntu 24.04)
- NVIDIA GPU + CUDA
- Python 3.12
- ALSA (`arecord`) and PipeWire (capture goes through the `default` source)
- `xdotool`, `xsel`
- GTK3 (`gi`) on the system Python
- faster-whisper + ctranslate2, pynput (installed in a venv)

## Installation

```bash
# 1. Clone
git clone https://github.com/zhuxixi/voice-input.git
cd voice-input

# 2. Create a venv and install dependencies
python3 -m venv venv
./venv/bin/pip install faster-whisper pynput nvidia-cublas-cu12 nvidia-cudnn-cu12 nvidia-cuda-nvrtc-cu12

# 3. Download the model
./download-model.sh
```

> **Important — hardcoded paths (for now)**: until #11 lands, the launcher scripts
> hardcode the original author's absolute install path. After cloning, search for
> `/home/` in the scripts `voice-ptt.sh`, `voice-toggle.sh`, `download-model.sh`,
> `test-mic.sh` and `voice-ptt.py` (the `VENV=` / `exec` lines) and replace it with
> the absolute path of your clone. This is tracked in #11 and will become
> unnecessary once scripts derive their location automatically.
>
> **Model snapshot path**: `download-model.sh` unpacks the model into
> `.../snapshots/downloaded`, but the code loads a specific snapshot-hash directory
> (`.../snapshots/edaa852e...`). After downloading, reconcile the two — e.g. rename
> the `downloaded` folder to the hashed name the code expects, or adjust `MODEL_PATH`
> in `voice-ptt.py` / `test-mic.sh` / `voice-toggle.sh`. (Also to be cleaned up by #11.)

## Usage

```bash
# Test your microphone first
./test-mic.sh

# Start (foreground; Ctrl+C to quit)
./voice-ptt.sh

# Or in the background
nohup ./voice-ptt.sh &
```

(`test-mic.sh` and `voice-toggle.sh` still record from the author's sound card via
`-D hw:3`; on other machines change that to `-D default`.)

On startup the model preloads (a few seconds), then you're ready: hold the **right
Command key** (Mac keyboards) — on PC keyboards this is **right Alt** — speak, and
release. The transcript is typed into the window you were using.

### Autostart (GNOME)

```bash
mkdir -p ~/.config/autostart
cat > ~/.config/autostart/voice-input.desktop << 'EOF'
[Desktop Entry]
Type=Application
Name=Voice Input
Comment=Push-to-talk voice input (hold right Command to record)
Exec=/path/to/voice-input/voice-ptt.sh
Hidden=false
NoDisplay=false
X-GNOME-Autostart-enabled=true
EOF
```

Adjust the `Exec` path to your actual install location.

## Configuration

All settings with their defaults and how to change them:

| Setting | Default | Effect | How to change |
|---|---|---|---|
| `VOICE_INPUT_ARCHIVE` | `1` (on) | Archive each recording (audio + transcript) to `~/.local/share/voice-input/recordings/` (one directory per recording + an `index.jsonl` index) | `VOICE_INPUT_ARCHIVE=0 ./voice-ptt.sh` |
| `VOICE_INPUT_PAUSE_MEDIA` | `1` (on) | Auto-pause MPRIS players (Chrome etc.) while recording, resume after release | `VOICE_INPUT_PAUSE_MEDIA=0 ./voice-ptt.sh` |
| `~/.config/voice-input/terms.json` | (none) | Custom vocabulary hotwords, see below | Edit the file |

### Custom vocabulary (hotwords)

`~/.config/voice-input/terms.json`:

```json
{
  "terms": ["Claude", "git", "diff", "pull request"],
  "hotwords": null
}
```

- **`terms`** — a list of words or phrases embedded into the Whisper `initial_prompt`
  (first 30 entries) to bias recognition toward your vocabulary (proper nouns,
  project names, technical terms).
- **`hotwords`** — `null`, a list, or a space-separated string forwarded to
  faster-whisper's `hotwords` parameter.
- **Robustness**: a missing, malformed, or non-UTF-8 `terms.json` never breaks
  transcription — the program degrades to plain transcription and logs a warning.

## How It Works

1. **Key press** — the active window is remembered, MPRIS media is paused, and
   `arecord` starts recording (16 kHz mono).
2. **Key release** — recording stops and the model transcribes (Chinese, large-v3
   float16 on CUDA).
3. **Typing** — the text goes to the clipboard (`xsel`) and is pasted into the
   remembered window (`xdotool key ctrl+shift+v`).
4. **Cleanup** — the recording and transcript are archived (if enabled) and media
   playback resumes.

### Audio capture

Recording uses the ALSA `default` device, which is served by PipeWire, so the
microphone is shared with other apps (e.g., the GNOME sound-settings level meter)
instead of being grabbed exclusively — direct hardware access (`hw:*`) caused EBUSY
failures when another app held the device (#6). To use a different microphone, set
your system default source:

```bash
pactl set-default-source <source-name>   # or: wpctl set-default <id>
```

Use `arecord -l` only to troubleshoot raw devices, not to pick the capture device.

## Project Layout

| File | Description |
|---|---|
| `voice-ptt.sh` | Launcher: sets CUDA library paths, runs with system Python + venv packages |
| `voice-ptt.py` | Main program: push-to-talk, GTK overlay, transcription, typing |
| `terms.py` | Hotwords: loads `terms.json`, builds `initial_prompt` + transcribe kwargs |
| `archive.py` | Archives each recording (audio + transcript) under `~/.local/share/voice-input/recordings/` with an `index.jsonl` index |
| `media_pause.py` | Pauses/resumes MPRIS media via D-Bus; zero extra dependencies |
| `voice-toggle.sh` | Alternative toggle mode: press once to start, press again to stop and type |
| `test-mic.sh` | Microphone test |
| `download-model.sh` / `download-model.py` | Model download (hf-mirror.com mirror + DoH DNS workaround for polluted DNS) |
| `test_terms.py` / `test_archive.py` / `test_media_pause.py` | Unit tests (stdlib `unittest`) |
| `docs/` | Design documents (Chinese; the `superpowers/` plans are English) |

## Model Notes

The model is **OpenAI Whisper large-v3**, converted to
[CTranslate2](https://github.com/OpenNMT/CTranslate2) format and maintained by
[Systran](https://github.com/SYSTRAN) — a French NLP company founded in 1968 — in the
[faster-whisper](https://github.com/SYSTRAN/faster-whisper) project.

- Trained on 680k hours of labeled audio, multilingual
- Transformer seq2seq (autoregressive)
- CTranslate2 inference: ~4x faster than openai/whisper, ~38% less memory
- Model size: 2.9 GB download; float16 on CUDA occupies ~3.9 GB VRAM resident

### Whisper model family

| Model | Parameters | VRAM | Relative speed | Accuracy |
|---|---|---|---|---|
| tiny | 39M | ~1 GB | ~10x | basic |
| base | 74M | ~1 GB | ~7x | fair |
| small | 244M | ~2 GB | ~4x | good |
| medium | 769M | ~5 GB | ~2x | very good |
| **large-v3** | **1550M** | **~10 GB** | **1x** | **best** |
| turbo | 809M | ~6 GB | ~8x | near large |

This project uses large-v3 + float16 (~3.9 GB VRAM in practice).

## Resource Usage

Measured on a dual RTX 2080 Ti (22.5 GB) machine:

| Component | Usage |
|---|---|
| Whisper model (resident) | ~3946 MB |
| Xorg + GNOME | ~558 MB |
| GPU 0 total | ~9286 MB / 22528 MB (41%) |

The model is preloaded and stays resident, so transcription after key release is
nearly instantaneous (no reload per utterance).

## Hardware Reference

The author's setup, for reference:

- **Microphone**: Maono PD200X USB dynamic microphone
  - Built-in ADC over USB: short analog path, resistant to mainboard EMI
  - Dynamic capsule naturally rejects ambient noise — well suited for desk voice input
  - Plug-and-play on Linux via ALSA UAC2
- Capture goes through the PipeWire default source (see [Audio capture](#audio-capture))

## Roadmap

- [x] **Custom vocabulary** — soft hotwords via `initial_prompt` (#4). Possible
  follow-up: post-processing correction for known misrecognitions.
- **Punctuation**: Whisper's Chinese punctuation is unstable. Options:
  [FunASR](https://github.com/modelscope/FunASR) ct-punc as a post-processing step,
  or SenseVoice-Small (punctuation built in, see below).
- **SenseVoice-Small upgrade** — [FunAudioLLM/SenseVoice-Small](https://github.com/FunAudioLLM/SenseVoice)
  (DAMO Academy) is the leading alternative:

  | | faster-whisper large-v3 (current) | SenseVoice-Small |
  |---|---|---|
  | Architecture | autoregressive | **non-autoregressive** (parallel) |
  | Parameters | 1550M | ~234M |
  | VRAM | ~3.9 GB | **~1.5 GB** |
  | Speed | ~1281 ms / 10 s | **~70 ms / 10 s (15x faster)** |
  | Chinese accuracy | good | **better** |
  | Punctuation | unstable | **built in** |
  | Model size | 2.9 GB | 827 MB |
  | Languages | 99+ | 50+ (focus: zh/ja/ko/yue/en) |

- **Windows support** (#9): light platform detection, swap the OS-specific outer layer.

## Testing

```bash
python3 -m unittest test_terms test_archive test_media_pause -v
```

The tests use only the standard library and don't touch the GPU.

## License

MIT — see [LICENSE](LICENSE).
