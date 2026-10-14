# Cloud Runの設定

| ファイル | 内容 |
|---|---|
| `mvp-api.yaml` | 公開サービス（`disaster-agentic-mvp`）の設定。最小0・最大1インスタンス |
| `MVP_DEPLOY.md` | 公開・非公開サービスのデプロイ手順（Cloud Shellから） |
| `COST_GUARD.md` | 費用の上限と、緊急時の止め方 |
| `ORCHESTRATOR_SECURITY.md` | 非公開サービス（Geminiを呼ぶ側）の権限とIAM |
| `gemma-gpu.yaml` | Gemmaの推論サービスをGPUで動かすときの設定案（今後の工程。デプロイしていない） |

## `gemma-gpu.yaml` の前提（今後の工程）

- リージョンは `asia-southeast1`（Cloud RunでL4 GPUを使えるため）。L4を1枚、4 vCPU、16 GiB、同時1件、最大1インスタンス。
- 最小インスタンスは指定せず、使わないときは0台に戻す。費用を抑えるため、GPUのゾーン冗長は無効にする（そのぶん可用性は下がる）。
- 非公開のまま使い、呼び出し側はサービスのオリジンを宛先にしたGoogleのIDトークンで呼ぶ。
- モデルとアダプタはコンテナの `/models` に置く。重みはリポジトリに入れない。

実際にデプロイする前に、`PROJECT_ID` と `IMAGE_TAG` を置き換え、GPUの割り当てと予算、Cloud Run GPUの最新の文書を確認してください。GPUは起動している間ずっと課金されるため、CIからは自動でデプロイしません。
