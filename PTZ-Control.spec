# -*- mode: python ; coding: utf-8 -*-
import os

# Icon path: same folder as this spec (ensures correct file is used)
_icon_path = os.path.join(SPECPATH, 'icon.ico.ico')

a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=[],
    datas=[('ui', 'ui')],
    hiddenimports=['mido.backends.rtmidi', 'rtmidi'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='PTZ-CONTROL 3.0',
    icon=_icon_path,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
