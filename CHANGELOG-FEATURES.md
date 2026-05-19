# Camera Control – Feature Changes Summary

Summary of feature changes and improvements for the client.

---

## OBS Integration

- **OBS connection** – Connect to OBS via WebSocket (host, port, optional password). Config saved in `obs.json` next to the app.
- **Camera source mapping** – Configure Camera 1 and Camera 2 source names in OBS (e.g. Cam1, Cam2) so the app knows which scene items to control.
- **Status bars** – Each camera panel shows a bar: **red** = live (Program), **green** = Preview (when Studio Mode is on), **gray** = not live. Bars update every ~300 ms.
- **CUT button** – One-tap switch: make the selected camera live and take the other off Program (per-camera CUT in normal mode).
- **OBS Settings panel** – Open via “⚙ OBS Settings” (left of Camera 2 Edit). Edit host, port, password, and Camera 1/2 source names; Save & Connect; refresh scene list. Current Program scene and source names shown for reference.
- **Studio Mode handling** – App checks if OBS Studio Mode is on before asking for the preview scene, so you don’t see a 506 error when Studio Mode is off. Red bar still works; green bar only when Studio Mode is on.

---

## Startup & Connection

- **Auto-connect on startup** – If `obs.json` has host and camera source names, the app connects to OBS as soon as it starts (no need to open OBS Settings first).
- **Auto-reconnect** – When OBS is disconnected, the app can try to reconnect every 10 seconds. Toggle: “Auto-reconnect when disconnected” in OBS Settings (saved in `obs.json`; default: **on** for first run).

---

## Camera Titles & Presets

- **Rename camera titles** – In Edit mode, the panel title (e.g. “Camera 1 Controls”) can be edited (e.g. to “Wide” or “Tight”). Stored in `presets.json`.
- **OBS Settings labels** – In OBS Settings, the source fields use the custom titles when set (e.g. “Wide Source:”, “Tight Source:” instead of “Camera 1 Source:”, “Camera 2 Source:”).
- **Preset name in status** – When a preset is recalled, the status message uses the preset’s label (e.g. “Drums Preset Recalled ✓” instead of “Preset 2 recalled ✓”).

---

## Simple Mode

- **Simple Mode toggle** – In OBS Settings, a toggle turns “Simple Mode” on or off. Change applies immediately (no need to press Save & Connect). Preference is saved.
- **Default off on start** – The app always starts with Simple Mode **off** and saves that, so the next launch is also in normal mode.
- **Layout in Simple Mode** – Only preset recall buttons and the live/preview bar per camera. No tracking controls, Edit, CUT, PTZ, Zoom, or Speed. One **Switch** control in the middle between the two panels.
- **Switch control** – In Simple Mode, a middle box shows “Switch camera” and a **“◀ Switch ▶”** button. One tap switches which camera is live (same effect as CUT to the other camera).
- **Equal panel width** – In Simple Mode, both camera panels keep equal width; the middle column is a fixed 90 px for the Switch box.
- **Edit mode when entering Simple Mode** – If either camera is in Edit mode when Simple Mode is turned on, Edit mode is turned off for both so no edit popups are left without a way to close them.

---

## Switching Hold Time

- **Hold time setting** – In OBS Settings, “Switching Hold Time” (0–5 seconds, 0 = disabled). After a **CUT** or **Switch**, the camera that is **now live** has all its controls (PTZ, zoom, speed, presets, tracking) **disabled for that many seconds** so the live camera isn’t moved by mistake.
- **Grayed-out controls** – Disabled controls are visually grayed out during the hold.
- **Single lock** – Only the camera that just became live is locked. If you cut or switch to the other camera before the hold ends, the previous camera’s lock is cleared immediately so both cameras are never locked at once.

---

## Window & UI

- **Frameless window** – The app window has no title bar so the camera controls sit closer to the camera previews above. The window can still be moved by dragging anywhere on the content.
- **Close instructions** – A short note at the bottom of the window (light gray) explains: “To close the app: press **Alt+F4** or right-click the taskbar icon and choose Close.”
- **Window position** – App can open in the bottom half of the primary monitor; position/size can be stored in `window.json` for next run.

---

## Performance & Polish

- **Faster OBS status updates** – Backend polls OBS every 250 ms and the UI every 300 ms so the red/green bar and CUT feedback feel responsive.
- **Less debug noise** – Removed temporary debug code and the 506 preview-scene error from the UI; Studio Mode is checked first so that request isn’t sent when Studio Mode is off.
- **Optimizations** – Backend and UI tweaks (e.g. cached DOM refs, single camera-ping interval, cached OBS config) for snappier behavior.

---

## Files Used by the App

- **obs.json** – OBS host, port, password, Camera 1/2 source names, auto-reconnect, switching hold time, Simple Mode.
- **presets.json** – Preset labels per camera, speed slider values, camera display titles.
- **window.json** – Optional window position and size.

---

*This list reflects feature changes made during development. For technical or support details, refer to the project or contact the developer.*
