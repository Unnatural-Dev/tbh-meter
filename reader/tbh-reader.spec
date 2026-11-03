# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['meter_windows.py'],
    pathex=['.'],
    binaries=[],
    datas=[('config/level_curve.json', 'config'), ('config/skill_attr_map.json', 'config'), ('config/calib_seed.json', 'config')],
    hiddenimports=[],
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
    name='tbh-reader',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
