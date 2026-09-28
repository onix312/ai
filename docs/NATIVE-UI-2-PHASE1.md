# Native UI 2.0 — Phase 1

Phase 1 turns the existing PySide6 shell into a first-class Luma control surface.

## Luma identity

The native UI now uses the assistant name **Люма** in:

- application title;
- Control Center;
- Quick Panel prompts;
- local chat labels;
- tray status and tooltip;
- Voice Orb.

The legacy QSettings organization key remains unchanged internally so existing
window geometry/preferences are not lost after the rename.

## STOP ALL

The native UI now exposes a latched emergency stop:

- tray action;
- Control Center button;
- Windows global hotkey: `Ctrl + Alt + Shift + Space`.

STOP ALL is implemented in the Agent backend, not in the UI.

When activated it:

1. latches execution off;
2. stops current TTS;
3. disables active voice listening;
4. cancels active model turns;
5. discards pending confirmations;
6. pauses running/waiting Task Engine tasks.

An already executing atomic provider call is not force-killed in the middle.
After it finishes, its task remains paused before the next step.

While the latch is active, new skills and raw queued UI actions are rejected.

Releasing STOP ALL only removes the execution latch. Paused tasks stay paused
until explicitly resumed, and voice stays disabled until explicitly enabled.

API:

- `GET /safety/status`
- `POST /safety/stop`
- `POST /safety/resume`

## Voice Orb

The Orb now has clearer Luma state identity for:

- listening;
- thinking;
- speaking;
- error;
- STOP ALL.

Active states receive a lightweight pulse animation. Listening still shows the
live partial transcript and microphone activity bar.

## Hotkeys

- Quick Panel: `Ctrl + Shift + Space`
- STOP ALL: `Ctrl + Alt + Shift + Space`

The STOP ALL hotkey is deliberately harder to hit accidentally.

## Invariant

The UI never executes providers directly.

Normal actions continue through:

```text
Brain / Planner
 -> Task Engine
 -> Agent.run_skill
 -> validation
 -> autonomy
 -> confirmation policy
 -> Runner
 -> Provider
```

STOP ALL is a local execution latch around this architecture, not a second
execution path.
