# -*- mode: python ; coding: utf-8 -*-
import sys
import os

# Detect python version for venv path
py_ver = f"python{sys.version_info.major}.{sys.version_info.minor}"

a = Analysis(
    ['../src-python/mainloop.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('./../src-python/models/overlay/fonts', 'fonts/'),
        ('./../src-python/models/translation/translation_settings/prompt', 'translation_settings/prompt/'),
        ('./../src-python/models/translation/translation_settings/languages', 'translation_settings/languages/'),
        (f'./../.venv/lib/{py_ver}/site-packages/zeroconf', 'zeroconf/'),
        (f'./../.venv/lib/{py_ver}/site-packages/openvr', 'openvr/'),
        (f'./../.venv/lib/{py_ver}/site-packages/faster_whisper', 'faster_whisper/'),
        (f'./../.venv/lib/{py_ver}/site-packages/hf_xet', 'hf_xet/')
        ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['pandas', 'matplotlib', 'PyQt5'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='VRCT-sidecar-x86_64-unknown-linux-gnu',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=[],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='bin',
)
