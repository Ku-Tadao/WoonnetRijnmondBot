# WoonnetRijnmondBot.spec

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('chromedriver.exe', '.'),
        # NEW: Also bundle the assets folder
        ('assets', 'assets')
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

# MODIFIED: This is now the last block.
exe = EXE(
    pyz,
    a.scripts,
    # MODIFIED: Removed the empty list and exclude_binaries=True
    # This ensures necessary binaries are included in the single file.
    a.binaries,
    a.zipfiles,
    a.datas,
    name='WoonnetRijnmondBot',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='assets/icon.ico'
)

