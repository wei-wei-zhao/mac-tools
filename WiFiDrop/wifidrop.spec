# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置"""

import sys
from pathlib import Path

BASE = Path(SPECPATH)

block_cipher = None

# 模板、二维码图片等资源一起打进包里
datas = [
    (str(BASE / "templates"), "templates"),
]
qr = BASE / "手机扫码打开.png"
if qr.exists():
    datas.append((str(qr), "."))

# 更新分发：把 release 目录一起打包，App 自己就能当更新源
_release = BASE / "dist" / "release"
if _release.is_dir():
    datas.append((str(_release), "release"))

a = Analysis(
    [str(BASE / "app_main.py")],
    pathex=[str(BASE)],
    binaries=[],
    datas=datas,
    hiddenimports=[
        "server",
        "updater",
        "flask",
        "flask.json",
        "jinja2",
        "werkzeug",
        "qrcode",
        "qrcode.image",
        "qrcode.image.pil",
        "PIL",
        "PIL.Image",
        "PIL.ImageDraw",
        "rumps",
        "objc",
        "Foundation",
        "AppKit",
        "PyObjCTools",
        "PyObjCTools.AppHelper",
        "urllib.request",
        "zipfile",
        "tarfile",
        "hashlib",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "matplotlib", "numpy", "scipy", "pandas", "tkinter",
        "notebook", "IPython", "pytest", "setuptools",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="WiFiDrop",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # 不弹终端窗口
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(BASE / "app_icon.icns") if (BASE / "app_icon.icns").exists() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="WiFiDrop",
)

app = BUNDLE(
    coll,
    name="WiFiDrop.app",
    icon=str(BASE / "app_icon.icns") if (BASE / "app_icon.icns").exists() else None,
    bundle_identifier="com.tal.wifidrop",
    version="1.4.5",
    info_plist={
        "CFBundleName": "WiFiDrop",
        "CFBundleDisplayName": "WiFiDrop",
        "CFBundleShortVersionString": "1.4.5",
        "CFBundleVersion": "1.4.5",
        "LSUIElement": True,          # 菜单栏应用，不占 Dock
        "NSHighResolutionCapable": True,
        "NSHumanReadableCopyright": "WiFiDrop · 局域网互传",
        "LSMinimumSystemVersion": "10.15",
        "NSAppleEventsUsageDescription": "WiFiDrop 需要选择文件夹来设置保存位置",
    },
)
