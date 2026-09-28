# Voice Engine 3.0 — Phase 6

Phase 6 adds a lightweight adaptive acoustic echo gate for Luma.

## Why not full AEC yet

The current local TTS backends (Windows SAPI, macOS `say`, Linux espeak/spd-say)
are launched as system processes and do not expose the exact PCM reference
stream needed for true adaptive echo cancellation.

Instead, Phase 6 reduces self-triggering at the microphone/VAD boundary without
adding a heavy audio stack.

## Adaptive echo gate

While Luma is speaking:

1. microphone peak level is sampled as before;
2. quiet speaker leakage updates an in-memory `echo_floor`;
3. the effective VAD threshold rises above that floor;
4. leakage below the adaptive threshold is suppressed;
5. suppressed speaker frames are not copied into ASR pre-roll;
6. a stronger owner speech spike can still cross the threshold and start ASR.

Human speech spikes are deliberately excluded from floor learning, so speaking
over Luma does not teach the gate to suppress the owner.

## Layered protection

Voice output now has three independent barriers:

```text
speaker leakage
 -> adaptive acoustic gate
 -> ASR
 -> recent-TTS text echo rejection
 -> wake-word barge-in
```

Stop phrases remain immediate.

## Telemetry

Voice status exposes:

- `echo_floor`
- `echo_threshold`
- `echo_suppressed`

The values are runtime-only and are useful for tuning different microphones,
speaker volumes and room acoustics.

## Settings

- `NOZZA_VOICE_ECHO_GATE_MULTIPLIER`, default `1.65`
- `NOZZA_VOICE_ECHO_GATE_MARGIN`, default `180`
- `NOZZA_VOICE_ECHO_FLOOR_ALPHA`, default `0.22`

The old `NOZZA_` environment prefix remains for compatibility even though the
assistant identity is now Luma.

## Next

A future true AEC backend can replace this gate when TTS provides a PCM
reference stream. Before that, the next high-value work is Native UI 2.0 and an
optional higher-quality local TTS provider.
