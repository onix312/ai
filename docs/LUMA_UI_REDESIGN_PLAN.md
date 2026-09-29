# Luma UI Redesign Roadmap

Target: bring the native PySide6 UI to the visual direction approved in the latest Luma concept:
dark glass panels, violet/cyan energy accents, a female Luma character presence,
soft holographic depth, and state-driven motion without turning the UI into a game HUD.

## 1. Visual foundation

### Palette
- Background: near-black navy / violet, never pure black.
- Primary accent: electric violet.
- Secondary accent: cyan-blue.
- Success: restrained mint.
- Warning: warm amber.
- Danger: saturated red only for STOP ALL / destructive states.
- Surfaces: translucent dark cards with subtle violet edge light.

### Materials
- 14-18 px rounded cards.
- 1 px low-contrast borders plus selective neon edge glow.
- Layered depth: background haze -> glass surface -> content -> active glow.
- Avoid gradients inside text and controls; gradients are reserved for atmospheric backgrounds,
  the orb, progress energy, and avatar halo.

### Typography
- Keep system-safe fonts for packaging.
- Strong visual hierarchy: 30-34 px page hero, 18-20 px section title, 14-16 px body.
- Numeric telemetry uses tabular digits where possible.

## 2. Global shell

### Sidebar
- Compact LUMA wordmark at the top.
- Animated online indicator.
- Navigation icons + labels.
- Selected item becomes a glowing violet capsule, not a flat row.
- Bottom zone: microphone state, local/offline badge, settings.

### Top status strip
- Current state: Idle / Listening / Thinking / Speaking / Acting.
- Active provider and local/offline state.
- Tiny live telemetry only when useful.
- STOP ALL remains always reachable but visually quiet until hovered or active.

### Ambient background
- Slow moving violet/cyan bloom behind the content.
- Very faint technical grid / particles.
- No constant high-frequency animation.

## 3. Luma identity / character

The assistant is female. The UI should use the approved female Luma visual language:
young futuristic AI, violet/cyan lighting, no male avatar and no user avatar.

Implementation stages:
1. Start with a stylized abstract silhouette / portrait container in the dashboard hero.
2. Keep avatar assets isolated from UI code so they can later be replaced by Live2D/video.
3. Character halo reacts to assistant state.
4. Speaking state can later drive subtle mouth/energy motion.

## 4. Motion system

All animations must be state-driven and stoppable.

### Idle
- 4-6 s slow orb breathing.
- Very subtle background drift.
- Avatar halo at low intensity.

### Listening
- Orb expands from microphone level.
- Cyan ring responds to VAD/audio level.
- Sidebar mic indicator pulses.
- 120-180 ms control transitions.

### Thinking
- Violet orbit particles rotate slowly.
- Thin scanning arc around the orb.
- Content cards can show a soft moving edge highlight.

### Speaking
- Waveform / radial amplitude motion.
- Avatar halo becomes brighter.
- Motion follows TTS activity, not a fake timer.

### Acting
- Short directional light sweep across the active task card.
- Action timeline highlights the current step.

### Error / STOP ALL
- Motion freezes.
- Red is used only as a focused state signal.
- Never flash continuously.

## 5. Screens

### A. Home / Today
Hero layout:
- Luma portrait/identity on the left.
- Large living voice orb in the center.
- Current status + short reply underneath.
- Right column: tasks, upcoming events, recent actions.
- Bottom row: quick actions.

This is the screen that should look closest to the approved concept.

### B. Conversation
- Large central conversation stream.
- Luma replies visually separated with violet glass.
- User messages stay neutral.
- Live transcript appears near the voice orb during listening.
- Streaming response grows naturally without card jumping.
- Speaking indicator tied to SpeechQueue.

### C. Tasks
- Kanban/timeline hybrid.
- Active task gets a neon progress spine.
- Steps: queued -> running -> waiting -> done.
- Replan is shown as a branching path, not another text dump.

### D. Activity
- Technical timeline of what Luma actually did.
- App icon / action / result / timestamp.
- Expandable details for diagnostics.
- Important failures visible without exposing noisy logs by default.

### E. Memory
- Search-first layout.
- Memory cards grouped by topic/time.
- Confidence/source metadata is secondary.
- Add a compact visual memory graph later, not in phase 1.

### F. Skills / Integrations
- Capability cards with live availability.
- Local/cloud/provider badges.
- Health state and last successful use.
- Search and category filters.

### G. Voice
Dedicated voice panel instead of burying TTS in generic settings:
- Hero: "Luma Voice".
- Primary profile: Silero v5_5_ru / baya / 48 kHz.
- Play preview button.
- Engine chain: Silero -> Piper -> System.
- Latency, model readiness, local/offline badge.
- Pronunciation dictionary.
- VAD / echo controls moved into an Advanced section.

### H. Settings
- Appearance.
- Voice.
- Persona.
- Autonomy.
- Providers.
- Hotkeys.
- Diagnostics.

Keep advanced technical settings collapsed by default.

## 6. Components to build

1. LumaTheme token layer.
2. GlassCard.
3. NeonButton / DangerButton.
4. StatusPill.
5. LumaOrb v2.
6. AnimatedSidebarItem.
7. MetricChip.
8. TimelineItem.
9. VoiceProfileCard.
10. LumaHero / avatar container.
11. Toast / transient notification layer.
12. Skeleton loading states.

## 7. PySide6 implementation strategy

- Centralize colors, spacing, radii and durations in one theme module.
- Move giant inline styles out of control_center.py.
- Prefer QPropertyAnimation and QVariantAnimation for cheap native motion.
- Use custom paintEvent only for the orb, halo and ambient canvas.
- Cap animation repaint rate and pause hidden widgets.
- Respect reduced-motion setting.
- Avoid GPU-heavy blur on every card; fake glass with layered surfaces where needed.
- Keep backend contracts unchanged during the visual migration.

## 8. Delivery phases

### Phase 1: Design system + shell
- Theme tokens.
- Main window background.
- Sidebar.
- Header/status.
- Glass cards.
- Base animation helpers.

### Phase 2: Voice identity
- Orb v2.
- Listening/thinking/speaking state animations.
- Dedicated Voice page.
- Baya status + preview.
- Avatar/halo container.

### Phase 3: Home + Chat
- New home/dashboard.
- Conversation redesign.
- Streaming transcript/reply states.
- Quick actions.

### Phase 4: Tasks + Activity
- Task timeline.
- Active step animation.
- Replan visualization.
- Activity cards.

### Phase 5: Memory + Skills + Settings
- Memory search/cards.
- Capability cards.
- Clean advanced settings.
- Provider health.

### Phase 6: Polish
- Motion timing pass.
- DPI scaling.
- 100/125/150/200% Windows scaling checks.
- Keyboard navigation.
- Reduced motion.
- Startup/empty/error states.
- Performance profiling.

## 9. Acceptance criteria

- Luma is unmistakably a female AI assistant.
- Main screen visually matches the approved violet/cyan concept direction.
- Idle UI stays calm; motion becomes expressive only when Luma is active.
- Listening, thinking, speaking and acting states are distinguishable without text.
- No regression in tray, quick panel, hotkeys, SpeechQueue, barge-in or STOP ALL.
- UI remains usable when the avatar asset is missing.
- 60 fps is not required everywhere, but animations must remain smooth on a normal Windows PC.
- Core controls remain readable at 125-150% DPI.
