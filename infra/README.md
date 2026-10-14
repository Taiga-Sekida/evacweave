# インフラ

Cloud Runの設定と手順は [`cloud-run/`](cloud-run/README.md) にあります。

クラウドの変更（デプロイ、権限、予算）は、プロジェクト・費用の上限・サービスアカウントとIAMを確認したうえで、Cloud Shellから明示的に行います。CIからは自動でデプロイしません（`.github/workflows/deploy-cloud-run.yml` は手動実行のみ）。
