# Voice Engine 3.0 — Phase 5

Phase 5 introduces the assistant name **Люма** and strengthens wake-word barge-in.

## Identity

Default assistant name:

- display name: `Люма`;
- primary wake word: `люма`;
- latin alias: `Luma`.

The previous names `ноза`, `нозза`, `nozza`, `noza` remain temporary
legacy wake aliases so existing habits and configs do not break after update.

Environment overrides:

- `NOZZA_ASSISTANT_NAME`;
- `PRINTFLOW_WAKE_WORD`;
- `NOZZA_LEGACY_WAKE_WORDS`.

## Wake-word barge-in

Previously, while TTS was playing, Voice Runtime accepted only an explicit
stop phrase.

Now an explicit wake phrase such as:

```text
Люма, открой Telegram
```

can interrupt active TTS and immediately dispatch the new command.

## Echo rejection

To avoid reacting to its own speaker output, the local TTS layer stores only
the most recent spoken text and timestamp in RAM.

When speech is recognized during TTS, Voice Runtime compares the phrase against
the recent TTS text. A strong lexical overlap is treated as self-voice echo and
is ignored.

This enables wake-word interruption without allowing the assistant to trigger
itself from its own audio.

No audio is persisted.

## Voice 3 pipeline

```text
mic -> VAD/pre-roll -> ASR -> dynamic vocabulary
    -> wake/legacy wake
    -> echo rejection if TTS active
    -> Brain
```

## Next

- refine acoustic echo suppression beyond text-level rejection;
- optional HQ local TTS backend;
- Native UI 2.0 integration for the Luma identity and voice state.
