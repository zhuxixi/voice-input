"""Silence trimming for transcription (#32) with quiet-speech fallbacks (#43).

Whisper hallucinates on silence and amplifies repeated tokens across long
silences; feeding it only VAD-detected speech fixes both. Silero VAD ships with
faster-whisper (assets/silero_vad_v6.onnx), so this adds no dependency; numpy and
faster_whisper are imported lazily so the rest of the module stays stdlib-only.

#43 adds two things on top:
  * the decision threshold is configurable and defaults to 0.3, because 0.5 cut
    real speech whose per-frame probability landed in the 0.3-0.5 band;
  * when VAD reports "no speech" but the recording is louder than the noise
    floor, the detector runs once more at a much lower threshold. Whispered
    (breathy) speech scores <= 0.08 even at 0.15, so only a floor-gated retry
    can recover it without turning silence into fake speech.
"""

import array
import contextlib
import dataclasses
import math
import os
import sys
import tempfile
import wave

DEFAULT_PAD_MS = 400
DEFAULT_THRESHOLD = 0.3          # was 0.5 before #43 (see issue #43 research)
DEFAULT_MIN_SILENCE_MS = 2000
RETRY_THRESHOLD = 0.1            # #43 F2: only used behind the energy gate
ENERGY_FLOOR_DBFS = -58.0        # between the noise floor (-61) and whispers (-54)
LOUD_FRAME_MS = 20               # transient guard: a single click must not open the gate
MIN_LOUD_FRAMES = 5              # >=100 ms of above-floor audio to justify a retry
ENV_VAD = "VOICE_INPUT_VAD"
ENV_THRESHOLD = "VOICE_INPUT_VAD_THRESHOLD"
ENV_PAD_MS = "VOICE_INPUT_VAD_PAD_MS"
ENV_MIN_SILENCE_MS = "VOICE_INPUT_VAD_MIN_SILENCE_MS"
SAMPLE_RATE = 16000


class TrimUnavailable(Exception):
    """VAD cannot run on this input (unsupported format / missing runtime)."""


class _PlainOptions:
    """Stdlib stand-in for VadOptions.

    Used when faster-whisper is not importable (system-interpreter test runs) or
    when a test injects its own detector; keeps `vad_summary` readable without
    forcing a third-party import.
    """

    def __init__(self, threshold=DEFAULT_THRESHOLD, speech_pad_ms=DEFAULT_PAD_MS,
                 min_silence_duration_ms=DEFAULT_MIN_SILENCE_MS):
        self.threshold = threshold
        self.speech_pad_ms = speech_pad_ms
        self.min_silence_duration_ms = min_silence_duration_ms


def _resolve_options(vad_options, injected):
    """Explicit options win; injected detectors use the stdlib stand-in."""
    if vad_options is not None:
        return vad_options
    if injected:
        return _PlainOptions()
    try:
        return vad_options_from_env()
    except Exception:
        return _PlainOptions()


def loud_frame_count(frames, floor_dbfs=ENERGY_FLOOR_DBFS, frame_ms=LOUD_FRAME_MS,
                     sample_rate=SAMPLE_RATE):
    """How many `frame_ms` windows sit above `floor_dbfs` (stdlib only).

    A whole-file RMS is fooled by a single click; counting loud frames keeps the
    retry gate tied to sustained audio rather than one transient.
    """
    step = max(1, int(sample_rate * frame_ms / 1000)) * 2
    return sum(1 for offset in range(0, len(frames) - step + 1, step)
               if rms_dbfs(frames[offset:offset + step]) > floor_dbfs)


def _with_threshold(options, threshold):
    """Copy `options` with a new threshold, preserving every other knob."""
    try:
        return dataclasses.replace(options, threshold=threshold)
    except (TypeError, ValueError):
        return _PlainOptions(
            threshold=threshold,
            speech_pad_ms=getattr(options, "speech_pad_ms", DEFAULT_PAD_MS),
            min_silence_duration_ms=getattr(options, "min_silence_duration_ms",
                                            DEFAULT_MIN_SILENCE_MS))


def parse_number(raw, default, cast=float):
    """Env-ish value -> cast(raw); blank/garbage/negative -> default (never raises)."""
    try:
        value = cast(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


def vad_options_from_env(env=None):
    """Build VadOptions from the environment; bad values fall back to defaults.

    The VadOptions import is lazy: callers construct this inside `trimmed_wav`'s
    try block, so a missing faster-whisper still degrades instead of raising.
    """
    from faster_whisper.vad import VadOptions

    source = os.environ if env is None else env
    threshold = parse_number(source.get(ENV_THRESHOLD), DEFAULT_THRESHOLD)
    if not 0.0 < threshold <= 1.0:
        threshold = DEFAULT_THRESHOLD
    return VadOptions(
        threshold=threshold,
        speech_pad_ms=parse_number(source.get(ENV_PAD_MS), DEFAULT_PAD_MS, int),
        min_speech_duration_ms=0,
        min_silence_duration_ms=parse_number(
            source.get(ENV_MIN_SILENCE_MS), DEFAULT_MIN_SILENCE_MS, int),
    )


def default_vad_options():
    """VadOptions carrying this module's defaults (env ignored)."""
    return vad_options_from_env({})


def rms_dbfs(frames):
    """RMS level of 16-bit mono PCM in dBFS (stdlib only; -inf for silence)."""
    usable = len(frames) - (len(frames) % 2)
    if usable <= 0:
        return float("-inf")
    samples = array.array("h")
    samples.frombytes(frames[:usable])
    if sys.byteorder != "little":
        samples.byteswap()
    if not samples:
        return float("-inf")
    mean_square = sum(sample * sample for sample in samples) / len(samples)
    if mean_square <= 0:
        return float("-inf")
    return 10.0 * math.log10(mean_square / (32768.0 ** 2))


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


def vad_summary(bounds, total_samples, sample_rate=SAMPLE_RATE, options=None):
    """Coverage summary for `bounds` (#44). Pure: `options` is duck-typed, not imported."""
    total = max(0, int(total_samples))
    kept = sum(stop - start for start, stop in bounds)
    if sample_rate and sample_rate > 0:
        total_s = round(total / sample_rate, 2)
        kept_s = round(kept / sample_rate, 2)
    else:
        total_s = kept_s = 0.0
    return {
        "threshold": float(getattr(options, "threshold", DEFAULT_THRESHOLD)),
        "speech_pad_ms": int(getattr(options, "speech_pad_ms", DEFAULT_PAD_MS)),
        "total_s": total_s,
        "kept_s": kept_s,
        "kept_ratio": round(kept / total, 3) if total else 0.0,
        "spans": len(bounds),
    }


def speech_spans(frames, sample_rate=SAMPLE_RATE, *, vad_options=None):
    """Silero VAD spans (sample indices) for 16-bit mono PCM frames."""
    import numpy as np
    from faster_whisper.vad import get_speech_timestamps

    samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    options = vad_options or vad_options_from_env()
    timestamps = get_speech_timestamps(samples, options, sampling_rate=sample_rate)
    return [(int(ts["start"]), int(ts["end"])) for ts in timestamps]


def spans_from_wav(path, *, vad_options=None):
    """Return (pcm_frames, spans) for a 16 kHz mono 16-bit wav."""
    with wave.open(path, "rb") as wav:
        if (wav.getframerate() != SAMPLE_RATE or wav.getnchannels() != 1
                or wav.getsampwidth() != 2):
            raise TrimUnavailable(
                "expected 16000Hz mono 16-bit, got "
                f"{wav.getframerate()}Hz {wav.getnchannels()}ch "
                f"{wav.getsampwidth() * 8}-bit")
        frames = wav.readframes(wav.getnframes())
    return frames, speech_spans(frames, SAMPLE_RATE, vad_options=vad_options)


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
def trimmed_wav(path, *, detector=None, enabled=None, log=None, stats=None,
                vad_options=None):
    """Yield a speech-only wav path, the original path, or None (no speech).

    Degrades to the original path on any VAD/format/IO problem; never raises.
    `stats` (optional dict) is cleared then filled with the decision (#44):
    status = disabled | kept | no_speech | degraded (+ reason), plus the
    `vad_summary` fields whenever spans were obtained, and `retried` (#43) on
    the detection paths.

    `detector` is a fixed-options test seam returning (frames, spans); the
    energy-gated retry (which needs different options) only applies to the
    default detector path.
    """
    log = log or (lambda msg: print(msg, file=sys.stderr))
    if stats is not None:
        stats.clear()

    def record(**fields):
        if stats is not None:
            stats.update(fields)

    if enabled is None:
        enabled = os.environ.get(ENV_VAD, "1").strip().lower() not in ("0", "false", "no")
    if not enabled:
        record(status="disabled")
        yield path
        return

    injected = detector is not None
    try:
        options = _resolve_options(vad_options, injected)
        if injected:
            frames, spans = detector(path)
        else:
            frames, spans = spans_from_wav(path, vad_options=options)
        with wave.open(path, "rb") as wav:
            sample_rate = wav.getframerate()
    except TrimUnavailable as exc:
        record(status="degraded", reason="unavailable")
        log(f"[voice-input] vad: unavailable ({exc}), using original audio")
        yield path
        return
    except Exception as exc:
        record(status="degraded", reason="error")
        log(f"[voice-input] vad: error ({type(exc).__name__}: {exc}), "
            "using original audio")
        yield path
        return

    total = len(frames) // 2
    try:
        bounds = span_bounds(spans, total)
    except Exception as exc:
        record(status="degraded", reason="error")
        log(f"[voice-input] vad: error ({type(exc).__name__}: {exc}), "
            "using original audio")
        yield path
        return

    retried = False
    if not bounds and not injected and RETRY_THRESHOLD < options.threshold:
        # #43 F2: only retry when the recording carries sustained audio above the
        # noise floor, otherwise pure silence (or a stray click) would be handed
        # to the model as "speech".
        loud = loud_frame_count(frames)
        level = rms_dbfs(frames)
        if loud >= MIN_LOUD_FRAMES:
            try:
                original_threshold = options.threshold
                retry_options = _with_threshold(options, RETRY_THRESHOLD)
                _frames, retry_spans = spans_from_wav(path, vad_options=retry_options)
                retry_bounds = span_bounds(retry_spans, total)
            except Exception as exc:
                record(status="degraded", reason="error")
                log(f"[voice-input] vad: retry error ({type(exc).__name__}: {exc}), "
                    "using original audio")
                yield path
                return
            retried = True
            if retry_bounds:
                bounds, options = retry_bounds, retry_options
                kept = sum(stop - start for start, stop in bounds)
                log(f"[voice-input] vad: no speech at {original_threshold:.2f}, "
                    f"retry at {RETRY_THRESHOLD} -> "
                    f"{kept / sample_rate:.2f}s kept (rms {level:.1f} dBFS)")
            else:
                log(f"[voice-input] vad: no speech, retry at {RETRY_THRESHOLD} "
                    f"(rms {level:.1f} dBFS) -> still empty")
        else:
            log(f"[voice-input] vad: no speech ({loud} loud frames, rms "
                f"{level:.1f} dBFS), below retry gate "
                f"({MIN_LOUD_FRAMES} frames > {ENERGY_FLOOR_DBFS:.1f} dBFS)")

    summary = vad_summary(bounds, total, sample_rate, options)
    if not bounds:
        record(status="no_speech", retried=retried, **summary)
        yield None
        return

    kept = sum(stop - start for start, stop in bounds)
    if kept >= total * 0.99:
        record(status="kept", retried=retried, **summary)
        yield path
        return

    try:
        tmp_path = _write_trimmed(frames, bounds, sample_rate)
    except Exception as exc:
        record(status="degraded", reason="error")
        log(f"[voice-input] vad: error ({type(exc).__name__}: {exc}), "
            "using original audio")
        yield path
        return

    record(status="kept", retried=retried, **summary)
    log(f"[voice-input] vad: {total / sample_rate:.2f}s -> "
        f"{kept / sample_rate:.2f}s ({len(bounds)} span)")
    try:
        yield tmp_path
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

