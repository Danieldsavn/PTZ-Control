# -*- mode: python ; coding: utf-8 -*-
import os

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules

_icon_path = os.path.join(SPECPATH, 'icon.ico.ico')

_hidden = [
    'mido.backends.rtmidi',
    'rtmidi',
    '_cffi_backend',
    'cffi',
    'clr_loader',
    'clr_loader.ffi',
    'clr_loader.ffi.netfx',
    'clr_loader.netfx',
    'pythonnet',
    'clr',
    'webview.platforms.edgechromium',
]
_hidden += collect_submodules('clr_loader')

_binaries = collect_dynamic_libs('cffi')
_binaries += collect_dynamic_libs('clr_loader')

a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=_binaries,
    datas=[('ui', 'ui')],
    hiddenimports=_hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=['pyi_rth_pythonnet.py'],
    excludes=['webview.platforms.winforms', 'webview.platforms.mshtml'],
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
    name='PTZ-Control',
    icon=_icon_path,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[
        '_cffi_backend.cp312-win_amd64.pyd',
        'python312.dll',
        'ClrLoader.dll',
    ],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
