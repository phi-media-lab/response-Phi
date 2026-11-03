# P1 冻结批次准备工具

`scripts/prepare_decode_cost_batch.py` 只准备来源与运行计划，不导入模型、不运行进程、不执行 GPU。它从一个已冻结的 decode-cost attempt 复制完整 payload，并分别生成六条件 smoke 或单形状的 36-cell Williams 矩阵。

```sh
python3 scripts/prepare_decode_cost_batch.py \
  --template /absolute/path/to/frozen-attempt \
  --output-dir /absolute/path/to/new-batch \
  --mode smoke --a-prompt-length 64 --a-decode-steps 40 \
  --b-prompt-length 1024 --arrival-after-a-decode 4
```

`--mode matrix` 固定六区组，每格 1 warmup＋1 measured trial。首行 `[0,1,5,2,4,3]`，另外五行按编号 mod6 平移；seed20261029 只重排完整区组。各位置各策略一次，区组内每个有序相邻策略对一次。smoke 固定六策略各一次、无 warmup。准备工具不自行选择或调整策略参数。

## 来源和执行边界

- 验证模板 manifest/prepared 身份及所有 payload 文件的完整 hash，拒绝文件和目录 symlink；复制后检查完整 inventory 与每份字节。使用独立复制，不使用 hardlink。
- batch root 保存原模板 manifest/prepared 的原始字节。每个新 attempt 保存 template hash、plan hash、cell 身份和继承的捕获时间。归档后不用回到原绝对路径才能重算派生关系。
- 仅改每格 run_spec、创建时间、派生说明与 scope。模型、token fixture、harness、helper、依赖源码和生成模板字节保持一致。
- 外部 runtime/model 身份从模板继承，**准备阶段不重新 hash 外部文件**，也不把原捕获时间写成当前捕获。每次实际执行仍使用该 attempt 中冻结的 wrapper，运行前后完整核验所有外部字节及实际导入来源。
- `prepared-index.json` 保存每格的准确执行 argv；`execution_exists:false` 是准备时的状态，后续不回填修改它。实际执行按固定 cells 顺序串行，失败或完成未知即停止，不自动重试覆盖。

准备成功只表示复制与计划建立，不表示模型正确性或性能验收。root 仍按[扩展计划](P1_DECODE_EXTENSION_PLAN.md)依次检查小 shape 六条件、S0 六条件，再运行 S0 正式矩阵。其余七个形状/到达组合仍有自己的 smoke 与六区组矩阵，不能用 S0 代替。

## 已验证的工具性质

11 项 CPU 测试覆盖位置/有序相邻对平衡、独立重复计划、六 smoke 工作数、形状/到达边界、完整复制与来源继承、没有隐藏的外部字节读取、源篡改、已有目录、目录 symlink 和部分复制失败记录。

正式记录从执行前冻结的两份源码运行：[source/manifest](../experiments/tiny-decode-cost/verification/batch-preparation-v1/manifest.json)、[execution](../experiments/tiny-decode-cost/verification/batch-preparation-v1/execution/run.json)。早期 working-source 测试 v1/v2 保留，不借正式记录倒推其历史来源。测试使用临时文件 fixture，没有模型或 GPU 工作。
