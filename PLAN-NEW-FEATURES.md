# Plan: New Features (6 items)

## Feature 1: Auto-reconnect to OBS

**Behavior**
- When status is "OBS disconnected", try to re-establish the WebSocket connection every **10 seconds** (only if auto-reconnect is enabled).
- **Toggle in OBS Settings**: "Auto-reconnect when disconnected". Persisted in obs.json; **default ON** first run.

**Where it touches**
- **Backend (`app.py`)**
  - `_default_obs_config()`: add `"auto_reconnect": True`.
  - `_load_obs_config()` / merge: ensure `auto_reconnect` is read and defaulted.
  - `ObsManager`: in the poll loop, when `not self._connected` and `self._config.get("auto_reconnect", True)`, every 10th tick (e.g. counter or last attempt time) call `_reconnect()`. Avoid hammering: only attempt every 10s, not every 250ms.
- **UI (`ptz-top-half.html`)**
  - OBS Settings panel: add a checkbox row "Auto-reconnect when disconnected", bound to `config.auto_reconnect`; include in save/load.

**Notes**
- Reconnect uses existing `_reconnect()` (same host/port/password from config). No new API.

---

## Feature 2: Move OBS Settings button

**Behavior**
- Move the "⚙ OBS Settings" button so it sits **to the left of the Camera 2 Edit button** and looks part of the layout (not floating).

**Where it touches**
- **UI only**
  - Remove the fixed-position wrapper around the OBS Settings button.
  - In the **Camera 2** panel header (`.hdr`), add the OBS Settings button **before** the Edit button. Order: `[Camera 2 Controls] [OBS Settings] [Edit] [IP dot]`.
  - Adjust CSS if needed (e.g. no `position: fixed`).

**Notes**
- Single, small HTML/CSS change.

---

## Feature 3: Rename camera title in edit mode + Settings labels

**Behavior**
- **In edit mode**: user can change the panel title (e.g. "Camera 1 Controls" → "Wide", "Camera 2 Controls" → "Tight").
- **OBS Settings**: the labels for the two source inputs change from "Camera 1 Source" / "Camera 2 Source" to "{Camera 1 title} Source" / "{Camera 2 title} Source" (e.g. "Wide Source", "Tight Source"). If no custom title, keep "Camera 1 Source" / "Camera 2 Source".

**Where it touches**
- **Backend**
  - **Storage**: persist camera display titles. Options: (A) in `presets.json` under e.g. `camera_titles: { "cam1": "", "cam2": "" }`, or (B) in `obs.json`. Using **presets.json** (with `_default_state()` / `_load_state()`) keeps “display” state in one place; OBS Settings would then need to read these titles (e.g. API `get_camera_titles()` / `set_camera_title(cam, title)`).
  - Add `camera_titles` to default state and migration in `_load_state()`; add getter/setter API for the UI.
- **UI**
  - **Edit mode**: when edit is on, show an editable control for the panel title (e.g. input or contenteditable) and call API to save on change/blur.
  - **OBS Settings**: when rendering the two source rows, set the `<label>` text to `{cameraTitle || "Camera N"} Source` using the stored titles (from state or API).

**Notes**
- Camera titles are for display only; OBS source names (`source_names.cam1` / `cam2`) stay as-is for the WebSocket.

---

## Feature 4: Preset name in status bar when recalled

**Behavior**
- When a preset is recalled, the status bar for that camera shows **"{Preset label} Preset Recalled"** (e.g. "Drums Preset Recalled") instead of "Preset 2 recalled ✓".

**Where it touches**
- **UI only**
  - In `buildPresetUI`, the recall button already has the preset `label`. Pass that label into the recall flow (e.g. `presetRecall(cam, preset, label)`).
  - In `presetRecall`, after a successful recall, call `status(cam, label + " Preset Recalled")` (or `status(cam, label + " Preset Recalled ✓")`).

**Notes**
- No backend change required; labels are already available in the UI.

---

## Feature 5: Simple Mode (OBS Settings toggle)

**Behavior**
- **Toggle in OBS Settings**: "Simple Mode" (default: off).
- **When Simple Mode is ON**:
  - **Layout**: two panels side by side; each panel shows:
    - Panel title (from Feature 3 if present).
    - OBS bar (red/green/gray) below title.
    - **Only** the preset buttons (preset titles only; no Save/Rename, no Edit button).
  - **Hidden in Simple Mode**: tracking controls (Auto Tracking ON/OFF), Edit button, CUT buttons, PTZ pad, Zoom, **Speed slider** (no PTZ shown so speed not shown either).
  - **Central "Switch" button**: one button **between** the two camera panels that cycles which camera is live (cam1 → cam2 → cam1 → …). Same effect as CUT to the other camera.
  - Bars under each camera (live/not live) stay as they are.

**Where it touches**
- **Backend**
  - `obs.json`: add `"simple_mode": false`; load/save in OBS config. API already returns full config; UI can persist and read `simple_mode`.
- **UI**
  - Persist `simple_mode` in state from OBS config; add checkbox in OBS Settings.
  - **Layout**: use a class on `.top` (e.g. `simpleMode`) to switch layout:
    - In Simple Mode: hide tracking row, edit button, CUT, PTZ block, zoom, speed; show only preset list per panel; add a centered "Switch" button between the two panels (e.g. in the grid between `#cam1Panel` and `#cam2Panel`).
  - **Switch logic**: on click, call `obs_get_status` (or use cached status) to see which camera is currently program; then call `obs_cut(other_cam)`.

**Notes**
- "Preset titles only" means the preset recall buttons with their names (e.g. "Drums", "Pulpit"); no edit/save/rename UI in Simple Mode.

---

## Feature 6: Switching Hold Time (0–5 seconds)

**Behavior**
- **OBS Settings**: new row "Switching Hold Time" with a control (slider or number input) range **0–5 seconds**. **0 = disabled** (no lock).
- **When a CUT or Switch happens**: the camera that becomes **live** has **all its controls disabled** for the next **X seconds** (X = configured value). Purpose: avoid accidentally moving the live camera.
- **All controls** disabled for that camera: PTZ, Zoom, Stop, Speed slider, Preset recall buttons, tracking buttons. **UI**: gray out the buttons (disabled state + visual graying) so the user clearly sees they are locked.

**Where it touches**
- **Backend**
  - `obs.json`: add `"switching_hold_seconds": 0`; validate 0–5 in load/save.
  - No new API required if UI already gets full OBS config; otherwise expose in `get_obs_config` / `set_obs_config`.
- **UI**
  - OBS Settings: add "Switching Hold Time" (0–5), 0 = disabled.
  - After `obs_cut(cam)` or "Switch" (which triggers a cut): set a per-camera "locked until" timestamp (e.g. `lockControlsUntil[cam] = Date.now() + holdSeconds*1000`). In a short interval (e.g. same as OBS poll) or in the existing poll, check if current time &gt; `lockControlsUntil[cam]` and clear the lock.
  - While locked: disable PTZ, Zoom, Stop, Speed, Presets, and tracking for that camera; **gray out** the buttons (e.g. `disabled` + CSS for opacity/reduced contrast) so the user sees they are locked. Optional: show a small label like "Live – locked 3s" that counts down.

**Notes**
- Apply hold after **every** CUT and after **Switch** (Switch is a CUT to the other camera). The camera that **becomes** live is the one that gets locked.

---

## Implementation order (recommended)

| Order | Feature | Rationale |
|-------|---------|-----------|
| 1 | **2 – OBS Settings button position** | Quick UI-only change; unblocks a cleaner Settings layout. |
| 2 | **1 – Auto-reconnect** | Self-contained; adds config key + poll logic + one checkbox. |
| 3 | **4 – Preset name in status** | Small UI change; no config, no new APIs. |
| 4 | **3 – Camera title rename** | Adds storage + API + edit UI + Settings labels; used by Feature 5 titles. |
| 5 | **6 – Switching Hold Time** | Config + UI lock logic; independent of Simple Mode; apply to both CUT and Switch. |
| 6 | **5 – Simple Mode** | Largest: new layout, Switch button, show/hide many elements; uses camera titles and benefits from hold time already being there. |

---

## Config / storage summary

- **obs.json** (existing + new): `auto_reconnect`, `simple_mode`, `switching_hold_seconds` (0–5).
- **presets.json** (existing + new): `camera_titles: { "cam1": "", "cam2": "" }` (or equivalent keys).

---

## Decided (from user)

1. **Feature 6 – Hold time**: **All controls** disabled for the camera that is now live (PTZ, zoom, speed, presets, tracking). **UI**: gray out the buttons (e.g. disabled state + visual graying) so the user clearly sees they are locked.
2. **Feature 5 – Simple Mode**: PTZ is hidden in Simple Mode, so **speed is not shown either** (no speed slider in Simple Mode). Backend can keep last speed for when they return to normal mode.
3. **Feature 1 – Auto-reconnect**: **Persist** the toggle in `obs.json` so it remains between runs. **Default to ON** the first time the program is run (i.e. `"auto_reconnect": true` in default config and when key is missing).
