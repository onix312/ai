# Voice Engine 3.0 — Phase 2

Phase 2 extends barge-in from TTS to active model generation.

## Behaviour

When the voice session is thinking and the owner says a stop phrase such as
`стоп`, `хватит` or `замолчи`:

1. VoiceRuntime detects the stop phrase even while its handler is busy.
2. Brain cancels only the active `voice` session model turn.
3. The cancellable Ollama NDJSON response is closed from the stop thread.
4. The interrupted result is returned as `kind=cancelled`.
5. The cancelled user/model turn is not written to dialog history.
6. No cancelled reply is sent to TTS.
7. The voice conversation returns to `listening` for a follow-up command.

The HTTP `/voice/stop` route uses the same cancellation path and still stops
active TTS.

## Scope

Cancellation is session-scoped. Stopping voice generation does not cancel a
planner, UI chat, task or another conversation session.

## Model transport

Normal model calls keep the existing non-streaming request path.

Only calls with a cancellation token switch Ollama to `stream=true` and read
NDJSON chunks. This gives the stop thread a live response object it can close
instead of waiting for the complete generation.

## History invariant

A cancelled generation is ephemeral. Neither the interrupted user request nor
a partial assistant response is persisted. This prevents an abandoned turn
from poisoning follow-up context.

## Next Voice 3 step

- streaming TTS / sentence-level playback;
- dynamic vocabulary for project names, apps and people;
- stronger wake-word and echo suppression.
