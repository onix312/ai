# Luma Native UI 2.0 — Phase 1

Phase 1 turns the existing PySide6 shell into Luma's control surface and adds a
single emergency STOP ALL primitive shared by UI, tray and global hotkey.

## Identity

The native application, tray, chat sender label and Control Center now present
the assistant as **Люма**.

The Voice Orb keeps the existing runtime states but adds:

- Luma identity in the state label;
- animated pulse for listening / thinking / speaking;
- live partial transcript while listening;
- dedicated STOP ALL state.

## STOP ALL

STOP ALL is a backend latch, not UI-only behaviour.

When activated it:

1. stops current TTS;
2. disables the active microphone / always-on voice loop;
3. cancels every active model generation;
4. discards pending confirmation cards;
5. pauses running or waiting Task Engine tasks;
6. rejects new skill execution and new queued UI actions until Resume.

An already-running atomic provider call is not killed in the middle. The task
remains paused after that atomic step returns, preserving provider invariants.

Resume only releases the latch. It does **not** automatically restart paused
tasks or re-enable the microphone.

## Surfaces

STOP ALL is available from:

- Control Center settings;
- tray menu;
- global Windows hotkey: `Ctrl + Alt + Shift + Space`.

Quick Panel remains `Ctrl + Shift + Space`.

## API

- `GET /safety/status`
- `POST /safety/stop`
- `POST /safety/resume`

The normal `/status` payload also includes `safety`.

## Architecture invariant

STOP ALL does not introduce an execution bypass. Skills still use:

```text
Brain / Planner -> Task Engine -> Agent.run_skill
 -> validation -> autonomy -> confirmation policy -> Runner -> Provider
```

The safety latch only prevents the chain from starting another action.

## Next

- Native UI 2.0 Phase 2: richer Quick Panel and streaming conversation surface;
- dedicated voice diagnostics using Voice 3 telemetry;
- optional HQ local TTS provider.
