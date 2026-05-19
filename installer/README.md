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

The installer detects what is already on the PC and only offers missing components:

| Component | How it is detected | If already present |
|-----------|-------------------|-------------------|
| WebView2 | Registry version ≥ 96.0.1054.62 | Not shown |
| VC++ 2015–2022 x64 | Visual Studio runtime registry key | Not shown |
| FFmpeg | Bundled `tools\ffmpeg.exe` or `ffmpeg` on PATH | Not shown |
| Desktop shortcut | Always offered when the tasks page is shown | — |

If WebView2, VC++, and FFmpeg are all already satisfied, the **Additional components** page is skipped (desktop shortcut remains on by default).

Shown items are pre-selected; the user can uncheck optional items before installing.
