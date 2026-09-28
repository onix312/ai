# Voice Engine 3.0 — Phase 4

Phase 4 adds Dynamic Vocabulary for local speech recognition.

## Goal

NOZZA should reliably hear names that matter on this computer:

- applications and app aliases;
- learned commands and macros;
- list names, goals and habits;
- named memory subjects;
- printer, customer, product and project names surfaced by local PrintFlow context.

## Design

Vosk hard grammar is deliberately not used.

Hard grammar improves listed terms but can destroy open-ended dictation by forcing
unknown speech into the grammar. Instead Phase 4 applies a conservative
post-ASR bias:

1. collect a small local vocabulary;
2. normalize and deduplicate it;
3. after final ASR, compare only same-sized short words/phrases;
4. replace only edit-distance <= 1 matches.

So `Orka` may become `Orca`, and `Алфа` may become `Альфа`, while an
unrelated sentence remains untouched.

## Sources

Vocabulary is built locally and cached for 30 seconds from:

- NOZZA wake-word variants;
- `pc.APPS` names and aliases;
- assistant whitelist;
- macros;
- learned skill names;
- memory subjects;
- active list names;
- active goals;
- habits;
- selected human-readable PrintFlow context fields.

PrintFlow access remains loopback-only. Free-form notes and phone numbers are
not imported into ASR vocabulary.

## Runtime

The final recognition pipeline is now:

```text
audio
 -> VAD + pre-roll
 -> streaming/final ASR
 -> Dynamic Vocabulary bias
 -> wake/follow-up gate
 -> Brain
```

Partial text shown in the UI is still the recognizer's live hypothesis.
Vocabulary correction is applied to the finalized phrase before execution.

## Next Voice 3 step

- stronger wake-word detector;
- echo suppression / self-voice rejection improvements;
- optional higher quality local TTS backend.
