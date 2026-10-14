# 提出準備

第5回 Agentic AI Hackathon with Google Cloud への提出物と、提出の手順です。フォームの各欄にそのまま貼る文章は [FORM.md](FORM.md) にあります。

## 日程

| 日付 | 内容 |
|---|---|
| **2026-10-15（木）23:59** | 参加登録・プロジェクト提出の締切 |
| 10月中旬〜下旬 | 1次審査・2次審査（審査ごとに全プロジェクトへ結果の連絡） |
| 2026-10-28（水） | ファイナリスト発表 |
| 2026-12-01（火） | 最終ピッチコンテスト・表彰式（渋谷ストリーム） |

## 審査期間中のルール（12/1まで）

- 連携したGitHubリポジトリの**デフォルトブランチ（`main`）を提出時点のまま保つ**。
- **デプロイを更新しない**。公開URLは動作確認できる状態のまま保つ。
- 提出後も開発を続けるときは、**別のブランチ**で行う。

このため、提出の前に、`main` と公開中のデプロイを同じ版にそろえておきます（下の手順2）。

## 提出物

| 提出物 | 内容 | 状態 |
|---|---|---|
| GitHubリポジトリ | 公開リポジトリ https://github.com/Taiga-Sekida/evacweave をダッシュボードのGitHub Appから連携。審査はデフォルトブランチ | 連携する |
| デプロイURL | https://disaster-agentic-mvp-66ttczdcza-an.a.run.app/ （ログイン不要） | 稼働中 |
| 説明文 | [FORM.md](FORM.md) | 済 |
| アーキテクチャ図 | `architecture/evacweave-architecture.png`（1600×900） | 済 |
| プロジェクトイメージ | `images/evacweave-project.png`（1600×900） | 済 |
| デモ動画 | 約3分、YouTubeで公開（[台本](VIDEO_SCRIPT.md)） | 撮影中 |
| 必須技術 | 実行基盤：Cloud Run（公開・非公開の2つ）／AI：Vertex AIのGemini 3.5 Flash | 済 |
| OSSライセンス | [NOTICE.md](../NOTICE.md) | 済 |

## 手順

1. **作業ブランチを `main` に取り込む**（GitHubでPRをマージ）。
2. **`main` からデプロイし直す**（Cloud Shell）。`/health` の `revision` が `main` の先頭コミットと同じになれば、審査対象のコードと公開URLがそろいます。
   ```bash
   cd ~/google-cloud-agentic-vol5 && git fetch -q origin main && git checkout -q --detach FETCH_HEAD && git log --oneline -1 && gcloud config set project physical-ai-guardian && ./scripts/deploy_cloud_run_orchestrator.sh && LIVE_AI_ENABLED=1 ./scripts/deploy_cloud_run_mvp.sh
   ```
3. **動作を確かめる**：トップのプレイ画面で「AIなしで比較」と「100人で一括検算」が動くこと（無料）。Geminiのボタンは1日の回数を使うので、必要なときだけ押します。
4. **デモ動画をYouTubeで公開**する。
5. **大会ダッシュボードから提出**する。
   1. [ダッシュボード](https://zenn.dev/hackathons/google-cloud-japan-ai-hackathon-vol5/dashboard)を開く
   2. 作成したプロジェクトを開く
   3. 提出物（GitHubリポジトリの連携・説明・アーキテクチャ図・デモ動画など）を登録する
   4. 提出タブで「提出する」を押す（プロジェクトの「管理者」だけができます）
6. **提出後は `main` とデプロイに触れない**（12/1まで）。開発は別のブランチで続けます。費用の監視と止め方は [COST_GUARD.md](../infra/cloud-run/COST_GUARD.md) にあります。

## 表示で守ること

- 「AIが判断した」と言えるのは、Geminiのボタンを押した結果と、場面9のGeminiのタブだけです。「AIなしで比較」・挑戦モード・場面9の既定のタブはルール方策、ラボは遺伝的アルゴリズム、一括検算は決定論の計算です。
- PASSは合成データ上の計算結果で、現実の安全を保証しません。

## そのほかの資料

- [VIDEO_SCRIPT.md](VIDEO_SCRIPT.md)：動画の台本
- [BACKEND_QA.md](BACKEND_QA.md)：審査員に構成を聞かれたときの答え方
- [IMAGE_CAPTURE.md](IMAGE_CAPTURE.md)：画面の画像の自動撮影
