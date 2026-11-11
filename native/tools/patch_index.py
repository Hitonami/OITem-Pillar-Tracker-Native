"""Web 版 index.html → ネイティブ版 index.html を作るパッチ。

  python tools/patch_index.py <Web 版 index.html> app/web/index.html

パッチ後の HTML も、そのままブラウザで開けば従来どおり OpenCV.js 版として動く。
ネイティブ版のサーバから配信されたとき（window.__OITEM_NATIVE__ があるとき）だけ
解析・フレーム取得・保存を Python 側に任せる。
Web 版を更新したら、このスクリプトをもう一度実行すれば追従できる
（置換箇所が見つからないときはエラーで止まるので、その箇所だけ直す）。
"""
import sys

src, dst = sys.argv[1], sys.argv[2]
html = open(src, encoding="utf-8").read()


def rep(old, new, count=1):
    global html
    n = html.count(old)
    if n != count:
        sys.exit(f"パッチ箇所が見つかりません（{n} 件）:\n{old[:200]}")
    html = html.replace(old, new)


# ---------------------------------------------------------------- 共通部品
rep('let cvReady = false;\n', '''let cvReady = false;
// ---- ネイティブ版（Python 解析エンジン）との連携 ----
const NATIVE = window.__OITEM_NATIVE__ || null;
async function napi(path, opts = {}) {
  const res = await fetch(path, { ...opts, headers: { ...(opts.headers || {}), "X-Token": NATIVE.token } });
  if (!res.ok) {
    let msg = res.status + " " + res.statusText;
    try { const j = await res.json(); if (j.error) msg = j.error; } catch {}
    throw new Error(msg);
  }
  return res;
}
async function nativeOpen(e) {
  if (e.vid) return;
  const res = await napi(`/api/open?name=${encodeURIComponent(e.name)}`, { method: "POST", body: e.file });
  const m = await res.json();
  e.vid = m.vid;
  e.width = m.width; e.height = m.height; e.duration = m.duration;
}
async function nativeFrame(e, t) {
  const res = await napi(`/api/frame?vid=${e.vid}&t=${t}`);
  const w = Number(res.headers.get("X-Width")), h = Number(res.headers.get("X-Height"));
  const buf = await res.arrayBuffer();
  return new ImageData(new Uint8ClampedArray(buf), w, h);
}
''')

# ---------------------------------------------------------------- OpenCV.js は読まない
rep('(function loadOpenCv() {\n', '''(function loadOpenCv() {
  if (NATIVE) {
    markCvReady();
    setCvStatus(`ネイティブ解析エンジン v${NATIVE.version}（同時 ${NATIVE.workers} 本）`, "ok");
    return;
  }
''')

# ---------------------------------------------------------------- 保存はネイティブのダイアログ
rep('function downloadBlob(blob, fileName) {\n', '''function downloadBlob(blob, fileName) {
  if (NATIVE && NATIVE.saveDialog) {
    napi(`/api/save?name=${encodeURIComponent(fileName)}`, { method: "POST", body: blob })
      .then(r => r.json())
      .then(j => { if (j.saved) setStatus("保存しました: " + j.saved); })
      .catch(err => setStatus("保存できませんでした: " + err.message, true));
    return;
  }
''')

# ---------------------------------------------------------------- フレーム取得
rep('''async function captureFirstFrame(e) {
  await loadIntoVideoEl(e);
  e.settings.roiFrame = roiFrameOf(e);''', '''async function captureFirstFrame(e) {
  if (NATIVE) {
    await nativeOpen(e);
    e.settings.roiFrame = roiFrameOf(e);
    e.firstFrame = await nativeFrame(e, frameTime(e, e.settings.roiFrame));
    return;
  }
  await loadIntoVideoEl(e);
  e.settings.roiFrame = roiFrameOf(e);''')

rep('''  try {
    await loadIntoVideoEl(e);
    e.width = vid.videoWidth;''', '''  try {
    if (NATIVE) {
      await nativeOpen(e);
      await captureFirstFrame(e);
      e.status = e.results.length ? "tracked" : "ready";
      e.error = "";
      return;
    }
    await loadIntoVideoEl(e);
    e.width = vid.videoWidth;''')

rep('''  const f = state.viewFrame;
  await loadIntoVideoEl(e);
  await seekTo(frameTime(e, f));
  if (token !== viewToken || state.running) return;''', '''  const f = state.viewFrame;
  if (NATIVE) {
    let img;
    try { img = await nativeFrame(e, frameTime(e, f)); } catch (err) { setStatus("フレームを読み込めませんでした: " + err.message, true); return; }
    if (token !== viewToken || state.running) return;
    e.viewImage = { frame: f, data: img };
    drawFirstFrameView();
    return;
  }
  await loadIntoVideoEl(e);
  await seekTo(frameTime(e, f));
  if (token !== viewToken || state.running) return;''')

# ---------------------------------------------------------------- 動画の削除
rep('''  const e = state.videos[idx];
  if (e.url) URL.revokeObjectURL(e.url);''', '''  const e = state.videos[idx];
  if (NATIVE && e.vid) napi(`/api/close?vid=${e.vid}`, { method: "POST" }).catch(() => {});
  if (e.url) URL.revokeObjectURL(e.url);''')

# ---------------------------------------------------------------- 解析（並列）
rep('''async function runAnalysis(list) {
  if (!cvReady)''', '''async function runAnalysisNative(targets, skipped) {
  state.running = true;
  state.abort = false;
  updateButtons();
  updateModeLabel();
  const t0 = performance.now();
  const jobs = [];
  try {
    for (const e of targets) {
      await nativeOpen(e);
      const s = cloneObj(e.settings);
      s.roiFrame = Math.min(s.roiFrame || 0, frameCount(e) - 1);
      const r = await napi("/api/track", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ vid: e.vid, settings: s, overlay: !!s.recordOverlay })
      });
      const { job } = await r.json();
      e.status = "running";
      e.results = [];
      jobs.push({ e, job, finished: false });
    }
    renderVideoList();
    let finishedCount = 0;
    while (finishedCount < jobs.length) {
      await new Promise(r => setTimeout(r, 150));
      const open = jobs.filter(j => !j.finished);
      const ids = open.map(j => j.job).join(",");
      const st = await (await napi(`/api/job?ids=${ids}`)).json();
      let sum = 0;
      for (const j of jobs) {
        const d = st[j.job];
        if (j.finished) { sum += 1; continue; }
        if (!d) continue;
        sum += d.progress;
        if (["done", "error", "aborted"].includes(d.state)) {
          j.finished = true;
          finishedCount++;
          const e = j.e;
          if (d.state === "done") {
            e.results = d.rows;
            e.stale = false;
            e.analyzedAt = new Date().toISOString();
            e.status = "tracked";
            e.repro = e.logResults ? compareWithLog(e) : null;
            if (e.settings.recordOverlay) {
              try {
                const blob = await (await napi(`/api/overlay?vid=${e.vid}`)).blob();
                if (e.overlayUrl) URL.revokeObjectURL(e.overlayUrl);
                e.overlayBlob = blob;
                e.overlayUrl = URL.createObjectURL(blob);
                e.overlayExt = "mp4";
              } catch (err) { console.warn(err); }
            }
            j.ok = true;
          } else if (d.state === "aborted") {
            e.status = e.results.length ? "tracked" : "ready";
            e.stale = !!e.results.length;
          } else {
            e.status = "error";
            e.error = d.error;
          }
          renderVideoList();
          if (e.id === state.activeId) { renderResults(); drawPlots(); }
        }
      }
      setProgress(sum / jobs.length);
      const done = jobs.filter(j => j.ok).length;
      if (!state.abort) setStatus(`解析中: ${done} / ${jobs.length} 本完了（同時 ${NATIVE.workers} 本まで並列）`);
    }
  } catch (err) {
    console.error(err);
    setStatus("解析中にエラーが発生しました: " + (err.message || err), true);
    for (const j of jobs) if (!j.finished && j.e.status === "running") { j.e.status = "error"; j.e.error = String(err.message || err); }
  } finally {
    state.running = false;
    setProgress(state.abort ? 0 : 1);
    renderVideoList();
    updateSlider();
    renderResults();
    updateOverlayPreview();
    await gotoFrame(state.viewFrame || 0);
  }
  const done = jobs.filter(j => j.ok).length;
  const errs = jobs.filter(j => j.e.status === "error");
  const sec = ((performance.now() - t0) / 1000).toFixed(1);
  if (state.abort) setStatus(`中止しました（完了 ${done} 本）。`);
  else if (errs.length) setStatus(`解析完了: ${done} 本、エラー ${errs.length} 本（${errs.map(e => e.e.name).join(", ")}）`, true);
  else if (!$("statusText").classList.contains("err")) setStatus(`解析完了: ${done} 本（${sec} 秒）` + (skipped ? `（ROI 未設定などで ${skipped} 本スキップ）` : ""));
}
async function runAnalysis(list) {
  if (NATIVE) {
    const targets = list.filter(e => e.file && e.settings.roi && e.firstFrame);
    if (!targets.length) { alert("解析できる動画がありません（動画の読み込みと ROI 設定が必要です）"); return; }
    return runAnalysisNative(targets, list.length - targets.length);
  }
  if (!cvReady)''')

rep('''$("abortBtn").onclick = () => { state.abort = true; setStatus("中止しています..."); };''',
    '''$("abortBtn").onclick = () => {
  state.abort = true;
  setStatus("中止しています...");
  if (NATIVE) napi("/api/abort", { method: "POST" }).catch(() => {});
};''')

# ---------------------------------------------------------------- ログに解析エンジンを記録
rep('''    app_version: APP_VERSION,
    created_at''', '''    app_version: APP_VERSION,
    engine: NATIVE ? `native ${NATIVE.version}` : "opencv.js",
    created_at''')

# ---------------------------------------------------------------- 表示まわり
rep('''window.addEventListener("beforeunload",''', '''if (NATIVE) {
  // ウィンドウにファイルを落としてもページが切り替わらないようにする
  for (const t of ["dragover", "drop"]) window.addEventListener(t, ev => ev.preventDefault());
  document.title = document.title.replace("OITem Pillar Tracker", "OITem Pillar Tracker（ネイティブ版）");
  for (const li of document.querySelectorAll("details li")) {
    if (li.textContent.includes("MediaRecorder")) li.textContent = "オーバーレイ動画は元動画と同じ fps 設定（解析条件の FPS）で MP4 (H.264) として書き出します。";
    else if (li.textContent.includes("opencv.js")) li.textContent = "ネイティブ版は OpenCV（C++）で解析し、複数の動画を並列に処理します。AVI など、ブラウザで再生できない形式も読み込めます。";
  }
}
window.addEventListener("beforeunload",''')

open(dst, "w", encoding="utf-8").write(html)
print("OK:", dst)
