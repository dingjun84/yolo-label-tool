# -*- mode: python ; coding: utf-8 -*-

import sys

block_cipher = None

# 窗口截图后端按平台动态导入，PyInstaller 的静态分析可能漏掉，这里显式声明
hiddenimports = ['core.capture']
if sys.platform == 'darwin':
    hiddenimports += ['core.capture.macos', 'objc', 'Quartz', 'Foundation']
elif sys.platform.startswith('win'):
    hiddenimports += ['core.capture.windows']

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('resources/style.qss', 'resources'),
        ('resources/classes.txt', 'resources'),
    ],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='YOLOTxtMaker',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=True,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

if sys.platform == 'darwin':
    app = BUNDLE(
        exe,
        name='YOLOTxtMaker.app',
        icon=None,
        bundle_identifier='com.evilmordy.yolotxtmaker',
    )
