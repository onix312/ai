# Voice Engine 3.0 — Phase 3

Phase 3 adds sentence-level streaming TTS for free-form voice answers.

## Behaviour

When the model is answering the voice session without executing a skill:

1. Ollama keeps streaming JSON as in Phase 2.
2. NOZZA inspects only the JSON `reply` field.
3. A sentence is released only after terminal punctuation or after the reply
   string closes.
4. Sentences enter a sequential `SpeechQueue`.
5. The next sentence never interrupts the previous sentence.
6. The final server reply is not spoken again when streaming already happened.

This reduces perceived latency without changing the final Brain payload.

## Safety around actions

Early TTS is allowed only when the streamed JSON already contains an empty
`skill` before `reply`.

An action reply such as:

```json
{"skill":"app.open","params":{"app":"telegram"},"reply":"Открываю Telegram."}
```

is never spoken early. The normal executor runs first, then the verified result
may be spoken by the existing voice path.

That prevents NOZZA from saying “готово” before an action actually succeeds.

## Cancellation

The Phase 2 cancellation token owns the active SpeechQueue as a closer.

A voice stop therefore:

- closes the model response;
- drops queued TTS sentences;
- stops the currently speaking sentence;
- prevents the cancelled answer from reaching history.

## Fallback

If there is no system TTS engine, SpeechQueue refuses streamed writes and
`voice_streamed_chars` stays zero. The existing final `pc.speak()` path
remains available when speech output is otherwise supported.

## Next Voice 3 step

- dynamic vocabulary for project names, applications and people;
- stronger wake-word / echo suppression;
- optional higher quality local TTS backend behind the same SpeechQueue API.
