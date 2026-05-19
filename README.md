# PTZ-Control

Windows desktop app for worship operators: GoStream switcher control, dual PTZ cameras, stream keys, stills, and MIDI scene triggers.

## Requirements

- Python 3.12 (for development builds)
- See `requirements.txt`

## Build

```powershell
py -3.12 -m pip install -r requirements.txt
py -3.12 -m PyInstaller PTZ-Control.spec --noconfirm
copy version.json dist\
```

Output: `dist\PTZ-Control.exe` (version is read from `version.json` beside the exe)

## Updates

The app checks `update.json` on the [latest GitHub release](https://github.com/Danieldsavn/PTZ-Control/releases/latest). Operator config files next to the exe (`switcher.json`, `presets.json`, `window.json`) are preserved across updates.

## License

Private / all rights reserved unless otherwise noted by the author.
