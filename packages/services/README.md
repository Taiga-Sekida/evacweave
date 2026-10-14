# Services

外部サービス境界。

- `budget.py`：1実行あたりの呼出し回数の上限（失敗しても返却しない）。Cloud Billingの予算の代わりではない。
- `gemini_attempts.py`：Cloud Shellから行った有料のGemini呼び出し（診断・記録）の手元の記録と、失敗した呼び出しをそのまま繰り返さないための規則。`scripts/diagnose_gemini.py` と `scripts/record_gemini_run.py` が使う。プロンプト・回答・トークンは残さない。
- `http_json.py`：Gemma経路（`packages/agents/gemma_provider.py`）用の上限付きJSONトランスポートとGoogle IDトークン取得。デプロイ済みのサービスには接続していない（今後の工程）。公開サービスから非公開サービスへの呼び出しは `apps/api/live_agent.py` が行う。
- `structured_log.py`：Cloud Loggingが構造化エントリとして取り込む1行JSONログと、リクエストのトレース相関。

Vertex Gemini のトランスポートは `packages/agents/orchestrator.py` にあり、SDKは遅延import。通常のテストでは実APIを呼ばない。
