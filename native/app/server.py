"""ローカル HTTP サーバ：index.html を配信し、解析を Python 側で実行する API を提供する。

127.0.0.1 にだけ待ち受け、起動ごとに作るトークンを持つリクエストしか受け付けない。
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import sys
import tempfile
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import numpy as np

import tracker

APP_NAME = "OITem Pillar Tracker"
NATIVE_VERSION = "1.0.0"


def resource_dir():
    # PyInstaller で固めたときは展開先、そうでなければこのファイルの場所
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


def default_workers():
    n = os.cpu_count() or 2
    return max(1, min(n - 1, 6))


class FrameReader:
    """ビューア用：コマ送りは続きから読み、離れた位置はシークして読む。"""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.sampler = None
        self.last_t = None

    def get(self, t):
        with self.lock:
            if self.sampler is None or self.last_t is None or t < self.last_t or t - self.last_t > 2.0:
                if self.sampler:
                    self.sampler.it.close()
                self.sampler = tracker.Sampler(self.path, "rgb24", start=max(0.0, t - 0.5))
            img, _ = self.sampler.at(t)
            self.last_t = t
            return img

    def close(self):
        with self.lock:
            if self.sampler:
                self.sampler.it.close()
                self.sampler = None


class Video:
    def __init__(self, vid, name, path):
        self.id = vid
        self.name = name
        self.path = path
        self.info = tracker.probe(path)
        self.reader = FrameReader(path)
        self.overlay_path = None


class JobState:
    def __init__(self, video, settings, overlay):
        self.id = uuid.uuid4().hex
        self.video = video
        self.settings = settings
        self.overlay = overlay
        self.state = "queued"  # queued / tracking / overlay / done / error / aborted
        self.job = tracker.Job()
        self.rows = None
        self.error = ""
        self.elapsed = {}

    def progress(self):
        p = self.job.progress
        if self.state == "tracking":
            return p * (0.4 if self.overlay else 1.0)
        if self.state == "overlay":
            return 0.4 + 0.6 * p
        if self.state == "done":
            return 1.0
        return 0.0

    def run(self, tmpdir):
        try:
            if self.job.abort.is_set():
                self.state = "aborted"
                return
            self.state = "tracking"
            t0 = time.perf_counter()
            self.rows = tracker.track_video(self.video.info, self.settings, self.job)
            self.elapsed["track"] = time.perf_counter() - t0
            if self.overlay:
                self.state = "overlay"
                self.job.progress = 0.0
                t0 = time.perf_counter()
                out = os.path.join(tmpdir, f"overlay_{self.video.id}_{self.id[:8]}.mp4")
                tracker.write_overlay(self.video.info, self.settings, self.rows, out, self.job)
                old, self.video.overlay_path = self.video.overlay_path, out
                if old and os.path.exists(old):
                    try:
                        os.remove(old)
                    except OSError:
                        pass
                self.elapsed["overlay"] = time.perf_counter() - t0
            self.state = "done"
        except tracker.Aborted:
            self.state = "aborted"
        except Exception as ex:  # noqa: BLE001
            traceback.print_exc()
            self.error = str(ex)
            self.state = "error"


class App:
    def __init__(self, workers=None):
        self.token = secrets.token_urlsafe(24)
        self.tmpdir = tempfile.mkdtemp(prefix="oitem_")
        self.videos: dict[str, Video] = {}
        self.jobs: dict[str, JobState] = {}
        self.workers = workers or default_workers()
        self.pool = ThreadPoolExecutor(max_workers=self.workers)
        self.save_dialog = None  # pywebview が設定する: (name, data: bytes) -> path | None
        self.lock = threading.Lock()

    def cleanup(self):
        self.pool.shutdown(wait=False, cancel_futures=True)
        for v in self.videos.values():
            v.reader.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def index_html(self):
        with open(os.path.join(resource_dir(), "web", "index.html"), encoding="utf-8") as f:
            html = f.read()
        cfg = json.dumps({"token": self.token, "version": NATIVE_VERSION, "workers": self.workers,
                          "saveDialog": self.save_dialog is not None})
        inject = f"<script>window.__OITEM_NATIVE__ = {cfg};</script>\n"
        return html.replace("<script>", inject + "<script>", 1)


def make_handler(app: App):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        # --- helpers -------------------------------------------------
        def send(self, code, body=b"", ctype="application/json", headers=None):
            if isinstance(body, (dict, list)):
                body = json.dumps(body, ensure_ascii=False).encode()
            elif isinstance(body, str):
                body = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def err(self, code, msg):
            self.send(code, {"error": msg})

        def body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(n) if n else b""

        def authed(self):
            return secrets.compare_digest(self.headers.get("X-Token", ""), app.token)

        def q(self):
            u = urlparse(self.path)
            return u.path, {k: v[0] for k, v in parse_qs(u.query).items()}

        # --- routes --------------------------------------------------
        def do_GET(self):
            path, q = self.q()
            if path in ("/", "/index.html"):
                return self.send(200, app.index_html(), "text/html; charset=utf-8")
            if not self.authed() and not (path == "/api/overlay" and q.get("token") == app.token):
                return self.err(403, "forbidden")
            try:
                if path == "/api/frame":
                    v = app.videos[q["vid"]]
                    img = v.reader.get(float(q["t"]))
                    h, w = img.shape[:2]
                    rgba = np.empty((h, w, 4), np.uint8)
                    rgba[..., :3] = img
                    rgba[..., 3] = 255
                    return self.send(200, rgba.tobytes(), "application/octet-stream",
                                     {"X-Width": str(w), "X-Height": str(h)})
                if path == "/api/job":
                    ids = q["ids"].split(",")
                    out = {}
                    for i in ids:
                        j = app.jobs.get(i)
                        if not j:
                            continue
                        d = {"state": j.state, "progress": j.progress(), "error": j.error, "elapsed": j.elapsed}
                        if j.state in ("done",) or (j.state == "overlay" and q.get("rows")):
                            d["rows"] = j.rows
                        out[i] = d
                    return self.send(200, out)
                if path == "/api/overlay":
                    v = app.videos[q["vid"]]
                    if not v.overlay_path:
                        return self.err(404, "no overlay")
                    with open(v.overlay_path, "rb") as f:
                        return self.send(200, f.read(), "video/mp4")
                if path == "/api/ping":
                    return self.send(200, {"ok": True, "workers": app.workers})
                return self.err(404, "not found")
            except KeyError as ex:
                return self.err(404, f"unknown id {ex}")
            except Exception as ex:  # noqa: BLE001
                traceback.print_exc()
                return self.err(500, str(ex))

        def do_POST(self):
            path, q = self.q()
            if not self.authed():
                return self.err(403, "forbidden")
            try:
                if path == "/api/open":
                    name = q.get("name", "video")
                    vid = uuid.uuid4().hex[:12]
                    ext = os.path.splitext(name)[1][:8] or ".bin"
                    dst = os.path.join(app.tmpdir, f"src_{vid}{ext}")
                    n = int(self.headers.get("Content-Length") or 0)
                    with open(dst, "wb") as f:
                        left = n
                        while left > 0:
                            chunk = self.rfile.read(min(left, 8 << 20))
                            if not chunk:
                                break
                            f.write(chunk)
                            left -= len(chunk)
                    try:
                        v = Video(vid, name, dst)
                    except Exception as ex:  # noqa: BLE001
                        os.remove(dst)
                        return self.err(415, f"この動画を読み込めませんでした: {ex}")
                    app.videos[vid] = v
                    i = v.info
                    return self.send(200, {"vid": vid, "width": i.width, "height": i.height,
                                           "duration": i.duration, "frames": i.nb_frames, "avgFps": i.avg_fps})
                if path == "/api/track":
                    d = json.loads(self.body())
                    v = app.videos[d["vid"]]
                    j = JobState(v, d["settings"], bool(d.get("overlay")))
                    app.jobs[j.id] = j
                    app.pool.submit(j.run, app.tmpdir)
                    return self.send(200, {"job": j.id})
                if path == "/api/abort":
                    for j in app.jobs.values():
                        j.job.abort.set()
                    return self.send(200, {"ok": True})
                if path == "/api/close":
                    v = app.videos.pop(q["vid"], None)
                    if v:
                        v.reader.close()
                        for p in (v.path, v.overlay_path):
                            if p and os.path.exists(p):
                                try:
                                    os.remove(p)
                                except OSError:
                                    pass
                    return self.send(200, {"ok": True})
                if path == "/api/save":
                    if not app.save_dialog:
                        return self.err(501, "no dialog")
                    data = self.body()
                    saved = app.save_dialog(q.get("name", "download"), data)
                    return self.send(200, {"saved": saved})
                return self.err(404, "not found")
            except KeyError as ex:
                return self.err(404, f"unknown id {ex}")
            except Exception as ex:  # noqa: BLE001
                traceback.print_exc()
                return self.err(500, str(ex))

    return H


def start_server(app: App, port=0):
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    srv.daemon_threads = True
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    return srv
