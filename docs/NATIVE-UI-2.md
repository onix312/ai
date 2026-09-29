# Luma Native UI 2.0

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

## Phase 2 — Live activity surface

Phase 2 makes the UI show what Luma is doing right now without adding a second
execution path or persisting model drafts.

The Agent keeps a small RAM-only activity snapshot:

- current phase;
- current user phrase;
- safe streaming reply preview;
- current skill;
- current Task Engine id and step detail;
- a tiny recent activity tail.

The normal `GET /status` payload exposes this snapshot as `activity`.

### Streaming reply

Local Ollama generation was already streaming internally. Phase 2 reuses that
same stream to update the activity snapshot.

Only safe free-answer JSON with an empty `skill` may appear as an early reply
preview. Action replies are still hidden until the canonical executor returns,
so the UI cannot claim an action succeeded before it actually did.

Streaming fragments are runtime-only. They are not inserted into chat history,
memory, or the task journal.

### Skill and task activity

Before a canonical skill executes, the activity surface can show the skill name
and description.

Task Engine publishes the active task id and current step. Every task step still
executes through `Agent.run_skill`; activity reporting is observational only.

### UI surfaces

The Voice Orb now contains:

- Luma state;
- the phrase heard from the user;
- streaming reply preview;
- current skill/task;
- compact recent activity.

Control Center conversation view has a matching live status strip.

Additional Orb states:

- `working`;
- `waiting`.

### State lifecycle

`None` means "preserve the previous activity field" while an explicit empty
value clears it. A new conversation turn therefore cannot accidentally inherit
the previous streamed reply or skill.

Completed activity remains briefly readable and then collapses back to idle.

## Next

- Native UI 2.0 Phase 3: richer task timeline / activity journal;
- dedicated Voice 3 diagnostics panel;
- optional HQ local TTS provider.
