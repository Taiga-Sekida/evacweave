#!/usr/bin/env bash
# 提出前の一括手順（Cloud Shell用）。Geminiの実使用までを順番に行います。
#
#   1. テスト（課金なし）
#   2. 最小権限のサービスアカウントとカスタムロール（setup_gemini_iam.sh）
#   3. 非公開エージェント（Vertex AIのGeminiが方策）をデプロイ
#   4. Geminiの診断（1回）→ 成功したら1回実行して記録
#      → data/evidence/recorded_gemini_run.json                         ※課金あり・確認あり
#   5. 公開サービスを「Geminiでライブ実行」有効でデプロイ（記録も同梱）
#   6. 公開URLからライブ実行と報告の読み取りを1回ずつ確認                 ※課金あり・確認あり
#
# 課金のある手順は、yes と入力したときだけ実行します（CONFIRM_PAID=yes で入力を省略）。
# LIVE_AI_DAILY_RUNS（既定40）で、公開画面からの1日のライブ実行・報告の読み取り・住民の事情の読み取りの合計回数を制限します。
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"
PROJECT="physical-ai-guardian"
REGION="${GCP_REGION:-asia-northeast1}"
if [[ "${GCP_PROJECT_ID:-${PROJECT}}" != "${PROJECT}" ]]; then
  echo "別プロジェクトには実行しません: ${GCP_PROJECT_ID}" >&2
  exit 2
fi

step() { printf '\n==== %s ====\n' "$1"; }
paid_ok() {
  if [[ "${CONFIRM_PAID:-}" == "yes" ]]; then return 0; fi
  local answer
  read -r -p "$1（課金あり）。実行するには yes と入力: " answer
  [[ "${answer}" == "yes" ]]
}
service_url() {
  gcloud run services describe "$1" --project "${PROJECT}" --region "${REGION}" --format='value(status.url)'
}

step "1/6 テスト"
python3 -m pytest -q

step "2/6 サービスアカウントとロール"
./scripts/setup_gemini_iam.sh

step "3/6 非公開エージェント（Vertex AI Gemini）"
./scripts/deploy_cloud_run_orchestrator.sh
PRIVATE_URL="$(service_url disaster-agentic-orchestrator)"

step "4/6 Geminiの診断（1回）と記録（最大6回）"
if paid_ok "Geminiを診断で1回呼び、成功したら最大6回呼んで再生検証に通った実行を記録します"; then
  if ! CONFIRM_PAID=yes python3 scripts/diagnose_gemini.py --service-url "${PRIVATE_URL}" --confirm-paid-call; then
    echo "診断で失敗したため、記録は行いません。表示された原因を直して再デプロイしてから、この手順をやり直してください。" >&2
    echo "（同じ版・同じモデルで失敗した診断は、診断スクリプトが繰り返しません）" >&2
  elif ! python3 scripts/record_gemini_run.py --service-url "${PRIVATE_URL}" --confirm-paid-call; then
    echo "記録できませんでした。表示された run_id の Cloud Logging（gemini_call_failed）で原因を確認してください。" >&2
  fi
else
  echo "記録を省略しました。"
fi

step "5/6 公開サービス（ライブ実行を有効化）"
LIVE_AI_ENABLED=1 ./scripts/deploy_cloud_run_mvp.sh
PUBLIC_URL="$(service_url disaster-agentic-mvp)"

step "6/6 公開URLからライブ実行と報告の読み取りを確認"
if paid_ok "公開URL経由でGeminiを最大7回呼び出します（実行1回・読み取り1回）"; then
  python3 scripts/check_live_ai.py --public-url "${PUBLIC_URL}" --confirm-paid-call || \
    echo "ライブ実行を確認できませんでした。上の表示と非公開サービスのログを確認してください。" >&2
fi

cat <<EOF

完了。公開URL: ${PUBLIC_URL}/ （報告文を書いて「Geminiエージェント出動」）、${PUBLIC_URL}/slides?shot=9 （「Geminiでライブ実行」）

次の手順（審査対象はデフォルトブランチです）:
  git add data/evidence/recorded_gemini_run.json
  git commit -m "evidence: record a Gemini run on Vertex AI"
  git push
  # PRをmainへマージした後、mainから公開サービスを再デプロイ（課金なし）:
  #   git checkout main && git pull && LIVE_AI_ENABLED=1 ./scripts/deploy_cloud_run_mvp.sh
EOF
