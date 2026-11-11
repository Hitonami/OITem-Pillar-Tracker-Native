# OITem Pillar Tracker ネイティブ版（macOS / Windows）

Web 版（`index.html`）の画面をそのまま使い、解析だけを Python + OpenCV（C++）で行うデスクトップアプリです。

- 画面・操作・CSV / Excel / ZIP / JSON ログは Web 版と同じです（ログは相互に読み込めます）。
- 動画を先頭から順番に読んで解析するので、1 フレームごとのシーク待ちがありません。
- 複数の動画を並列に解析します（同時に処理する本数は CPU コア数から自動で決めます）。
- オーバーレイ動画は、元動画と同じ fps 設定の MP4 (H.264) として書き出します（MediaRecorder を使わないので、再生速度がずれません）。
- AVI（MJPEG など）、ブラウザでは再生できない形式も読み込めます。

## 使い方（配布版）

GitHub の **Actions** タブ → 「Build native app」→ 最新の実行 → **Artifacts** からダウンロードします（タグを付けた場合は **Releases** にも添付されます）。

| ファイル | 対象 |
|---|---|
| `OITem-Pillar-Tracker-macOS-AppleSilicon.zip` | M1 以降の Mac |
| `OITem-Pillar-Tracker-macOS-Intel.zip` | Intel Mac |
| `OITem-Pillar-Tracker-Windows.zip` | Windows 10 / 11（64 bit） |

### macOS
1. zip を展開して `OITem Pillar Tracker.app` を「アプリケーション」フォルダに移動します。
2. 初回だけ、署名していないアプリのため警告が出ます。
   - macOS 14 まで：アプリを右クリック →「開く」→「開く」
   - macOS 15 以降：一度開こうとした後、「システム設定」→「プライバシーとセキュリティ」→ 下の方の「このまま開く」
   - またはターミナルで `xattr -dr com.apple.quarantine "/Applications/OITem Pillar Tracker.app"`

### Windows
1. zip を展開し、フォルダごと好きな場所に置きます（フォルダの中のファイルは消さないでください）。
2. `OITem Pillar Tracker.exe` を起動します。初回に SmartScreen が出たら「詳細情報」→「実行」。
3. 画面表示には Microsoft Edge WebView2 ランタイムを使います（Windows 10 / 11 には通常入っています）。

## Web 版との違い（解析結果）

解析の手順・パラメータ（テンプレートマッチ TM_CCOEFF_NORMED、Gaussian 5×5、大津の二値化、最小外接円、探索範囲、前後方向の追跡）は Web 版と同じで、OpenCV も同じ 4.10 系です。同じ画素を入れたときに Web 版と結果が一致することを確認しています。ただし、動画のデコードがブラウザと違うため、次の差が出ます。

1. **画素値の ±1〜2 階調の差**：YUV→RGB 変換はブラウザ（Chrome）と同じ BT.709 に合わせていますが、完全には一致しません。輪郭のわずかな違いで、中心が 0.5 px 程度ずれるフレームがあります（Web 版自体も、同じフレームを読み直すと同程度ずれることがあります）。
2. **キーフレーム直前のフレーム**：Web 版（Chrome）は、シークしたときにキーフレーム直前のフレームを表示できず、次のフレームを読む場合があります（テスト動画では 25 フレームに 1 枚）。ネイティブ版は正しくそのフレームを解析します。

## ソースから実行する（開発用）

```bash
cd native
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python app/main.py              # アプリのウィンドウで起動
python app/main.py --browser    # 既定のブラウザで開く
python app/main.py --selftest   # 合成動画で動作確認
```

## Web 版を更新したとき

`index.html`（リポジトリ直下）を更新して push すれば、ビルド時に `tools/patch_index.py` が自動でネイティブ版用の画面を作り直します。手元で作り直す場合：

```bash
python tools/patch_index.py ../index.html app/web/index.html
```

パッチ箇所が見つからないとエラーで止まるので、その箇所だけ `tools/patch_index.py` を直してください。パッチ後の `index.html` は、ブラウザで直接開けば従来どおり OpenCV.js 版として動きます。

## 構成

```
native/
  app/main.py         起動（pywebview のウィンドウ / --browser / --selftest）
  app/server.py       ローカル API サーバ（127.0.0.1 のみ、起動ごとのトークン付き）
  app/tracker.py      解析エンジン（Web 版の trackVideo / trackFrame / refineCenter の移植）
  app/selftest.py     動作確認（既知の動きをする合成動画で誤差を確認）
  app/web/index.html  画面（Web 版 index.html にパッチを当てたもの）
  tools/patch_index.py
  build.spec          PyInstaller 設定
.github/workflows/build-native.yml  macOS / Windows の自動ビルド
```

## ライセンス上の注意

配布版には FFmpeg（PyAV 同梱）と x264（GPL）が含まれます。研究室内での利用には問題ありませんが、外部に配布する場合はこれらのライセンス条件（ソースコードの提供など）に従ってください。OpenCV は Apache 2.0（`LICENSE-opencv.txt`）です。
