"""Prompt-echo guard around `model.transcribe` (#43).

The hotword list rides on Whisper's `initial_prompt`. When VAD hands the model
only a tiny fragment of speech, the decoder can start reading that prompt back
instead of transcribing, emitting the hotwords over and over (observed: nineteen
repeats of Zima/DeepSeek/GitHub/Kimi on 2026-10-24). Dropping the prompt is the
direct fix; if the echo persists without a prompt the fragment was too short to
decode, and an empty result beats dumping the word list into the user's text.

Stdlib-only and duck-typed: both the faster-whisper model and the NPU engine
adapter expose `transcribe(wav, language=..., initial_prompt=..., **kw)`.
"""

from terms import hotword_dominated, looks_like_prompt_echo

LANGUAGE = "zh"          # both call sites transcribe Chinese; multi-language is out of scope


def transcribe_guarded(model, src, *, prompt, extra_kw, terms, log):
    """Transcribe `src`, retrying without the prompt if the result is an echo.

    Returns `(text, mode)` with `mode` in {"none", "retried", "dropped"}.
    Exceptions from the model propagate untouched: a real transcription failure
    must not be swallowed by the guard.
    """
    text = _transcribe(model, src, prompt, extra_kw)
    if not prompt or not terms or not looks_like_prompt_echo(text, terms):
        return text, "none"
    log("[voice-input] echo guard: prompt echo detected, retrying without prompt")
    retried = _transcribe(model, src, None, _without_hotwords(extra_kw))
    if looks_like_prompt_echo(retried, terms):
        if hotword_dominated(retried, terms):
            log("[voice-input] echo guard: echo persisted without prompt, dropping output")
            return "", "dropped"
        # A real sentence that merely repeats a hotword: keep it rather than
        # deleting something the user actually said.
        log("[voice-input] echo guard: echo-like but text kept (not hotword-dominated)")
    return retried, "retried"


def _without_hotwords(extra_kw):
    """Retry drops every prompt-like condition, not just `initial_prompt`."""
    return {key: value for key, value in extra_kw.items() if key != "hotwords"}


def _transcribe(model, src, prompt, extra_kw):
    segments, _info = model.transcribe(src, language=LANGUAGE,
                                       initial_prompt=prompt, **extra_kw)
    return "".join(segment.text for segment in segments).strip()
