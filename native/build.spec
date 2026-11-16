# PyInstaller 設定:  pyinstaller build.spec
import os, sys
block_cipher = None
# 自動 ROI 用に Web 版の opencv.js（リポジトリ直下）を同梱する
_datas = [('app/web', 'web')] + ([('../opencv.js', 'web')] if os.path.exists('../opencv.js') else [])
a = Analysis(['app/main.py'], pathex=['app'], datas=_datas,
             hiddenimports=['selftest', 'server', 'tracker'], excludes=['tkinter', 'matplotlib', 'pandas', 'scipy'] + (['gi'] if sys.platform != 'linux' else []))
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='OITem Pillar Tracker',
          console=False, disable_windowed_traceback=False, argv_emulation=False,
          codesign_identity=None, entitlements_file=None,
          icon='icon/icon.ico' if sys.platform == 'win32' else None)
coll = COLLECT(exe, a.binaries, a.datas, name='OITem Pillar Tracker')
if sys.platform == 'darwin':
    app = BUNDLE(coll, name='OITem Pillar Tracker.app', icon='icon/icon.icns', bundle_identifier='jp.ac.oit.oitem.pillartracker',
                 info_plist={'CFBundleShortVersionString': '1.1.0', 'NSHighResolutionCapable': True,
                             'LSMinimumSystemVersion': '11.0',
                             # アプリ内のローカルサーバ (http://127.0.0.1) に接続するため
                             'NSAppTransportSecurity': {'NSAllowsLocalNetworking': True}})
