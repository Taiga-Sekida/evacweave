# 権利・来歴・第三者ライセンス

## 提出物の権利（参加規約 第5条・第6条）

- ソースコード、合成データ（`data/`）、画面（`apps/web/`）、構成図（`submission/architecture/`）は、参加者が独自に制作したものです。著作権・知的財産権は参加者に帰属します（第6条1項）。
- 主催者等には、第6条2項に定める審査・評価・プロモーション目的に限り、提出物の使用権を無償で付与します。
- リポジトリ全体に対する公開ライセンス（OSSライセンス）は**未選定**です。`LICENSE` を置くまでは、上記の使用許諾を除き、第三者に複製・再配布の権利を付与していません。
- 個人情報、実在の災害・地図データ、第三者の画像・フォント・データセット、学習済みモデルの重みは同梱していません。画面のフォントはOS標準のフォントを名前で参照するだけで、ファイルは含みません。

## 利用しているオープンソースソフトウェア（第5条4項）

各ソフトウェアは、それぞれのライセンス条件に従って利用しています。表記はPyPIのパッケージ情報によるもの（PyPIに表記のないtorchaoのみ、上流リポジトリのLICENSE本文）で、版は `requirements-*.txt` の指定または2026-10-12時点の確認結果です。

### 公開・非公開Cloud Runサービス（`requirements-api.txt` / `requirements-cloud.txt`）

| パッケージ | ライセンス |
|---|---|
| pydantic / pydantic-core | MIT |
| annotated-types / typing-inspection | MIT |
| typing-extensions | PSF-2.0 |
| fastapi | MIT |
| starlette（fastapiが依存） | BSD-3-Clause |
| uvicorn | BSD-3-Clause |
| google-genai | Apache-2.0 |
| google-auth | Apache-2.0 |

### テスト・開発（`requirements-dev.txt`）

| パッケージ | ライセンス |
|---|---|
| pytest / pluggy / iniconfig | MIT |
| Pygments | BSD-2-Clause |
| packaging | Apache-2.0 または BSD-2-Clause |
| httpx | BSD-3-Clause |
| colorama（Windowsのみ） | BSD-3-Clause |
| ruff / mypy / mypy_extensions / librt / pytest-cov | MIT |
| coverage | Apache-2.0 |
| pathspec（mypyが依存） | MPL-2.0 |

### 提出画像の自動撮影（CI／`scripts/capture_submission.py`、依存ファイルには含まない）

| パッケージ | ライセンス |
|---|---|
| playwright | Apache-2.0 |

### 学習・GPU推論の予定経路（`requirements-gpu.txt`、Colab。**現時点で本番には組み込んでいません**）

| パッケージ | ライセンス |
|---|---|
| transformers / accelerate / peft / sentencepiece / datasets / evaluate / tensorboard / trl | Apache-2.0 |
| bitsandbytes | MIT |
| protobuf / torchao | BSD-3-Clause |
| torch | 複合（PyPI表記: Apache-2.0 AND BSD-2-Clause AND BSD-3-Clause AND BSL-1.0 AND MIT ほか） |

### 注意が必要な間接依存

- certifi（httpx・google-auth等が間接的に利用）と pathspec（開発時のみ）は **MPL-2.0** です。いずれも改変せずに利用しています。
- 間接依存を含む全件は、依存をインストールした環境で次を実行すると一覧できます。

```bash
python scripts/list_licenses.py
```

## Google Cloudサービスとモデル

- Cloud Run、Vertex AI（Gemini）は各サービスの利用規約に従って利用します。Geminiは非公開のオーケストレータからのみ呼び出します。公開画面のライブ実行は、IAMで認証してその非公開サービスに依頼するもので、公開サービス自身はVertex AIを呼びません。
- **Gemma** の重みはリポジトリに含みません。学習・推論の工程は今後の予定で、利用時は [Gemma利用規約](https://ai.google.dev/gemma/terms) に従います。
- 実行環境のベースイメージは `python:3.12-slim`（Python: PSF License、OSパッケージ: 各パッケージのライセンス）です。
