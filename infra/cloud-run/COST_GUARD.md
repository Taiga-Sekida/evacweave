# EvacWeave — Cloud Runの費用と公開アクセスの制限

対象Project: `physical-ai-guardian`、リージョン: `asia-northeast1`。

## 運用ポリシー

| 設定 | 公開Web/API | 非公開Vertexオーケストレータ |
|---|---|---|
| 公開アクセス | 可（ログイン不要） | 不可（Cloud Run IAM） |
| 最小インスタンス数 | 0 | 0 |
| サービス最大インスタンス数 | 1 | 1 |
| 同時実行数 | 4 | 2 |
| 最大リクエスト時間 | 90秒（ライブ実行の待ち時間） | 60秒（Geminiは1回25秒、45秒以降は送らない） |
| CPU | 1 vCPU | 1 vCPU |
| メモリ | 512 MiB | 512 MiB |
| CPU課金方式 | リクエスト処理時優先（throttling有効） | throttling有効 |
| 認証なしの任意シミュレーション | **既定で無効** | 外部公開しない |
| 実行時のサービスアカウント | `evacweave-public-sa`（プロジェクトのロールなし。回数を数えるバケットだけ読み書き可） | `guardian-agent-sa`（`guardianVertexGenerate` のみ） |
| 有料モデル呼出し | **直接は呼ばない**。ライブ実行と報告の読み取り（既定で無効）は非公開サービス経由、同時1件・合わせて1日 `LIVE_AI_DAILY_RUNS` 回（既定40） | 認証済み、1実行最大6回（エージェントの1ステップ1回）、1読み取り1回 |

公開の `POST /v1/simulate` は `PUBLIC_SIMULATION_ENABLED=0` で403を返します。公開ダッシュボードで利用する `GET /`、`GET /v1/showcase`、`GET /v1/demo/blocked`、`GET /v1/demo/repaired` は引き続き利用できます。開発者がローカルで任意の合成シナリオをテストしたい場合に限り、環境変数を `1` に設定して起動できます。これを公開サービスへ設定しないでください。

## 既存サービスへの適用

デプロイスクリプトは、毎回この表と同じ制限を設定します。デプロイし直さずに設定だけをそろえるときは、リポジトリのディレクトリで次を実行します。

```bash
./scripts/apply_cloud_run_limits.sh
```

非公開オーケストレータがあれば、そちらにも適用します。公開サービスのURLは変わりません。

## 絶対に設定する課金側の保護

[Google Cloudコンソールの請求先アカウント → 予算とアラート](https://console.cloud.google.com/billing)で、「**Spend cap enforcement（支出上限の適用）**」を選択してください。「通知のみ」では利用停止しません。

利用額上限は**プロジェクトと対象サービスの組合せごと**です。

1. Project `physical-ai-guardian`、Service `Cloud Run` に月間上限を設定
2. 同じProject、Service `Vertex AI` に**別の**月間上限を設定
3. 対象金額は許容できる実費を考慮して決め、50%、80%、100%通知を確認

どちらの支出上限も、このProjectで動く**同一サービスのすべてのリソースに適用**されます。たとえばCloud Runの上限に到達すると、公開MVPだけでなく非公開Cloud Runなど他の同一Project内Cloud Run利用も停止する点に注意してください。

この予算の集計期間は月次ですが、**上限が発動すると対象サービスの新規利用は自動復帰せず、管理者が手動で解除する必要があります**。発動後に同月内で解除した場合、その月は上限金額を引き上げない限り再発動しません。次の予算期間には再設定されます。利用停止までの計測遅延による超過分は請求されます。割引・無料クレジットは上限判定に反映されません。またCloud BuildやArtifact Registry等、対象外サービスの費用もこの上限では止まりません。利用できるかは請求アカウントの対象条件と権限にも依存します。

公式: https://docs.cloud.google.com/billing/docs/how-to/budgets-spend-caps

## Geminiのライブ実行の費用と停止

プレイ画面の「Geminiで事情を読み、計画を組み直す」（川沿いの地区）は、まず住民の事情の読み取り（Gemini1回、全員分を1回で答えるため出力は思考を含めて8192トークン以内、待ち時間は50秒まで）、報告文があればその読み取り（Gemini1回）、続けてエージェントの実行（Gemini最大6回）を行います。基本シナリオの「Geminiで計画を立て直す」は、事情の読み取りがありません。場面9の「Geminiでライブ実行」は実行だけです。出力は思考を含めて1回512トークン以内（読み取りは1024トークン以内）で、読み取りは `thinking_level=LOW`、エージェントの判断は `thinking_level=MINIMAL` です。事情の読み取り・報告の読み取り・実行はそれぞれ1日の上限 `LIVE_AI_DAILY_RUNS`（既定40）を1回ずつ使い、上限に達するとその日は429を返し、ルール方策と記録済みの実行だけを表示します。公開画面経由のGemini呼出しは、すべて実行に使った場合でも1日最大 40×6＝240回です。回数はCloud Storageの非公開バケット `physical-ai-guardian-evacweave-live-quota` に1日1つの小さなファイルとして数えるので、Cloud Runのインスタンスが入れ替わっても0回に戻りません（`apps/api/live_quota.py`）。バケットは `LIVE_AI_ENABLED=1` でデプロイするときに `scripts/setup_live_quota.sh` が作り、公開サービスのサービスアカウントにはこのバケットのオブジェクトの読み書き（`roles/storage.objectUser`）だけを付けます。回数を読み書きできないときは、Geminiを呼ばずに断ります。Cloud Run上でバケットが設定されていなければ、ライブ実行自体が無効になります。

**目安の費用（概算）：** 1回の呼出しは入力が約1,500トークン以下（システム指示と観測、または道路一覧と報告文）、出力が思考を含めて512トークン以下（読み取りは1024トークン以下）です。Gemini 3.5 Flashの単価を入力100万トークンあたり1.50ドル、出力9.00ドルとすると（第三者の価格表による。請求前に[公式の料金](https://cloud.google.com/vertex-ai/generative-ai/pricing)で確認してください）、エージェントの1回あたり最大約0.007ドル、報告の読み取り1回あたり最大約0.012ドル、事情の読み取り1回あたり最大約0.08ドル（入力約3,000トークン、出力8192トークンの上限まで使った場合）です。1日の上限40回をすべて事情の読み取りに使う最悪の場合で約3.2ドル、すべて実行に使った場合で約1.7ドルです。本番で計った事情の読み取りは18秒で、出力は上限よりずっと少ない見込みです。

ライブ実行だけを止める（画面とURLはそのまま。記録済みの実行は引き続き表示されます）:

```bash
gcloud run services update disaster-agentic-mvp \
  --project physical-ai-guardian \
  --region asia-northeast1 \
  --update-env-vars LIVE_AI_ENABLED=0
```

再開は `LIVE_AI_ENABLED=1` です。回数を変えるには `LIVE_AI_DAILY_RUNS=10` のように指定します。

上限はサービス全体で数えます。1人で使い切られないように、公開サービスは送り元ごとにも数えます（Geminiを使う依頼は10分に6回・1日9回まで、全員合わせて1時間15回まで。`apps/api/rate_limit.py`）。それでも使い切られた場合、その日はライブ実行ができず、記録済みの実行（同じ再生検証つき）が表示されます。必要なら、費用を確認したうえで `LIVE_AI_DAILY_RUNS` を一時的に上げます。

「AIなしで比較」と挑戦モード（`POST /v1/agent/challenge`）はモデルを呼ばず、組合せが有限（最大61通り）の答えをキャッシュするため、追加の費用はほぼありません。

## 非常時の公開サービス停止（URLは維持）

意図しないアクセスが発生した場合、Cloud Runの手動スケーリングを0にすれば**新規リクエストの処理を停止**できます（処理中のリクエストは完了します）。

```bash
gcloud run services update disaster-agentic-mvp \
  --project physical-ai-guardian \
  --region asia-northeast1 \
  --scaling=0
```

停止中は誰も閲覧できません。復帰するには次を実行します。

```bash
gcloud run services update disaster-agentic-mvp \
  --project physical-ai-guardian \
  --region asia-northeast1 \
  --scaling=auto \
  --min=0 \
  --max=1
```

[Cloud Runのサービス停止の公式手順](https://docs.cloud.google.com/run/docs/managing/services)も参照してください。

## 上限で防げないもの

- `max=1` を短時間超える動作が発生する場合があります。
- 少数インスタンスでも長時間の大量アクセスには課金が発生します。
- `timeout` は処理の上限であり、リクエスト数そのものの上限ではありません。
- アプリ内の403拒否もCloud Run側の受付・転送コストを完全には排除しません。
- Cloud Armorによる流量制限には外部ロードバランサ等の別構成と費用が必要です。今回の低コストMVPには導入しません。

支出額を厳密な数学的上限として保証する機能ではないので、Cloud Billingで毎日費用を監視し、異常時は上記のサービス停止を実行してください。
