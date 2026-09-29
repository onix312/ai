# Luma Speech Quality Layer 1.0

This layer improves how local Piper TTS reads assistant text without changing Voice Engine 3 transport.

## Position in the voice pipeline

`SpeechQueue -> pc.speak() -> tts_quality.prepare_tts_text() -> Piper`

Only the local Piper path is transformed. If Piper fails and Luma falls back to system TTS, the original text is used.

Therefore existing streaming, sentence ordering and barge-in behavior remain unchanged.

## Built-in pronunciation

The layer contains conservative pronunciation hints for common technical and workshop terms, including:

- AI, API, CPU, GPU, RAM
- SSD, HDD, USB
- UI, UX, HTTP, HTTPS, JSON, SQL
- Wi-Fi, GitHub, PySide6, Piper
- Bambu, Orca
- PLA, PETG, TPU, ABS
- P1S

## Normalization

Before Piper synthesis Luma also prepares:

- clock notation such as `8:30`
- small standalone numbers
- decimal numbers
- percentages
- temperatures
- common storage/time units
- HTTP/HTTPS URLs
- dashes, semicolons and noisy punctuation into more natural pauses

The transformations are intentionally conservative. The quality layer does not rewrite the semantic content of the response.

## Personal pronunciation dictionary

Default local file:

`~/.printflow/tts-pronunciations.json`

On Windows this resolves inside the user's home directory.

Example:

```json
{
  "MIKHAIL": "михаил",
  "Bambu": "бэмбу",
  "MyProject": "май проджект"
}
```

A custom path can be selected with:

`LUMA_TTS_PRONUNCIATIONS`

Custom entries override built-in pronunciations.

The file is read with mtime-aware caching, so changes become active without rebuilding the application.

## Diagnostics

`pc.tts_status()["quality"]` exposes:

- whether the layer is enabled
- built-in term count
- custom term count
- active pronunciation dictionary path

## Safety / fallback

The quality layer is pure text processing. It does not:

- open audio devices
- start processes
- change SpeechQueue
- change VoiceRuntime
- change wake-word handling
- change interruption behavior

If the quality layer produces an empty string for any reason, `pc.speak()` falls back to the original sentence.


## Control Center editor

Control Center → Settings → **Произношение** provides a small live editor for the
personal dictionary. Add a pair such as `Bambu → бэмбу`, or select a custom
rule and delete it. The next Piper sentence uses the updated dictionary; no
restart or rebuild is required.
