"""OITem Pillar Tracker（ネイティブ版）起動スクリプト

  python main.py            アプリのウィンドウで起動（pywebview）
  python main.py --browser  既定のブラウザで開く（ウィンドウが使えない環境用）
"""
from __future__ import annotations

import argparse
import multiprocessing
import os
import sys
import threading
import time
import webbrowser

import server


def run_browser(app, srv):
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    print(f"{server.APP_NAME}: {url}  （終了は Ctrl+C）")
    webbrowser.open(url)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


def run_window(app, srv):
    import webview

    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    holder = {}

    def save_dialog(name, data):
        win = holder["win"]
        ext = os.path.splitext(name)[1].lstrip(".")
        types = (f"{ext.upper()} (*.{ext})", "All files (*.*)") if ext else ("All files (*.*)",)
        res = win.create_file_dialog(webview.SAVE_DIALOG, save_filename=name, file_types=types)
        if not res:
            return None
        path = res if isinstance(res, str) else res[0]
        with open(path, "wb") as f:
            f.write(data)
        return path

    app.save_dialog = save_dialog
    win = webview.create_window(server.APP_NAME, url, width=1440, height=920, min_size=(960, 640),
                                text_select=True)
    holder["win"] = win
    webview.start(private_mode=False)


def main():
    multiprocessing.freeze_support()
    ap = argparse.ArgumentParser()
    ap.add_argument("--browser", action="store_true", help="既定のブラウザで開く")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--workers", type=int, default=0, help="同時に解析する動画の数")
    ap.add_argument("--selftest", action="store_true", help="合成動画で動作確認して終了")
    ap.add_argument("--log", help="出力をこのファイルにも書く")
    a, _ = ap.parse_known_args()  # macOS が付ける -psn_ 引数などは無視

    if a.log or sys.stdout is None:
        # ウィンドウ版（コンソールなし）でもログを残せるようにする
        import tempfile
        path = a.log or os.path.join(tempfile.gettempdir(), "oitem_pillar_tracker.log")
        f = open(path, "w", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = f

    if a.selftest:
        import selftest
        sys.exit(selftest.run())

    app = server.App(workers=a.workers or None)
    srv = server.start_server(app, a.port)
    try:
        if a.browser:
            run_browser(app, srv)
        else:
            try:
                run_window(app, srv)
            except ImportError:
                run_browser(app, srv)
    finally:
        srv.shutdown()
        app.cleanup()


if __name__ == "__main__":
    main()
