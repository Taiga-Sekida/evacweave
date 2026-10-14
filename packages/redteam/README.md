# Red Team

`probes.py` は、合格した計画が通る道路を1本ずつ全時間閉鎖して再実行する**決定論的な**単一障害点の探索です。学習済みのRed Team AIではありません。

- `with_road_closed(scenario, road_id)`：1本の道路を全時間閉鎖した世界を作り、契約の検証をやり直す。
- `single_road_faults(scenario, plan)`：各閉鎖でCoreを実行し、`FaultProbe` を返す（道路ID順で安定）。

FAILになった閉鎖への代替計画は `packages/repair/contingency.py` が作ります。
