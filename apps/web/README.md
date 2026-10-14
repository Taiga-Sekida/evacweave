# 公開画面（`apps/web/static`）

外部ライブラリもビルドもない、そのままのJavaScriptです。Content-Security-Policyで同じオリジンのスクリプトだけを許し、サーバーやメモの文字は必ず文字列として表示します（HTMLとして解釈しない）。

| ファイル | 画面 | 内容 |
|---|---|---|
| `play.html`・`play.js`・`play.css` | `/`（トップ） | プレイ画面。Geminiで事情を読んで計画を組み直す、メモの書き換え、通行止め、住民の入れ替えと一括検算 |
| `index.html`・`app.js`・`app.css` | `/slides` | 仕組みの説明（10場面）。`?shot=N` で場面を指定 |
| `map-view.js` | 両方 | 地図・住民の移動・エージェントの記録の描画（共通部品） |
| `evac-lab.html`・`evac-lab-*.mjs`・`evac-lab.css` | `/lab` | 2D避難ラボ（ブラウザ内で完結し、サーバーの計算もAIも使わない） |
| `api.html` | `/api-guide` | API一覧 |

画面に出す数字は、すべてサーバーのAPIが返した値（Simulation Coreの計算結果）です。画面の中で判定を作ることはしません。画面の画像は `scripts/capture_screens.py` で自動撮影できます（Playwright）。
