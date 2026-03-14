# rider.spec — PyInstaller spec for ReDrive Rider
# Build:  pyinstaller rider.spec

from PyInstaller.building.api import PYZ, EXE
from PyInstaller.building.build_main import Analysis

block_cipher = None

a = Analysis(
    ['rider_app.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[
        'aiohttp',
        'aiohttp.connector',
        'aiohttp.client_ws',
        'aiohttp.http_websocket',
        'aiohttp.streams',
        'asyncio',
        'tkinter',
        'tkinter.scrolledtext',
        'tkinter.ttk',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['redrive', 'tkinter.test'],
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
    name='ReDriveRider',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,        # no console window — GUI only
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # icon='assets/icon.ico',   # uncomment and add an .ico file to enable
)
