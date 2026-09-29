# Luma HQ Local TTS

Luma now prefers a fully local Piper voice while keeping the existing Voice Engine 3 transport intact.

## Architecture

The public voice contract did not change:

- `SpeechQueue` still accepts sentence chunks in order.
- `pc.speak()` is still the single playback entry point.
- `pc.stop_speaking()` is still the barge-in cancellation hook.
- VoiceRuntime does not know or care which TTS backend is active.

Only the backend behind `pc.speak()` changed.

## Backend order

1. Piper HQ local TTS, when both a Piper executable and model are available.
2. Existing system fallback:
   - Windows: SAPI
   - macOS: `say`
   - Linux: `espeak-ng`, `espeak`, or `spd-say`

If Piper fails to synthesize or cannot play the generated WAV, the same sentence transparently falls back to the system voice.

## Configuration

Canonical variables:

- `LUMA_TTS_PIPER` — path to the Piper executable. If omitted, `piper` is searched on PATH.
- `LUMA_TTS_MODEL_PATH` — path to the local Piper `.onnx` voice model.
- `LUMA_TTS_SPEAKER` — optional speaker id for multi-speaker models.

Compatibility aliases remain available:

- `NOZZA_TTS_PIPER`
- `NOZZA_TTS_MODEL_PATH`
- `NOZZA_TTS_SPEAKER`

If no model path is configured, Luma also checks:

`models/tts/luma.onnx`

The model is intentionally not committed to git because voice checkpoints are large and machine-specific.

## Streaming and interruption

Sentence streaming remains sequential through `SpeechQueue`.

For Piper, the active subprocess is registered before synthesis starts. Therefore `stop_speaking()` can interrupt:

1. Piper synthesis itself.
2. WAV playback after synthesis.

Queued sentences are still discarded by `SpeechQueue.stop()`, preserving current barge-in semantics.

Temporary WAV files are removed after playback, cancellation, or synthesis failure.

## Playback

Generated WAV playback uses:

- Windows: PowerShell + `System.Media.SoundPlayer`
- macOS: `afplay`
- Linux: `paplay`, then `aplay`, then `ffplay`

## Diagnostics

`pc.tts_status()` reports:

- selected engine
- whether HQ local TTS is active
- model filename
- optional speaker id
- Piper/model readiness

This is deliberately lightweight and does not load the voice model.
