# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = []
binaries = []
hiddenimports = []
tmp_ret = collect_all('streamlink')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('streamlink_cli')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('certifi')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('pychromecast')   # Google Cast / Android TV support
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('zeroconf')        # mDNS discovery for cast devices
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('static_ffmpeg')   # bundled ffmpeg (HLS->Cast remux). Pre-fetch the binary
# the helper only calls ffmpeg; drop the unused ffprobe (~95 MB) from the frozen build.
_no_ffprobe = lambda lst: [t for t in lst if not str(t[0]).lower().endswith(('ffprobe.exe', 'ffprobe'))]
datas += _no_ffprobe(tmp_ret[0]); binaries += _no_ffprobe(tmp_ret[1]); hiddenimports += tmp_ret[2]  # before building (see PACKAGING.md §6)


a = Analysis(
    ['..\\helper\\streaming-lan-cast-helper.py'],
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
    name='streaming-lan-cast-helper',
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
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='streaming-lan-cast-helper',
)
