# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Windows. Build with: pyinstaller windows_build.spec

Kept ASCII-only so cmd.exe never has to deal with non-ANSI file names.
"""
from PyInstaller.utils.hooks import collect_all

datas = []
binaries = []
hiddenimports = ["pygetwindow", "PIL.ImageGrab"]
datas.append(("app_icon.png", "."))
datas.append(("app_icon.ico", "."))
for package in ("rapidocr", "onnxruntime"):
    collected_datas, collected_binaries, collected_hidden = collect_all(package)
    datas += collected_datas
    binaries += collected_binaries
    hiddenimports += collected_hidden

a = Analysis(
    ["wechat_signup_app.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
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
    [],
    exclude_binaries=True,
    name="WeChatSignupAssistant",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX off keeps the onnxruntime DLLs intact and avoids antivirus noise.
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="app_icon.ico",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="WeChatSignupAssistant",
)
