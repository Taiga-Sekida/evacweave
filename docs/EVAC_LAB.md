# EvacWeave 2D Lab / 合成災害シミュレーション

## このMVPで動くもの

- 合成の道路ネットワーク（有向道路、閉鎖、移動時間）、避難所定員、住民行動を生成する
- 住民ごとの反応時間・慎重さの仮定を用い、GAで避難所と経路の割当を探索する
- `MoveIntent` 形式の行動案を生成し、決定論的な**ブラウザ参照版**イベント処理で PASS/FAIL を出す
- Canvas 2Dで住民の待機、移動、受入・拒否をアニメーション表示する
- 停止、再開、時刻スライダー、道路をクリックして閉鎖・解除、JSON出力
- 同一条件における閉鎖未考慮の最短経路とGAの比較、9件の画面内セルフテスト

## 実行

```bash
python -m pip install -r requirements-dev.txt
python -m uvicorn apps.api.main:app --host 127.0.0.1 --port 8000
```

`http://127.0.0.1:8000/lab` を開く。ビルド不要、追加のJSライブラリ不要、モデルAPI呼び出しなし。

- 「環境生成・経路探索」でスライダー設定を反映
- 地図上の道路をクリックすると、閉鎖状態を切り替えて再探索
- 「自動テスト」でJS Coreの主要な境界条件を確認
- 「JSON保存」はPython Coreに再入力できる合成 `scenario` と `plan` を保存する

GitHub CIでのJSテスト:

```bash
node --test tests_js/*.test.mjs
node tests_js/export-lab-fixture.mjs /tmp/evaclab.json
python scripts/check_lab_export.py /tmp/evaclab.json
```

ブラウザから保存したJSONも同じ `check_lab_export.py` に渡せる。Python Coreで結果を再計算し、ブラウザ側の`status`、避難人数、最終位置、イベントを照合する。実装時のPython CoreとJS参照版が食い違う場合は失敗として扱い、ブラウザ版の結果を正としていない。

## 選んだ方式と限界

[mesa/mesa](https://github.com/mesa/mesa) はPythonのABMライブラリとして調査した。今回のMVPは依存関係とサーバー課金を最小化するため自作のJavaScript GAを採用し、外部リポジトリのコードはコピーしていない。

現在の「専門AIクラスタ」の住民役は**GAによる代替行動生成**。**Gemma 4のLoRA追加学習やGPU推論と接続済みではない**。避難所・道路は合成ルールに基づく環境制約であり、未接続の情報・Red Team・修復エージェントを稼働済みと表示しない。GeminiがPASS/FAILを上書きする経路はない。

このMVPは、危険地域での実際の歩行者挙動、心理、混雑圧力、避難勧告や実測地形を再現するものではない。避難時間・安全性の予測値として使用できない。現場データによる校正・検証は別途必要。元のPython決定論的Simulation Coreを唯一の判定主体とし、フロントJSは参照モデルとして扱う。

公開の計算負荷を増やさないため、GAはブラウザのCPUで実行される。公開 API `POST /v1/simulate` は従来どおり既定で403となり、サーバー側の任意シミュレーションは有効にしない。
