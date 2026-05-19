# PTZ-Control Windows installer

## Prerequisites (bundled by Setup)

Place these files in `installer/deps/` before compiling (or run `download-deps.ps1`):

| File | Purpose |
|------|---------|
| `MicrosoftEdgeWebview2Setup.exe` | WebView2 Evergreen bootstrapper (required for UI) |
| `vc_redist.x64.exe` | Visual C++ 2015–2022 x64 redistributable |
| `ffmpeg.exe` | Optional — live camera previews |

```powershell
cd installer
.\download-deps.ps1
```

## Build

From the repo root (after PyInstaller builds `dist\PTZ-Control.exe` and `dist\PTZ-Control-Updater.exe`):

```powershell
& "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer\PTZ-Control.iss
```

Output: `dist\PTZ-Control-Setup.exe`

## User choices during install

On the **Additional components** step, users can approve or skip:

- WebView2 (required — warned if skipped)
- VC++ Redistributable (recommended — warned if skipped)
- FFmpeg (optional previews)
- Desktop shortcut
