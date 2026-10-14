# EvacWeave — 提出用画像10枚の作成手順

画面とデータを別々に手作業で作らず、**稼働中の公開アプリから自動撮影**する方式です。

## GitHub Actionsからの自動作成

`.github/workflows/capture-submission.yml` は、提出用の10場面を自動撮影し、`evacweave-submission-images` という成果物（ZIP）として保存します。CIはローカルで実際にAPIを起動して画面を描画するため、撮影物は動作実装の証拠として利用できます。

ただしCIで作った画像はCloud Run上の実行画面ではありません。**最終提出には、公開Cloud Runを最新版に更新してから再撮影する**ことを推奨します。

## 1. 画面の確認

最新版をCloud Runへ反映した後、下記URLで10場面を閲覧できます。

https://disaster-agentic-mvp-66ttczdcza-an.a.run.app/slides?shot=1

番号1〜10をURL末尾で指定できます（`/slides?shot=N`）。トップ（`/`）はプレイ画面で、撮影スクリプトは `00-play.png` として初期状態も保存します。

## 2. 画像をまとめて撮影

Python 3.12以降の端末またはCloud Shellで、リポジトリ直下から実行します。

\`\`\`bash
python -m pip install playwright
python -m playwright install chromium
python scripts/capture_submission.py \
  --base-url https://disaster-agentic-mvp-66ttczdcza-an.a.run.app \
  --out artifacts/submission/images
\`\`\`

ローカル動作確認では、別ターミナルで \`uvicorn apps.api.main:app --host 127.0.0.1 --port 8000\` を起動し、\`--base-url http://127.0.0.1:8000\` を使います。

**注意:** 旧公開版（リビジョン \`448ca82\`）には \`/v1/showcase\` がないため、画像は作れません。最新版デプロイ後に実行してください。

デプロイ済みの版を確認したい場合は \`--expected-revision\` を指定できます。

自動撮影では、10枚の画面画像に加えて、提出フォームの「アーキテクチャ図」欄用の `architecture-diagram.png` も作成します。編集可能な原図は `submission/architecture/evacweave-architecture.html`（1600×900）で、描画済みの画像は `submission/architecture/evacweave-architecture.png` です。

## 3. 撮影対象と証拠の区分

| 番号 | 画像の見出し | 証拠の扱い |
|---|---|---|
| 01 | EvacWeave — AIの提案を、確かな検証へ | 紹介用の構成表現 |
| 02 | 避難計画を検証する公開画面 | 実稼働画面、計算結果 |
| 03 | 道路閉鎖による避難計画の不成立 | Simulation Coreの実計算 |
| 04 | 避難できなかった原因の確認 | 実行イベントから表示 |
| 05 | 代替経路による避難計画の改善 | コードによる修復候補・再検証 |
| 06 | 住民の移動経過と避難完了 | 実行イベントから表示 |
| 07 | 判断する役割と、検証する役割を分ける | 実装済みのエージェント構成図 |
| 08 | 公開処理と非公開AI処理の分離 | アーキテクチャ図 |
| 09 | 観測し、判断し、実行し、検証する | エージェントの実行記録（基本シナリオ）。**公開画面の判断はルール方策（AIではない）**。川沿いの地区のタブ（引き継ぎ事項）は自動撮影の対象外なので、必要なら手動で撮影 |
| 10 | 役割別AIの学習と活用構想 | **未本番接続の将来構想** |

画像の末尾には「合成シナリオによる研究用試作／現実の安全や避難判断を保証しません」と表記します。

\`manifest.json\` に、画像ごとのファイル名、SHA-256、参照URL、根拠となるシミュレーション入力のダイジェスト、撮影日時、リビジョンを保存します。画像だけが流通しても、実装済みか計画かを復元できるように区分しています。

## 4. 提出前の確認

- \`/health\` が \`status=ok\` を返すこと。
- \`/v1/showcase\` の \`baseline_result.status=FAIL\` と \`repaired_result.status=PASS\` を確認すること。
- \`live_ai_used=false\` は公開デモの計算部分を指します。AI推論の実演と混同しないこと。
- 画像9（自動撮影）の判断はルール方策です。Geminiの判断として見せるのは、画面のGeminiタブ（ライブ実行、または再生検証に通った `data/evidence/recorded_gemini_run.json`）を選んだ状態だけです。Geminiの画像が必要なら、ライブ実行の後に手動で撮影してください。修復・弱点探索・代替計画は有界なアルゴリズムで、学習済みAIの成果とは表記しないこと。
- 画像10は今後の学習・推論接続の設計図です。
- 非公開Cloud RunのIAM設定は公開スクリーンショットから確認できません。Geminiの実推論の証跡は、ライブ実行の `run_id` と、同じ `run_id` の非公開サービスのCloud Logging（`scripts/check_live_ai.py` がコマンドを表示）です。
