# 公開サービス（`apps.api.main:app`）

Cloud Run上の公開の入口です。ログイン不要で、合成データだけを扱います。Vertex AIの権限は持たず、Geminiが必要な依頼は、IAMで保護した非公開サービス（`apps/orchestrator_service`）に送ります。

| ファイル | 役割 |
|---|---|
| `main.py` | 画面とAPIの入口、セキュリティヘッダー、送り元ごとの回数制限 |
| `live_agent.py` | 非公開サービスの呼び出しと、返ってきたGeminiの判断の再生検証 |
| `live_quota.py` | Geminiの1日の利用回数（Cloud Storageに記録） |
| `rate_limit.py` | 送り元ごとの回数制限（429） |
| `labels.py` | 道路・避難所の日本語名 |

## 画面

- `GET /` — プレイ画面（`?shot=N` 付きは `/slides` へ転送）
- `GET /slides` — 仕組みの説明（`?shot=1..10`）
- `GET /lab` — 2D避難ラボ
- `GET /api-guide` — API一覧（`/openapi.json` も公開）

## API

| 方法とパス | 内容 | Gemini |
|---|---|---|
| `GET /health` | 稼働確認 | — |
| `GET /v1/showcase` | 仕組みの説明画面のデータ（Coreで計算し直したもの） | — |
| `GET /v1/demo/{blocked,repaired}` | 固定シナリオの判定 | — |
| `POST /v1/simulate` | 任意のシナリオと計画の判定（既定で無効。64KiBと計算量の上限つき） | — |
| `POST /v1/agent/challenge` | 道路を閉じた世界で、ルール方策のエージェントを動かす | — |
| `GET /v1/agent/care` | 住民の事情メモと避難所の設備（`?pattern=0..29` で住民を入れ替え） | — |
| `GET /v1/agent/care/sweep` | 30通りの一括検算と、足りない設備の提案 | — |
| `POST /v1/agent/care` | Geminiによる事情メモの読み取り | 1回 |
| `POST /v1/agent/read` | Geminiによる報告文の読み取り（通れない道路） | 1回 |
| `GET /v1/agent/live` | Geminiが使えるか・残り回数 | — |
| `POST /v1/agent/live` | Geminiを方策にしたエージェントの実行（再生検証して返す） | 最大6回 |

Geminiを使う依頼は、1日の上限・同時1件・送り元ごとの回数制限を通ってから非公開サービスに届きます。詳しくは [SECURITY.md](../../docs/SECURITY.md) と [COST_GUARD.md](../../infra/cloud-run/COST_GUARD.md) を参照してください。
