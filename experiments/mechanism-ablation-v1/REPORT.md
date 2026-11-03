# MA1 机制消融：首批结果

2026-11-03。三个级别在同一个受检情境、同一个 Python MLX worker、同一份冻结输入上实现并运行。本批只跑 CPU stub worker；真实 MLX 的 N 情境每级一次由 root 串行安排。结论先行：worker 的线级状态机（三级别共用）本身已经拒绝了大多数误用，宿主侧机制的边际价值集中在三处，而这正是判断"语言层是否必要"的落点。

## 1. 三个级别都已跑通 N 情境（CPU stub）

| 级别 | 实现 | N 情境结果 | A/B token |
| --- | --- | --- | --- |
| L0 手写协议 | 单文件 Swift，Foundation 加原始管道读写，无 owner/账本 | passed | 83429 / 147645 |
| L1 动态账本库 | SMWO1 adapter 移植到 cm1 线协议 | passed | 83429 / 147645 |
| L2 受检 PL | 规范运行时 cm2-checked 加 CM2 源程序 | 见下 CPU 门 | 见下 |

L0 与 L1 的 token 一致，且都通过 checkReuse（B 复用 A 的逐层逻辑页槽、代次递增）。L2 的受检路径由 CM2 的 CPU 套件覆盖，本工作区已用修复后的 `cm2-cpu` 重跑并通过（见第 4 节）。

L0 的 N 情境时间线，取消请求为起点，单位毫秒，stub 时钟，单次：

| 起点 → 终点 | L0 |
| --- | ---: |
| 取消请求 → 等待返回 | 0.033 |
| 取消请求 → A 原 Task join | 0.726 |
| 取消请求 → A 归还确认 | 2.780 |
| B 请求 → B 准入 | 0.012 |
| B 请求 → B receipt | 1.921 |
| 会话总时长 | 7.786 |

L1 的同类 stub 时间线由移植报告给出（取消到等待返回 0.406，取消到归还确认 15.323，B 请求到 receipt 10.069，总时长 216.580），时钟域不同不与 L0 直接比较，各为单次非上界。

## 2. 误用目录的实际判定（CPU stub）

记号：C 编译期；W worker 线级；H 宿主运行时；X 不可表达；N 无拒绝（静默）。

| 误用 | L0 实际 | L1 实际 | L2 实际 |
| --- | --- | --- | --- |
| M1 归还前未 join | **N 静默**：worker 接受早 RELEASE，宿主无阻拦（`release_before_join` 记录在案） | H：首次直接 release 宿主不拦，仅二次归还被会话 phase 挡下，sticky | X 加 H：源程序无法发 RELEASE，Ledger `requireBaseSafe` 要求实际 ownjoin |
| M2 归还前提交 B | W：worker 报 "EVAL while a prior lease is live" | H：owner busy 拒绝后恢复 | X 加 H：Owner epoch 非 nil 拒绝 |
| M3 重复归还 | W：worker 报 "release without sealed receipt" | H：drain-once 拒绝后恢复 | X：β 只存在一次 |
| M4 取消当完成 | W：worker 报 "EVAL while a prior lease is live" | H：拒绝后恢复 | X 加 H |
| M5 遗忘 A 直接 shutdown | W：worker 报 "shutdown before releases complete" | H：会话 phase 拒绝，sticky | X 加 H：scope 退出要求 ownjoin 与 β Safe |
| M6 ACK 身份错 | **N 静默**：L0 未校验 ACK 的 lease epoch，接受错误 ACK | H：`validateRelease` 拒绝，sticky | H：`validateMLXReturn` 拒绝，sticky |
| M7 丢 ACK | **N 静默后挂起**：worker 已归还，宿主无证据仍等待，最终超时 | H：sticky，B 不准入 | H：sticky（CM2 CPU C7 保留 Unresolved） |
| M8 复用旧 lease | W：worker 报 "wire lease must be fresh monotonic counter" | W：worker 单调计数兜底，sticky | S 加 H：旧 epoch access 被 `validAccess` 拒绝 |
| M9 同后端两个 owner | X：L0 无 owner 概念，误用无从定义 | H：会话单租约准入拒绝，owner1 恢复 | H：`BackendOwnerBusy`，size 必须为 1 |
| M10 非创建者取消 producer | N：任何代码都能取消任意 Task，无屏障 | 弱 C：消费者句柄无取消方法，但同模块仍可 `task.cancel()` | S：Control 必须显式授予 |

## 3. 这组结果说明什么

- **worker 的线级状态机是三级别共享的安全底座**。M2、M3、M4、M5、M8 在 L0 就被 worker 拒绝，和宿主机制无关。任何关于"受检 PL 比手写更安全"的论证都必须先扣掉这一层，否则会把 worker 的功劳记到语言头上。
- **宿主机制的边际价值集中在三处**：join 先于归还（M1）、ACK 身份与丢失（M6、M7）、所有权与取消权（M9、M10）。这三处恰好对应责任分离设计的核心主张，也是 L0 唯一静默失败的地方。
- **L0 的 M1/M6/M7 静默失败是真实的**。worker 看不见 Swift Task 的 join，只要宿主不自己记账，早归还和错误 ACK 都会被接受。L1 用运行时账本堵住，L2 用源程序结构加 Ledger 堵住。
- **L1 与 L2 对 M9/M10 的差别是"运行时 vs 类型/静态"**。L1 靠会话单租约和私有句柄（动态、可绕过同模块），L2 靠 `BackendOwnerBusy` 和显式 Control 授予（结构性）。这是语言层相对库的唯一结构性增量，且目前只有 M9 有运行时证据、M10 只有编译负例。

## 4. L2 受检路径的 CPU 门（修复后运行时）

本工作区用修复后的运行时重跑了 CM2 的 CPU 套件（`cm2-cpu`，六源七组八串行 worker），exit 0，passed=True，7 个 fixture（C1 到 C7）全部通过，C7 的丢 ACK 情境仍保持 Unresolved 且 B 不准入。证据在 `results/runtime-fixes-v1/cm2-cpu-run-v1/`。

## 5. 代码量与成本（粗略）

| 级别 | 情境程序 | 机制（需信任但不写） | release 构建 |
| --- | --- | --- | --- |
| L0 | 约 560 行单文件 Swift | 无（worker 线协议除外） | 约 5 秒 |
| L1 | Main.swift 情境函数 | Owner/Wait/Session/Support，约 1200 行 Swift（移植自 SMWO1） | 见移植报告 |
| L2 | normal.json（由约数百行 Python 构造器生成） | 16,619 行 Swift 加 6,443 行前端 Python，含 CM2 路径外功能 | 约 96 到 107 秒 |

成本是观察不是上界，三级别不做统计检验。

## 6. 不声称什么

三级别共用 worker，worker 的拒绝对所有级别相同，本实验只度量宿主侧差异。CPU stub 的时间不是真实模型延迟。n=1 每情境，不支持尾延迟或最坏界。M10 在 L0/L1 只有编译观察，无运行时证据。L2 机制行数包含 CM2 路径之外的功能。

## 7. 真实 MLX 的 N 情境（每级一次，root 串行执行）

三个级别的 N 情境已在真实 Apple MLX worker（`runtime/backend/worker.py`，GPU，Qwen3-0.6B-MLX-4bit）上各跑一次,与本工作区修复后的运行时。全部 exit 0,A/B 首 token 均为参考值 72/271,与 CM2、SMWO1 一致。

| 级别 | 结果 | A/B token | 取消→等待返回 | 取消→A 归还确认 | 会话总时长 |
| --- | --- | --- | ---: | ---: | ---: |
| L0 手写 | passed | 72 / 271 | 0.033 ms | 80.649 ms | 5.893 s |
| L1 库 | passed | 72 / 271 | 见 result.json | 见 result.json | 6.506 s |
| L2 受检 PL | passed | 72 / 271 | Core 事件流 | Core 事件流 | 11.036 s |

证据:`results/mechanism-ablation-v1/gpu-l0/N`、`gpu-l1/N`、`gpu-l2/N`。L0 的时间线显示取消在 0.033 ms 返回,而真实 prefill 的 join 在约 79 ms 到达,归还确认约 80.6 ms,B 的 receipt 约 115 ms,shutdown 约 759 ms。三段延迟分离,起点不同不相加,每级 n=1,不是上界。L2 由规范运行时 cm2-checked 加 CM2 的 normal.json 源程序,走 FORK 等待者取消加 owner KV 归还的完整受检路径。

这补上了第 1 节标注的"真实 MLX 的 N 情境未跑":消融的安全排序结论现在在三级别上都有真实模型证据,而不只是 CPU stub。worker 共用底座的结论因此更稳:同一个真实 worker 对三级别给出相同 token 与相同归还协议,宿主侧差异仍只在第 2 节的误用矩阵里。
