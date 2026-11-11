"""OITem Pillar Tracker — ネイティブ解析エンジン

index.html（OpenCV.js 版）の trackVideo / trackFrame / refineCenter を
そのまま移植したもの。違いは「1 フレームごとにシークする」代わりに
動画を先頭から順番にデコードする点だけで、どのフレームを解析するか
（フレーム中央の時刻 (f+0.5)/fps に表示される画像）は Web 版と同じ。
"""
from __future__ import annotations

import math
import os
import sys
import threading
from dataclasses import dataclass, field
from fractions import Fraction

import av
import cv2
import numpy as np

cv2.setNumThreads(1)  # 動画単位で並列化するので OpenCV 内部のスレッドは使わない


class Aborted(Exception):
    pass


# ----------------------------------------------------------------------
# 動画の読み込み
# ----------------------------------------------------------------------
@dataclass
class VideoInfo:
    path: str
    width: int
    height: int
    duration: float
    nb_frames: int
    avg_fps: float


def probe(path: str) -> VideoInfo:
    with av.open(path) as c:
        s = c.streams.video[0]
        dur = None
        if s.duration is not None and s.time_base is not None:
            dur = float(s.duration * s.time_base)
        if not dur and c.duration:
            dur = c.duration / 1_000_000
        w, h = s.codec_context.width, s.codec_context.height
        nb = s.frames or 0
        fps = float(s.average_rate) if s.average_rate else 0.0
    if not dur or not w:
        # 長さが記録されていない形式は一度最後まで読んで調べる
        last, cnt = 0.0, 0
        for t, img in iter_frames(path, "gray"):
            last, cnt = t, cnt + 1
            h, w = img.shape[:2]
        dur = dur or (last + (1 / fps if fps else 0))
        nb = nb or cnt
    return VideoInfo(path, int(w), int(h), float(dur), int(nb), fps)


def _frame_time(frame, t0):
    if frame.pts is not None and frame.time_base is not None:
        return float(frame.pts * frame.time_base)
    return t0


def iter_frames(path: str, fmt: str = "gray", start: float = 0.0):
    """(pts_time, ndarray) を表示順に返す。fmt: gray / rgb24 / bgr24。
    start > 0 なら start 以前の最寄りのキーフレームから読む。"""
    with av.open(path) as c:
        s = c.streams.video[0]
        s.thread_type = "AUTO"
        if start > 0 and s.time_base:
            try:
                c.seek(int(start / s.time_base), stream=s, backward=True, any_frame=False)
            except av.error.FFmpegError:
                c.seek(0, stream=s)
        t_prev = 0.0
        for fr in c.decode(s):
            t = _frame_time(fr, t_prev)
            t_prev = t
            if fmt == "gray":
                # 必要な部分だけ RGB → グレー変換する（追跡に使うのは ROI 周辺だけ）
                yield t, LazyGray(fr)
                continue
            if fmt == "fast":
                # 表示・オーバーレイ用（速度優先。色の変換は swscale に任せる）
                yield t, fr.reformat(format="rgb24", src_colorspace=_sws_cs(fr),
                                     src_color_range=_sws_range(fr)).to_ndarray()
                continue
            rgb = to_rgb(fr)
            if fmt == "bgr24":
                yield t, cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
            else:
                yield t, rgb


# YUV → RGB 変換はブラウザ（Chromium）に合わせる：
# 色空間の指定がない動画は BT.709・リミテッドレンジ、色差は最近傍補間。
# （ffmpeg 既定の BT.601 だと Web 版と画素値が数階調ずれ、結果がわずかに変わる）
_KR_KB = {1: (0.2126, 0.0722), 5: (0.299, 0.114), 6: (0.299, 0.114), 9: (0.2627, 0.0593)}


def _sws_cs(fr):
    return "ITU601" if getattr(fr, "colorspace", 2) in (5, 6) else "ITU709"


def _sws_range(fr):
    full = fr.format.name.startswith("yuvj") or getattr(fr, "color_range", 0) == 2
    return "JPEG" if full else "MPEG"


class LazyGray:
    """gray[y0:y1, x0:x1] で、その範囲だけを Web 版と同じ手順
    （YUV → RGB(8bit) → グレー）で変換して返す。"""

    def __init__(self, fr):
        self.fr = fr
        self.shape = (fr.height, fr.width)
        self._full = None

    def __getitem__(self, key):
        if self._full is not None:
            return self._full[key]
        ys, xs = key
        H, W = self.shape
        y0, y1, _ = ys.indices(H)
        x0, x1, _ = xs.indices(W)
        rgb = to_rgb(self.fr, (y0, y1, x0, x1))
        return cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)

    def full(self):
        if self._full is None:
            self._full = cv2.cvtColor(to_rgb(self.fr), cv2.COLOR_RGB2GRAY)
        return self._full


def to_rgb(fr, crop=None):
    name = fr.format.name
    H, W = fr.height, fr.width
    y0, y1, x0, x1 = crop or (0, H, 0, W)
    if name not in ("yuv420p", "yuvj420p", "yuv422p", "yuvj422p", "yuv444p", "yuvj444p"):
        if name.startswith(("rgb", "bgr", "argb", "abgr", "gbr", "gray")):
            return np.ascontiguousarray(fr.to_ndarray(format="rgb24")[y0:y1, x0:x1])
        fr = fr.reformat(format="yuv444p", interpolation="POINT")
        name = "yuv444p"
    full = name.startswith("yuvj") or getattr(fr, "color_range", 0) == 2
    kr, kb = _KR_KB.get(getattr(fr, "colorspace", 2), _KR_KB[1])
    kg = 1 - kr - kb
    ch = 2 if "420" in name else 1
    cw = 2 if ("420" in name or "422" in name) else 1

    def plane(i):
        p = fr.planes[i]
        return np.frombuffer(p, np.uint8).reshape(p.height, p.line_size)

    y = plane(0)[y0:y1, x0:x1].astype(np.float32)
    cy0, cy1 = y0 // ch, (y1 - 1) // ch + 1
    cx0, cx1 = x0 // cw, (x1 - 1) // cw + 1

    def chroma(i):
        c = plane(i)[cy0:cy1, cx0:cx1].astype(np.float32) - 128
        if ch > 1 or cw > 1:
            c = np.repeat(np.repeat(c, ch, 0), cw, 1)
            oy, ox = y0 - cy0 * ch, x0 - cx0 * cw
            c = c[oy:oy + (y1 - y0), ox:ox + (x1 - x0)]
        return c

    u, v = chroma(1), chroma(2)
    if full:
        ys, cs = 1.0, 1.0
    else:
        y -= 16
        ys, cs = 255 / 219, 255 / 224
    y *= ys
    u *= cs
    v *= cs
    rgb = np.empty(y.shape + (3,), np.float32)
    rgb[..., 0] = y + 2 * (1 - kr) * v
    rgb[..., 1] = y - (kr / kg) * 2 * (1 - kr) * v - (kb / kg) * 2 * (1 - kb) * u
    rgb[..., 2] = y + 2 * (1 - kb) * u
    np.rint(rgb, out=rgb)
    np.clip(rgb, 0, 255, out=rgb)
    return rgb.astype(np.uint8)


def frame_count(duration: float, fps: float) -> int:
    return max(1, int(math.floor(duration * fps + 1e-6)))


def sample_time(f: int, fps: float, duration: float) -> float:
    # index.html の frameTime と同じ（フレーム中央）
    return min((f + 0.5) / fps, max(0.0, duration - 1e-4))


class Sampler:
    """サンプル時刻（単調増加）ごとに「その時刻に表示されているフレーム」を返す。
    ブラウザの <video> と同じく、pts <= t の最後のフレーム（なければ最初のフレーム）。"""

    def __init__(self, path, fmt="gray", start=0.0):
        self.it = iter_frames(path, fmt, start)
        self.cur = None
        self.nxt = next(self.it, None)
        self.serial = -1  # 何枚目のデコード画像か（同じ画像の再利用判定用）

    def at(self, t):
        if self.cur is None:
            if self.nxt is None:
                raise RuntimeError("動画からフレームを読み出せませんでした")
            self.cur, self.nxt = self.nxt, next(self.it, None)
            self.serial += 1
        while self.nxt is not None and self.nxt[0] <= t + 1e-9:
            self.cur, self.nxt = self.nxt, next(self.it, None)
            self.serial += 1
        return self.cur[1], self.serial


def grab_frame(path: str, t: float, fmt: str = "rgb24"):
    """1 枚だけ取得（ビューア・ROI 設定用）。"""
    s = Sampler(path, fmt, start=max(0.0, t - 0.5))
    img, _ = s.at(t)
    s.it.close()
    return img


# ----------------------------------------------------------------------
# 追跡（index.html の trackFrame / refineCenter と同じ処理）
# ----------------------------------------------------------------------
def roi_box(r):
    if r["type"] == "rect":
        return int(r["x"]), int(r["y"]), int(r["w"]), int(r["h"])
    return int(r["cx"] - r["r"]), int(r["cy"] - r["r"]), int(2 * r["r"]), int(2 * r["r"])


def _js_round(v):
    # JavaScript の Math.round（.5 は +∞ 方向）
    return int(math.floor(v + 0.5))


def refine_center(gray, x, y, box, s, W, H):
    pad = 10
    bx, by, bw, bh = box
    rx = max(0, _js_round(x - pad))
    ry = max(0, _js_round(y - pad))
    rw = min(W - rx, _js_round(bw + pad * 2))
    rh = min(H - ry, _js_round(bh + pad * 2))
    if rw <= 0 or rh <= 0:
        return None
    local = gray[ry:ry + rh, rx:rx + rw]
    blur = cv2.GaussianBlur(local, (5, 5), 0)
    typ = cv2.THRESH_BINARY if s["polarity"] == "bright" else cv2.THRESH_BINARY_INV
    _, binimg = cv2.threshold(blur, 0, 255, typ + cv2.THRESH_OTSU)
    roi = s["roi"]
    if roi["type"] == "circle":
        mask = np.zeros(binimg.shape, np.uint8)
        lcx = _js_round(x + bw / 2 - rx)
        lcy = _js_round(y + bh / 2 - ry)
        cv2.circle(mask, (lcx, lcy), _js_round(roi["r"] + pad), 255, -1)
        binimg = cv2.bitwise_and(binimg, mask)
    contours, _ = cv2.findContours(binimg, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    best, best_area = None, 0.0
    for cnt in contours:
        a = cv2.contourArea(cnt)
        if a > best_area:
            best_area, best = a, cnt
    if best is None or best_area <= 5:
        return None
    (ccx, ccy), rad = cv2.minEnclosingCircle(best)
    return rx + float(ccx), ry + float(ccy), float(best_area), float(rad)


def track_frame(gray, template, last_x, last_y, s, W, H):
    box = roi_box(s["roi"])
    _, _, bw, bh = box
    margin = s["margin"]
    sx = max(0, _js_round(last_x - margin))
    sy = max(0, _js_round(last_y - margin))
    sw = min(W - sx, bw + margin * 2)
    sh = min(H - sy, bh + margin * 2)
    if sw < bw or sh < bh:
        return None
    search = gray[sy:sy + sh, sx:sx + sw]
    res = cv2.matchTemplate(search, template, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, max_loc = cv2.minMaxLoc(res)
    mx, my = sx + max_loc[0], sy + max_loc[1]
    out = {"mx": mx, "my": my, "score": float(max_val), "x": None}
    c = refine_center(gray, mx, my, box, s, W, H)
    if c:
        out.update(x=c[0], y=c[1], area=c[2], radius=c[3])
    return out


def empty_row(f, fps):
    return {"frame": f, "t": f / fps, "x": None, "y": None, "mx": None, "my": None,
            "score": None, "area": None, "radius": None}


@dataclass
class Job:
    progress: float = 0.0
    message: str = ""
    abort: threading.Event = field(default_factory=threading.Event)

    def check(self):
        if self.abort.is_set():
            raise Aborted()


def track_video(info: VideoInfo, s: dict, job: Job | None = None, chunk_bytes=512 * 2**20):
    """追跡して行データのリストを返す（index.html の trackVideo と同じ順序・同じ規則）。"""
    job = job or Job()
    W, H = info.width, info.height
    fps = float(s["fps"])
    n = frame_count(info.duration, fps)
    f0 = min(int(s.get("roiFrame") or 0), n - 1)
    bx, by, bw, bh = roi_box(s["roi"])

    # テンプレート：ROI 設定フレームの画像から作る（Web 版の firstFrame と同じフレーム）
    first = grab_frame(info.path, sample_time(f0, fps, info.duration), "gray")
    template = first[by:by + bh, bx:bx + bw].copy()

    rows = [None] * n
    total = n
    done = 0

    def tick():
        nonlocal done
        done += 1
        job.progress = done / total
        job.check()

    cache = {}

    def track_at(gray, serial, f, last):
        t = track_frame(gray, template, last[0], last[1], s, W, H)
        row = empty_row(f, fps)
        if t:
            row.update(mx=t["mx"], my=t["my"], score=t["score"])
            if t["x"] is not None:
                row.update(x=t["x"], y=t["y"], area=t["area"], radius=t["radius"])
                last[0], last[1] = t["mx"], t["my"]
        return row

    # ① ROI 設定フレームより前：後ろ向き（f0-1 → 0）。メモリを抑えるため区間ごとに読む
    if f0 > 0:
        last = [bx, by]
        per = max(1, chunk_bytes // max(1, W * H))
        hi = f0  # 未処理区間 [lo, hi)
        while hi > 0:
            lo = max(0, hi - per)
            t_lo = sample_time(lo, fps, info.duration)
            sampler = Sampler(info.path, "gray", start=max(0.0, t_lo - 1.0))
            frames = []
            for f in range(lo, hi):
                img, serial = sampler.at(sample_time(f, fps, info.duration))
                frames.append(img)
            sampler.it.close()
            for i in range(hi - 1, lo - 1, -1):
                rows[i] = track_at(frames[i - lo], None, i, last)
                tick()
            frames.clear()
            hi = lo

    # ② f0 以降：前向き
    last = [bx, by]
    sampler = Sampler(info.path, "gray", start=max(0.0, sample_time(f0, fps, info.duration) - 1.0))
    for f in range(f0, n):
        img, _ = sampler.at(sample_time(f, fps, info.duration))
        rows[f] = track_at(img, None, f, last)
        tick()
    sampler.it.close()
    return [r or empty_row(i, fps) for i, r in enumerate(rows)]


# ----------------------------------------------------------------------
# オーバーレイ動画（index.html の drawTrackOverlay と同じ見た目）
# ----------------------------------------------------------------------
_FONT_CANDIDATES = [
    "/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
    "C:/Windows/Fonts/meiryo.ttc",
    "C:/Windows/Fonts/YuGothM.ttc",
    "C:/Windows/Fonts/msgothic.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]
_font_cache = {}


def _font(size):
    if size in _font_cache:
        return _font_cache[size]
    from PIL import ImageFont
    f = None
    for p in _FONT_CANDIDATES:
        if os.path.exists(p):
            try:
                f = ImageFont.truetype(p, size)
                break
            except OSError:
                pass
    _font_cache[size] = f
    return f


def _lw(W):
    return max(1.5, W / 480)


def _draw_label(img, text, ok, W):
    fs = max(12, round(W / 55))
    font = _font(fs)
    color = (255, 255, 255) if ok else (255, 180, 171)
    if font is None:
        text = text.replace("未検出", "not detected")
        scale = fs / 22
        (tw, _), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, 1)
        _bg(img, 6, 6, tw + 12, fs + 10)
        cv2.putText(img, text, (12, 6 + fs + 1), cv2.FONT_HERSHEY_SIMPLEX, scale, color, 1, cv2.LINE_AA)
        return
    from PIL import Image, ImageDraw
    tw = int(font.getlength(text))
    bw, bh = tw + 12, fs + 10
    _bg(img, 6, 6, bw, bh)
    patch = Image.fromarray(img[6:6 + bh, 6:6 + bw])
    ImageDraw.Draw(patch).text((6, bh - 4), text, font=font, fill=color, anchor="ls")
    img[6:6 + bh, 6:6 + bw] = np.asarray(patch)


def _bg(img, x, y, w, h):
    h = min(h, img.shape[0] - y)
    w = min(w, img.shape[1] - x)
    sub = img[y:y + h, x:x + w]
    img[y:y + h, x:x + w] = (sub.astype(np.float32) * 0.45).astype(np.uint8)


SHIFT = 4  # サブピクセル描画


def _p(x, y):
    return int(round(x * (1 << SHIFT))), int(round(y * (1 << SHIFT)))


def draw_overlay(img, s, rows, f, W, show_traj):
    """img: RGB ndarray（書き換える）"""
    lw = _lw(W)
    th = max(1, int(round(lw)))
    roi = s["roi"]
    row = rows[f] if f < len(rows) else None
    hit = row is not None and row.get("x") is not None
    if hit and roi:
        _, _, bw, bh = roi_box(roi)
        mx, my = row["mx"], row["my"]
        green = (52, 199, 89)
        if roi["type"] == "rect":
            cv2.rectangle(img, (int(mx), int(my)), (int(mx + bw), int(my + bh)), green, th, cv2.LINE_AA)
        else:
            cv2.circle(img, _p(mx + bw / 2, my + bh / 2), int(round(roi["r"] * (1 << SHIFT))), green, th, cv2.LINE_AA, SHIFT)
    if show_traj:
        pts = [_p(r["x"], r["y"]) for r in rows[:f + 1] if r and r.get("x") is not None]
        if len(pts) >= 2:
            cv2.polylines(img, [np.array(pts, np.int32)], False, (255, 165, 0), th, cv2.LINE_AA, SHIFT)
    if hit:
        rad = max(3.5, lw * 2.2)
        c = _p(row["x"], row["y"])
        cv2.circle(img, c, int(rad * (1 << SHIFT)), (255, 59, 48), -1, cv2.LINE_AA, SHIFT)
        cv2.circle(img, c, int(rad * (1 << SHIFT)), (255, 255, 255), max(1, int(round(lw * 0.6))), cv2.LINE_AA, SHIFT)
    t = f / float(s["fps"])
    label = (f"frame {f}  t={t:.3f}s  score={row['score']:.3f}" if hit else f"frame {f}  t={t:.3f}s  未検出")
    _draw_label(img, label, hit, W)


def write_overlay(info: VideoInfo, s: dict, rows, out_path: str, job: Job | None = None):
    """解析結果を重ねた動画を、元の fps 設定のまま MP4 (H.264) で書き出す。"""
    job = job or Job()
    fps = float(s["fps"])
    n = len(rows)
    W, H = info.width, info.height
    W2, H2 = W + (W % 2), H + (H % 2)  # yuv420p は偶数サイズが必要
    with av.open(out_path, "w") as out:
        rate = Fraction(fps).limit_denominator(1001)
        codec = h264_encoder()
        st = out.add_stream(codec, rate=rate)
        st.width, st.height, st.pix_fmt = W2, H2, "yuv420p"
        if codec == "libx264":
            st.options = {"crf": "20", "preset": "veryfast"}
        else:
            st.bit_rate = int(W2 * H2 * fps * 0.15)
        try:
            st.thread_type = "AUTO"
        except Exception:  # noqa: BLE001
            pass
        sampler = Sampler(info.path, "fast")
        show = bool(s.get("showTrajectory", True))
        for f in range(n):
            img, _ = sampler.at(sample_time(f, fps, info.duration))
            img = img.copy()
            draw_overlay(img, s, rows, f, W, show)
            if (W2, H2) != (W, H):
                img = cv2.copyMakeBorder(img, 0, H2 - H, 0, W2 - W, cv2.BORDER_REPLICATE)
            frame = av.VideoFrame.from_ndarray(img, format="rgb24")
            frame.pts = f
            frame.time_base = Fraction(1) / rate
            for pkt in st.encode(frame):
                out.mux(pkt)
            job.progress = (f + 1) / n
            job.check()
        sampler.it.close()
        for pkt in st.encode():
            out.mux(pkt)


def h264_encoder():
    # libx264 が基本。無い場合は OS 標準のハードウェアエンコーダを使う
    for c in ("libx264", "h264_videotoolbox", "h264_mf", "libopenh264", "mpeg4"):
        if _has(c):
            return c
    raise RuntimeError("動画エンコーダが見つかりません")


def _has(codec):
    try:
        av.codec.Codec(codec, "w")
        return True
    except Exception:
        return False
