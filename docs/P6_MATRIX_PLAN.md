# P6：冻结的正常工作矩阵

2026-10-29。状态：预注册编排、11项轻CPU验证和源码冻结完成后，root已串行执行64main+64warmups，并通过全量独立检查及另一份独立复核；[结果报告](../results/batch4/control-matrix-REPORT.md)保留全部配对与限制。T16/T64四模式正确性smoke另列，性能矩阵使用同一份attempt-1 binary，不重新编译或修改算法。冻结bundle保留测量前的计划版本，本文件状态更新不改变预注册顺序。

具体算法、全量数值参考、Metal 资源契约与四模式含义见 [P6_CONTROL_PLAN](P6_CONTROL_PLAN.md)。本矩阵只测正常完成的固定 T 轮工作，`cancel_policy=none`；既有取消 smoke 不混入本矩阵。

## 固定样本与完整顺序

- T=16、64；每个 T 有 8 个区组，每区组 H/G/P/F 各一次。
- 共 64 个独立 cell/process。每进程 `--warmups 1 --trials 1`，合计 64 次主 trial、64 次 warmup。warmup 原始记录和正确性检查保留，主分布只使用 measured trial。
- 同一 frozen `metal-control-location/.build/attempt-1/metal-control-location`，binary SHA256 `3912ccf7d183769d1f156aaeb45b51ef1104eab21d7c7d4b9e13859256f7bb1b`；所有 source/build/CPU reference/checker 在运行前按字节冻结。
- 每个 block 包含两个 T 子区组。Williams 行依次为 `H G F P`、`G P H F`、`P F G H`、`F H P G`，每行重复两次；第一次 T16→T64，第二次 T64→T16。
- 使用 `random.Random(20261029).shuffle` 一次打乱这 8 个完整 paired block。每个 T 的 mode 顺序位置均衡，每个有序相邻 mode 对在区组内部出现两次；T 先后各 4 次，且在每条 Williams 行内交叉。
- 完整 64 cell 顺序事先存入 [schedule.json](../experiments/metal-control-matrix/schedule.json)，随后连同 driver/checker 复制到不可变 bundle。不会依据已观察速度改变顺序、追加次数或替换样本。

T16 与 T64 是不同工作量，分开统计。这里平衡的是区组内部位置和相邻 mode，以及每个 block 内 T 的先后；不声称所有区组边界的 carryover、温度和系统扰动已消除。随机顺序只有一个固定种子，不是多轮独立随机实验。

## 运行与失败

每 cell 用独立 run_recorded 保存精确 argv、工作目录、进程状态、超时、stdout/stderr 及其 hash。设备超时为 30 秒，外层进程超时为 180 秒；超时不是完成证明，不继续下一个 cell。

进程返回后，冻结的标准库 analyzer 复算完整 score/winner/context/final state，验证 CB/dispatch、CPU/GPU 时钟、资源归还、源 artifact 和 warmup/measured 数量。只有当前 cell 通过这些正确性检查才进入下一个。性能数值不参与 PASS 判定。

每次运行必须创建全新结果目录；在首个 GPU 进程启动前写入完整 plan/bundle 绑定。任一 cell 失败、异常或 frozen source 改变立即停止，保留已有和失败 raw、逐项 progress 与 matrix-result；不自动重试、不续跑、不静默删去失败样本。需要修订则另建版本和新计划，由 root 决定。

所有设备工作串行。独立 checker 的 CPU 工作发生在 cell 之间，可能形成自然间隔；不使用 sleep 伪造冷却，不将间隔当恒定热状态。Swift/Metal 管线编译/准备与进程启动发生在 work_ms 之外，warmup 在同一进程内完成后才测主 trial。模式专属 command 编码仍在 work_ms 内，不减掉 P 的预编码成本。

## 预定分析

主对照是 G `gpu-step` ↔ P `gpu-preencoded`：同一 T、同一 block_id 的主 trial 配对。先逐对计算 `P.work_ms − G.work_ms` 和 `P.work_ms / G.work_ms`，再报告 8 对差值/比值的中位数与范围，保留每对原值；不能用两个边际中位数相减冒充配对中位数。

各模式同时保留 encoding_ms、first_submit_to_completion_ms、gpu_execution_sum_ms、gpu_span_ms、CPU selection 和完整 trial/validation 时间。它们存在重叠和不同起点，不能直接相加。G/P 相同 3T kernel 调用与逻辑全局访问只支持“固定可预知操作图的包络/host 往返”解释，不证明物理 DRAM 流量相等。

H `cpu-feedback` 与 F `device-loop-global` 单独报告：H 改变 CPU 真实读取/选择和 CB/dispatch 数；F 改变 kernel 融合、barrier 与调度边界。即使出现速度差，也不能全部归因于 continuation 位置，更不能叫 device Future 或 agent/LLM 端到端收益。

这是每配置 8 次的小型 pilot；用中位数、范围和完整原始分布，不报告稳定 p95 或最坏响应界。没有“必须加速”的验收阈值。取消不在矩阵中，正常工作耗时不能直接换算为取消响应性收益。
