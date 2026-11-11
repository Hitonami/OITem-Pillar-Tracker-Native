"""動作確認：既知の動きをする合成動画を作り、追跡・オーバーレイ書き出しまで通す。

  python main.py --selftest      （ビルド後のアプリでも同じ引数で実行できる）
"""
import math
import os
import tempfile
import time
from fractions import Fraction

import av
import cv2
import numpy as np

import tracker


def make_video(path, n=150, fps=50, W=640, H=480):
    xs, ys = [], []
    with av.open(path, "w") as out:
        st = out.add_stream(tracker.h264_encoder(), rate=Fraction(fps))
        st.width, st.height, st.pix_fmt = W, H, "yuv420p"
        for i in range(n):
            img = np.full((H, W, 3), 170, np.uint8)
            cx = 300 + 25 * math.sin(2 * math.pi * i / 60)
            cy = 240 + 10 * math.sin(2 * math.pi * i / 45)
            cv2.circle(img, (int(round(cx * 16)), int(round(cy * 16))), 9 * 16, (40, 40, 40), -1, cv2.LINE_AA, 4)
            xs.append(cx)
            ys.append(cy)
            fr = av.VideoFrame.from_ndarray(img, format="rgb24")
            for p in st.encode(fr):
                out.mux(p)
        for p in st.encode():
            out.mux(p)
    return xs, ys


def run():
    d = tempfile.mkdtemp(prefix="oitem_selftest_")
    src = os.path.join(d, "synthetic.mp4")
    print("encoder:", tracker.h264_encoder())
    xs, ys = make_video(src)
    info = tracker.probe(src)
    print("video:", info.width, "x", info.height, f"{info.duration:.3f}s", info.nb_frames, "frames")
    s = dict(fps=50, margin=40, polarity="dark", roi={"type": "circle", "cx": 300, "cy": 240, "r": 14},
             roiFrame=30, showTrajectory=True)
    t0 = time.perf_counter()
    rows = tracker.track_video(info, s)
    t1 = time.perf_counter()
    err = [math.hypot(r["x"] - xs[r["frame"]], r["y"] - ys[r["frame"]]) for r in rows if r["x"] is not None]
    print(f"tracked {len(err)}/{len(rows)} frames in {t1 - t0:.2f}s, max error {max(err):.2f}px, mean {sum(err) / len(err):.2f}px")
    ov = os.path.join(d, "overlay.mp4")
    tracker.write_overlay(info, s, rows, ov)
    print(f"overlay {os.path.getsize(ov)} bytes in {time.perf_counter() - t1:.2f}s")
    ok = len(err) == len(rows) and max(err) < 1.0 and os.path.getsize(ov) > 1000
    print("SELFTEST", "OK" if ok else "FAILED")
    return 0 if ok else 1
