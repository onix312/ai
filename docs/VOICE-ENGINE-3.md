# Voice Engine 3.0 — Phase 1

Цель этой фазы — сократить ощущаемую задержку и сделать голосовой контур наблюдаемым, не меняя правила исполнения команд.

## Что добавлено

- incremental ASR для Vosk через один живой `KaldiRecognizer` на фразу;
- live partial transcript в `/voice/status`;
- live `audio_level` для Native Voice Orb;
- RAM pre-roll перед срабатыванием VAD, чтобы начало wake-word не обрезалось;
- Voice Orb показывает partial transcript и уровень микрофона;
- faster-whisper остаётся fallback и распознаёт финализированную фразу целиком.

## Ключевой инвариант

Partial transcript никогда не отправляется в Brain и не может запустить skill.

Путь остаётся таким:

```text
microphone -> VAD -> partial ASR -> UI only
                     |
                     -> final ASR -> wake/follow-up gate -> Brain -> Agent.run_skill
```

Это позволяет UI реагировать мгновенно, но исключает выполнение команды по промежуточной гипотезе распознавателя.

## Pre-roll

Voice Runtime хранит несколько последних аудио-чанков только в RAM. При срабатывании VAD они добавляются в начало текущей фразы. На диск аудио не пишется.

Настройка:

- `NOZZA_VOICE_PREROLL_CHUNKS`, default: `3`;
- `NOZZA_VOICE_PARTIAL_MIN_CHARS`, default: `2`.

## Fallback

Если Vosk недоступен и активен faster-whisper, streaming session не создаётся. Voice Runtime сохраняет прежний путь: собирает фразу до паузы и вызывает `transcribe_wav()`.

## Следующая фаза Voice 3.0

После Phase 1:

1. interruption model generation, а не только TTS;
2. streaming TTS;
3. dynamic vocabulary для имён, проектов и приложений;
4. более сильный wake-word detector / echo suppression.
