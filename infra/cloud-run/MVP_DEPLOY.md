# Cloud Runへのデプロイ

公開サービスと非公開サービスを、Cloud Shell（または `gcloud` で認証した端末）からデプロイします。スクリプトの既定は、プロジェクト `physical-ai-guardian`、リージョン `asia-northeast1`（東京）です。別のプロジェクトに置くときは `GCP_PROJECT_ID` などで上書きします（非公開サービスのスクリプトは、想定外のプロジェクトへのデプロイを断ります）。

## 手順

```bash
gcloud config set project physical-ai-guardian
./scripts/setup_gemini_iam.sh                 # 初回だけ：サービスアカウントとカスタムロール（キーは作らない）
./scripts/deploy_cloud_run_orchestrator.sh    # 非公開サービス（Geminiを呼ぶ側）
LIVE_AI_ENABLED=1 ./scripts/deploy_cloud_run_mvp.sh   # 公開サービス（画面とAPI）
```

`LIVE_AI_ENABLED` を付けなければ、公開サービスはGeminiを使わずに動きます（「AIなしで比較」と一括検算だけ）。どちらのスクリプトもGeminiを呼びません。

## 設定

| 項目 | 公開サービス（`disaster-agentic-mvp`） | 非公開サービス（`disaster-agentic-orchestrator`） |
|---|---|---|
| 公開範囲 | 認証なし | Cloud Run IAMのみ（`--no-allow-unauthenticated`） |
| 実行アカウント | `evacweave-public-sa`（プロジェクトのロールなし、Vertex AIの権限なし） | `guardian-agent-sa`（カスタムロール `guardianVertexGenerate` だけ） |
| 台数・同時処理 | 最小0・最大1、同時4件 | 最小0・最大1、同時2件 |
| タイムアウト | 90秒（Geminiを使う依頼が非公開サービスを待つため） | 60秒 |
| CPU・メモリ | 1 vCPU／512 MiB、リクエスト外はCPUを絞る | 1 vCPU／512 MiB |
| 主な環境変数 | `LIVE_AI_ENABLED`、`LIVE_AI_DAILY_RUNS`（既定40）、`PUBLIC_SIMULATION_ENABLED=0` | `VERTEX_GEMINI_MODEL`（既定 `gemini-3.5-flash`）、`GEMINI_PROFILE`、`DECISION_PROFILE`、`GOOGLE_CLOUD_LOCATION` |

- 公開サービスが非公開サービスを呼べるのは、そのサービスに対する `roles/run.invoker` だけです（デプロイスクリプトが付与します）。
- Geminiの1日の回数は、非公開のCloud Storageバケットに記録します（`scripts/setup_live_quota.sh`、公開サービスのデプロイ時に自動で実行）。
- 1つのコンテナイメージで両方のサービスを動かし、`APP_MODULE` で切り替えます（`Dockerfile`）。

## デプロイ後の確認

公開サービスのスクリプトは、デプロイ直後に次を確かめます。失敗したときは、前の版に戻すコマンドを表示します。

- `/health` が、いまデプロイした版を返すこと
- `/v1/showcase` の内容（エージェントの結論、記録したGeminiの実行の再生検証）
- 画面（`/`、`/slides?shot=9`、`/lab`、`/api-guide`）が開くこと
- `/v1/agent/live` が、Geminiの有効・無効を正しく返すこと

手で確かめる場合：

```bash
URL="$(gcloud run services describe disaster-agentic-mvp --region asia-northeast1 --format='value(status.url)')"
curl -fsS "$URL/health"
curl -fsS "$URL/v1/demo/blocked"    # 合成シナリオ：FAIL
curl -fsS "$URL/v1/demo/repaired"   # 合成シナリオ：PASS
```

## デプロイに必要な権限

ソースからのデプロイ（`gcloud run deploy --source`）には、デプロイする人に Cloud Run Source Developer と Service Usage Consumer、実行アカウントに対する Service Account User が必要です。ビルド用のサービスアカウントには Cloud Run Builder が必要です。IAMのエラーが出たときは、エラーに書かれた対象を確かめてから、このプロジェクトの範囲で付与してください。

サービスアカウントのJSONキーは作らず、リポジトリにも置きません。費用の上限と緊急時の止め方は [COST_GUARD.md](COST_GUARD.md) を参照してください。
