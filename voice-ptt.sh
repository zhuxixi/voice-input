#!/bin/bash
# 语音输入守护进程 - 按住右 Command 录音，松开转写
VENV="/home/elling/.local/share/voice-input/venv"
export LD_LIBRARY_PATH="$VENV/lib/python3.12/site-packages/nvidia/cublas/lib:$VENV/lib/python3.12/site-packages/nvidia/cudnn/lib:$VENV/lib/python3.12/site-packages/nvidia/cuda_nvrtc/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
# 用系统 Python（有 gi/GTK），加 venv 的包路径
export PYTHONPATH="$VENV/lib/python3.12/site-packages${PYTHONPATH:+:$PYTHONPATH}"
exec /usr/bin/python3 /home/elling/.local/share/voice-input/voice-ptt.py
