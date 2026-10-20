"""Silence trimming for transcription (#32).

Whisper hallucinates on silence and amplifies repeated tokens across long
silences; feeding it only VAD-detected speech fixes both. Silero VAD ships with
faster-whisper (assets/silero_vad_v6.onnx), so this adds no dependency; numpy and
faster_whisper are imported lazily so the rest of the module stays stdlib-only.
"""

import contextlib
import os
import sys
import tempfile
import wave

DEFAULT_PAD_MS = 400
ENV_VAD = "VOICE_INPUT_VAD"
SAMPLE_RATE = 16000


class TrimUnavailable(Exception):
    """VAD cannot run on this input (unsupported format / missing runtime)."""


def span_bounds(spans, total_samples):
    """Clamp spans into [0, total_samples], merge overlaps, drop empties."""
    total = max(0, int(total_samples))
    bounds = []
    for start, end in sorted(spans, key=lambda span: (int(span[0]), int(span[1]))):
        begin = max(0, min(int(start), total))
        stop = max(0, min(int(end), total))
        if stop <= begin:
            continue
        if bounds and begin <= bounds[-1][1]:
            bounds[-1] = (bounds[-1][0], max(bounds[-1][1], stop))
        else:
            bounds.append((begin, stop))
    return bounds


def speech_spans(frames, sample_rate=SAMPLE_RATE, vad_options=None):
    """Silero VAD spans (sample indices) for 16-bit mono PCM frames."""
    import numpy as np
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    options = vad_options or VadOptions(speech_pad_ms=DEFAULT_PAD_MS,
                                        min_speech_duration_ms=0)
    timestamps = get_speech_timestamps(samples, options, sampling_rate=sample_rate)
    return [(int(ts["start"]), int(ts["end"])) for ts in timestamps]


def spans_from_wav(path):
    """Return (pcm_frames, spans) for a 16 kHz mono 16-bit wav."""
    with wave.open(path, "rb") as wav:
        if (wav.getframerate() != SAMPLE_RATE or wav.getnchannels() != 1
                or wav.getsampwidth() != 2):
            raise TrimUnavailable(
                "expected 16000Hz mono 16-bit, got "
                f"{wav.getframerate()}Hz {wav.getnchannels()}ch "
                f"{wav.getsampwidth() * 8}-bit")
        frames = wav.readframes(wav.getnframes())
    return frames, speech_spans(frames, SAMPLE_RATE)


def _write_trimmed(frames, bounds, sample_rate):
    fd, tmp_path = tempfile.mkstemp(prefix="voice-input-trim-", suffix=".wav")
    try:
        os.close(fd)
        with wave.open(tmp_path, "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(sample_rate)
            out.writeframes(b"".join(frames[start * 2:stop * 2]
                                     for start, stop in bounds))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return tmp_path


@contextlib.contextmanager
def trimmed_wav(path, *, detector=None, enabled=None, log=None):
    """Yield a speech-only wav path, the original path, or None (no speech).

    Degrades to the original path on any VAD/format/IO problem; never raises.
    """
    log = log or (lambda msg: print(msg, file=sys.stderr))
    if enabled is None:
        enabled = os.environ.get(ENV_VAD, "1").strip().lower() not in ("0", "false", "no")
    if not enabled:
        yield path
        return

    detector = detector or spans_from_wav
    try:
        frames, spans = detector(path)
        with wave.open(path, "rb") as wav:
            sample_rate = wav.getframerate()
    except TrimUnavailable as exc:
        log(f"[voice-input] vad: unavailable ({exc}), using original audio")
        yield path
        return
    except Exception as exc:
        log(f"[voice-input] vad: error ({type(exc).__name__}: {exc}), "
            "using original audio")
        yield path
        return

    total = len(frames) // 2
    bounds = span_bounds(spans, total)
    if not bounds:
        yield None
        return
    kept = sum(stop - start for start, stop in bounds)
    if kept >= total * 0.99:
        yield path
        return

    try:
        tmp_path = _write_trimmed(frames, bounds, sample_rate)
    except Exception as exc:
        log(f"[voice-input] vad: error ({type(exc).__name__}: {exc}), "
            "using original audio")
        yield path
        return

    log(f"[voice-input] vad: {total / sample_rate:.2f}s -> "
        f"{kept / sample_rate:.2f}s ({len(bounds)} span)")
    try:
        yield tmp_path
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
