"""ローカル HTTP サーバ：index.html を配信し、解析を Python 側で実行する API を提供する。

127.0.0.1 にだけ待ち受け、起動ごとに作るトークンを持つリクエストしか受け付けない。
"""
from __future__ import annotations

import json
import os
import re
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
NATIVE_VERSION = "1.2.0"


def resource_dir():
    # PyInstaller で固めたときは展開先、そうでなければこのファイルの場所
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


def user_data_dir():
    """ROI 学習データなどを置くフォルダ（macOS: ~/Library/Application Support/…, Windows: %APPDATA%\\…）"""
    if sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    elif sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    d = os.path.join(base, APP_NAME)
    os.makedirs(d, exist_ok=True)
    return d


DS_SUBDIR = "OITem_ROI_dataset"
_BAD = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def sample_file_name(s):
    """Web 版と同じ規則のファイル名（同じ共有フォルダを Web 版とネイティブ版で使える）"""
    return _BAD.sub("_", f"{s.get('file_name')}__{s.get('file_size') or 0}")[:180] + ".json"


def _newer(a, b):
    return str(a.get("added_at") or "") > str(b.get("added_at") or "")


class RoiDataset:
    """ROI 学習データ（Web 版の IndexedDB と共有フォルダに相当）。

    手元の控え: <データフォルダ>/roi_dataset.json（共有フォルダにつながらないときも使える）
    共有フォルダ: <選んだフォルダ>/OITem_ROI_dataset/ に 1 サンプル 1 ファイル（Web 版と同じ形式・ファイル名）
    """
    FORMAT = "oitem-pillar-tracker-roi-dataset"

    def __init__(self, path=None, config_path=None):
        self.path = path or os.path.join(user_data_dir(), "roi_dataset.json")
        self.config_path = config_path or os.path.join(os.path.dirname(self.path), "config.json")
        self.lock = threading.RLock()
        self.samples: dict[str, dict] = {}
        self.known: dict[str, tuple] = {}      # ファイル名 → (mtime, size)
        self.in_folder: dict[str, str] = {}    # id → added_at
        self.connected = False
        self.error = ""
        try:
            with open(self.path, encoding="utf-8") as f:
                for s in json.load(f).get("samples", []):
                    if isinstance(s, dict) and s.get("id"):
                        self.samples[s["id"]] = s
        except FileNotFoundError:
            pass
        except Exception:  # noqa: BLE001  壊れていたら退避して新しく始める
            traceback.print_exc()
            try:
                os.replace(self.path, self.path + f".broken-{int(time.time())}")
            except OSError:
                pass
        self.folder = self._load_config().get("dataset_folder")
        if self.folder:
            self._sync()

    # --- 設定 ---
    def _load_config(self):
        try:
            with open(self.config_path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:  # noqa: BLE001
            return {}

    def _save_config(self):
        cfg = self._load_config()
        cfg["dataset_folder"] = self.folder
        tmp = self.config_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.config_path)

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"format": self.FORMAT, "version": 1, "samples": list(self.samples.values())}, f, ensure_ascii=False)
        os.replace(tmp, self.path)

    def shared_dir(self):
        if not self.folder:
            return None
        return self.folder if os.path.basename(os.path.normpath(self.folder)) == DS_SUBDIR else os.path.join(self.folder, DS_SUBDIR)

    def location(self):
        if not self.folder:
            return {"kind": "app", "path": self.path, "connected": False}
        return {"kind": "folder", "path": self.shared_dir(), "name": os.path.basename(os.path.normpath(self.folder)),
                "connected": self.connected, "error": self.error}

    # --- 共有フォルダとの同期 ---
    def _write_file(self, d, s):
        fp = os.path.join(d, sample_file_name(s))
        tmp = fp + f".tmp-{uuid.uuid4().hex[:6]}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False)
        os.replace(tmp, fp)
        st = os.stat(fp)
        self.known[os.path.basename(fp)] = (st.st_mtime_ns, st.st_size)
        self.in_folder[s["id"]] = s.get("added_at")

    def _sync(self):
        """共有フォルダの新しい・変わったファイルを読み、手元にしかない（新しい）サンプルを書き出す。"""
        d = self.shared_dir()
        if not d:
            return 0
        try:
            if not os.path.isdir(self.folder):  # NAS が外れているときなど（親フォルダは作らない）
                raise OSError(f"フォルダが見つかりません: {self.folder}")
            os.makedirs(d, exist_ok=True)
            changed = 0
            for name in os.listdir(d):
                if not name.endswith(".json"):
                    continue
                fp = os.path.join(d, name)
                try:
                    st = os.stat(fp)
                    sig = (st.st_mtime_ns, st.st_size)
                    if self.known.get(name) == sig:
                        continue
                    with open(fp, encoding="utf-8") as f:
                        s = json.load(f)
                    self.known[name] = sig
                except Exception:  # noqa: BLE001  書きかけ・壊れたファイルは飛ばす
                    continue
                if not isinstance(s, dict) or not s.get("id"):
                    continue
                self.in_folder[s["id"]] = s.get("added_at")
                cur = self.samples.get(s["id"])
                if cur is None or _newer(s, cur):
                    self.samples[s["id"]] = s
                    changed += 1
            for s in list(self.samples.values()):
                if s["id"] not in self.in_folder or str(s.get("added_at") or "") > str(self.in_folder[s["id"]] or ""):
                    self._write_file(d, s)
            if changed:
                self._save()
            self.connected, self.error = True, ""
            return changed
        except OSError as ex:
            self.connected, self.error = False, str(ex)
            return 0

    def set_folder(self, folder):
        with self.lock:
            if folder:
                folder = os.path.abspath(os.path.expanduser(folder))
                if not os.path.isdir(folder):
                    raise ValueError(f"フォルダが見つかりません: {folder}")
            self.folder = folder or None
            self.known.clear()
            self.in_folder.clear()
            self.connected = False
            self._save_config()
            if self.folder:
                self._sync()
                if not self.connected:
                    raise OSError(f"共有フォルダに書き込めません: {self.error}")

    def all(self):
        with self.lock:
            if self.folder:
                self._sync()
            return list(self.samples.values())

    def put_many(self, samples):
        with self.lock:
            for s in samples:
                if isinstance(s, dict) and s.get("id"):
                    cur = self.samples.get(s["id"])
                    if cur is None or not _newer(cur, s):
                        self.samples[s["id"]] = s
            self._save()
            if self.folder:
                self._sync()   # 書き出し（つながらないときは次回に送る）

    def clear(self):
        with self.lock:
            if self.folder:
                raise ValueError("共有フォルダ使用中は消去できません")
            self.samples.clear()
            self._save()


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
        self._dataset = None
        self.folder_dialog = None  # pywebview が設定する: () -> path | None
        self.lock = threading.Lock()

    def cleanup(self):
        self.pool.shutdown(wait=False, cancel_futures=True)
        for v in self.videos.values():
            v.reader.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    @property
    def dataset(self):
        if self._dataset is None:
            self._dataset = RoiDataset()
        return self._dataset

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
            if path == "/opencv.js":  # 自動 ROI 用（画面側で必要になったときだけ読み込む）
                cands = [os.path.join(resource_dir(), "web", "opencv.js"),
                         os.path.join(resource_dir(), "..", "..", "opencv.js")]  # ソースから実行したとき（リポジトリ直下）
                fp = next((c for c in cands if os.path.exists(c)), None)
                if not fp:
                    return self.err(404, "opencv.js がありません")
                with open(fp, "rb") as f:
                    return self.send(200, f.read(), "text/javascript; charset=utf-8")
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
                if path == "/api/dataset":
                    samples = app.dataset.all()
                    return self.send(200, {"samples": samples, "location": app.dataset.location()})
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
                if path == "/api/dataset/put":
                    d = json.loads(self.body() or b"{}")
                    app.dataset.put_many(d.get("samples") or [])
                    return self.send(200, {"ok": True, "count": len(app.dataset.samples), "location": app.dataset.location()})
                if path == "/api/dataset/clear":
                    try:
                        app.dataset.clear()
                    except ValueError as ex:
                        return self.err(409, str(ex))
                    return self.send(200, {"ok": True})
                if path == "/api/dataset/folder":
                    # {"choose": true} → フォルダ選択ダイアログ / {"path": "..."} → 指定 / {"path": null} → 共有をやめる
                    d = json.loads(self.body() or b"{}")
                    if d.get("choose"):
                        if not app.folder_dialog:
                            return self.err(501, "no dialog")
                        folder = app.folder_dialog()
                        if not folder:
                            return self.send(200, {"cancelled": True})
                    else:
                        folder = d.get("path")
                    try:
                        app.dataset.set_folder(folder)
                    except (OSError, ValueError) as ex:
                        return self.err(400, str(ex))
                    return self.send(200, {"samples": app.dataset.all(), "location": app.dataset.location()})
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
