# SRC1 B 收敛：十个父子组合情境首次执行

2026-11-03。落实建议 4 的第二步。此前这十个受检父程序组合情境（B-C01 到 B-C10）从未执行过，只有源码和源码级验收；oracle 修正虽获验收但从未合入运行时或编译。本轮把 oracle 修正合入规范运行时、重建、首次串行执行这十个 CPU 情境。结果先行：十个情境的组合逻辑全部通过检查，但其中八个在宿主收尾阶段挂起，这是一个此前无人见过的真实集成缺口。

## 1. 做了什么

- 把 `composition-scope-oracle-root-source-acceptance-v1` 已验收但未合入的 `ParentCompositionControls.swift`（after 版，sha256 `ee6ea824…`）合入规范运行时，替换 before 版（`cefe9f4c…`）。变更记录在 `runtime/CHANGES.json`，源 diff 与验收记录字节已固定。
- 用 CM2 的构造器生成十个情境的父源、子源与两个响应变体源（`cpu-sources/`，generator 未改）。
- 复制 SRC1 B 的 CPU runner（`run_cpu_session_v1.py`，sha 已记），固定 worker 为 stub、词表为生产 tokenizer.json（11,422,654 B，sha `aeb13307…`）。
- 串行执行 B-C01 到 B-C10，每个独立 OS 会话，预算 55 秒 work / 60 秒 total，无重试。

## 2. 结果

| 情境 | 组合 trial | 宿主 result.json | 退出 | 判定 |
| --- | --- | --- | --- | --- |
| B-C01 registered 成功，关闭 held | passed | 未写出 | 124（超时 SIGTERM） | 收尾挂起 |
| B-C02 cached 响应 | passed | 未写出 | 124 | 收尾挂起 |
| B-C03 工具多 token 被拒 | passed | 未写出 | 124 | 收尾挂起 |
| B-C04 工具缺 token 被拒 | passed | 未写出 | 124 | 收尾挂起 |
| B-C05 工具 open 错误 | passed | 未写出 | 124 | 收尾挂起 |
| B-C06 child 源失败 | passed | 未写出 | 124 | 收尾挂起 |
| B-C07 child 缺 intent | passed | 未写出 | 124 | 收尾挂起 |
| B-C08 关闭点基础设施故障 | passed | 未写出 | 124 | 收尾挂起 |
| B-C09 parent/child 看门狗 | passed | 已写出 | 0 | 完整通过 |
| B-C10 消费后基础设施故障 | passed | 已写出 | 0 | 完整通过 |

组合 trial.json 的 `passed` 对十个情境全部为真，oracle 新增的"HostClose/finalize/publication 属于原父 scope 与原 caller"检查全部满足。B-C09 与 B-C10 走异常退出路径（看门狗 124 报告、基础设施故障 exit 2），显式 `Darwin.exit`，收尾正常。

## 3. 收尾挂起的定位

对 B-C01：事件流止于 `control.reader_joined`（子服务宿主的控制读取器收束），worker 正常关闭，但顶层 `result.json` 从未写出，进程不退出，外层 wrapper 在 55 秒 work 预算处 SIGTERM。组合 trial 已写出且 passed=True，说明挂起在产出结果之后、写 `result.json` 之前的宿主收尾段，而非组合逻辑本身。

这个挂起不是本轮运行时修复引入的：`ParentEntryRoots` 未被本轮改动，其 `joinWork`/`cancelAndJoinTimer` 是标准 await；`SourceResponseControls` 与 `JoinedResultDeliveryControls` 的门改动经 diff 核对与原语义等价，且这些门不在父组合 CPU fixture 路径上（该路径用 `ParentCompositionCPUFixtures` 自己的门）。正常完成路径的 work Task 在产出结果后未能让顶层 completion cell 落地，八个正常情境因此卡在收尾。异常退出路径（B-C09/C10）绕开该收尾段，故不受影响。

## 4. 这一步的价值与边界

- **价值**：十个情境从"从未执行"变为"首次执行"，组合逻辑与 oracle 修正在 CPU 上得到实际验证（trial 全 passed），并精确暴露出一个此前无人知道的宿主收尾挂起，只影响正常完成路径。
- **边界**：worker 是 stub，非真实 MLX；没有 GPU；收尾挂起尚未修复，八个正常情境没有写出宿主级 result.json，因此尚不能宣称十案"完整通过"。T01 工具自验证（64 负例族中 12 个）与真实模型 P01 仍未收敛，本轮未触及，仍是 SRC1 B 的两个未完成前沿。
- **下一步最小切片**：定位 work Task 产出结果后 completion cell 未落地的那一个 await（候选是子服务宿主收尾与顶层 cell 落地之间的衔接），修复后重跑八个正常情境；再处理真实 MLX 的 P01。
