# MA1：机制消融（手写协议 / 动态账本库 / 受检 PL）

2026-11-03。落实 P8 语言子集提案第 10 节要求、此前从未执行的机制消融。问题只有一个：同一个情境下，宿主侧机制每升一级，多拒绝了哪些误用、多花了多少代码、多付出多少延迟。结果目录 `results/mechanism-ablation-v1/`。

## 1. 固定情境

沿用 CM2 的 N 情境（自然早取消），全部级别使用同一个 Python MLX worker（`runtime/backend/worker.py`，线协议 cm1-inputs-v1）与同一份冻结输入（CM2 的 `preparation/inputs.json`，A32 与 b_prefix7）。

1. 会话启动：HELLO 经 wrapper 做 OS 核验后发 LOAD_PERMIT，worker 才加载模型。
2. A：发 EVAL Prefill32，原 producer Task 等待 receipt 并发布。
3. W：一个独立等待者等待 A 的结果；宿主在 EVAL_ENTER 之后、receipt 发布之前请求取消 W。W 以取消返回并执行后继。
4. 归还：owner 实际等待 A 的原 Task 返回，再发 RELEASE，核对完整 28 层 ACK。
5. B：B8 等于 b_prefix7 加本会话 A 的实际 token。只有归还确认后才允许 EVAL Prefill8。B 同样 join 与归还。
6. SHUTDOWN 与 ACK，reap，IO EOF。

每会话两次 prefill，不做 decode、warmup 或重试。参考 token 来自同机 `cm2-reference` 的独立会话。

## 2. 三个级别

| 级别 | 实现 | 宿主侧机制 |
| --- | --- | --- |
| L0 手写协议 | `l0-manual/`，新写的单文件 Swift 程序，Foundation Process 加 JSON 行 | 无。所有次序靠程序员；只有 worker 的线级检查兜底 |
| L1 动态账本库 | `l1-library/`，SMWO1 的 Swift adapter 移植到 cm1 线协议，来源与补丁记录在 `ORIGIN.json` 与 `PATCH.md` | owner 状态机、epoch、可取消等待 hub、会话健康检查；全部运行时判定 |
| L2 受检 PL | `l2-checked/`，规范运行时 `runtime/` 的 `cm2-checked` 加 CM2 的 `normal.json` 源程序 | 前端静态检查（仿射、逃逸、效果）加 Ledger 义务账本加 Swift 解码器对应 |

L2 的 worker 由运行时按固定相对路径启动；L0 与 L1 由 wrapper 传入 `--worker-python` 与 `--worker-script`，指向同一个脚本。

## 3. 共同 harness

`harness/run_level.py` 由 `runtime/backend/run_session.py` 推广而来：保留许可流程、进程组控制、原始流保存与 run.json；去掉对产品名和 CM2 专有参数的限制，改为 `--swift ABS --inputs ABS --output-dir NEW --mode gpu|cpu --swift-arg ...`。它总是为 Swift 提供 `--inputs --session --pgid 0 --output-dir`，其余参数原样传递。

L0 与 L1 的 CLI：`--inputs --session --pgid --output-dir --worker-python --worker-script --scenario N|misuse:<id> [--stub-scenario NAME]`。L2 使用它自己的 CLI。

统一事件名（L0 与 L1 直接发到 stdout JSONL，字段 `event`、`swift_ns`，时钟原点为 Trace 构造；L2 由 `harness/compare.py` 从 Core 事件流映射）：

| 事件 | 含义 |
| --- | --- |
| `session.started` | Swift 进程开始 |
| `a.eval_requested` | A 的 EVAL 已登记待发 |
| `a.eval_entered` | 收到 A 的 EVAL_ENTER |
| `cancel.requested` | 宿主请求取消 W |
| `wait.returned` | W 的等待以取消返回 |
| `a.receipt_validated` | A 的 EVAL_RECEIPT 通过核验 |
| `a.join_returned` | owner 观察到 A 原 Task 返回 |
| `a.release_requested` | RELEASE 已发 |
| `a.release_acked` | 完整 28 层 ACK 通过核验，归还确认 |
| `b.requested` | 宿主决定提交 B |
| `b.admitted` | B 取得准入（owner 或 Ledger 允许 EVAL） |
| `b.receipt_validated` | B 的 EVAL_RECEIPT 通过核验 |
| `session.shutdown_acked` | SHUTDOWN_ACK 通过核验 |

`compare.py` 报告的区间：T1 取消请求到等待返回；T2 取消请求到归还确认；T3 B 请求到准入；T4 B 请求到 receipt；T5 归还确认到 B 准入；T6 会话总时长。各区间起点不同，不相加，每级别 n 次运行给出全部原始值与中位数。worker 时钟内的 eval 区间单独列出，不与 Swift 时钟拼接。

## 4. 代码量与开销

代码量分两类：情境程序（用户为这个情境写的代码）与机制（用户必须信任但不写的代码）。L0 全部是情境程序。L1 的情境程序是 `Main.swift` 的情境函数，机制是 Owner、Wait、Session、Support。L2 的情境程序是 `normal.json`（同时报告生成它的 Python 构造器行数），机制是整个 `runtime/` 的 Swift 与前端 Python，按文件列出并说明哪些在 CM2 路径上。

开销只比较同一 worker、同一输入、同一情境下三级别的 T1 到 T6。n=5 每级别，串行，GPU 由本工作区的执行者按序运行。这是观察，不是上界；不做统计检验。

## 5. 误用目录与判定点

见 `MISUSE-CATALOG.md`。每条误用在每个级别上记录：能否表达；在哪一层被拒绝（Swift 编译器 / 前端静态检查 / 宿主运行时 / worker 线级检查 / 无）；拒绝后系统停在什么状态（崩溃 / Unresolved 粘性 / 继续运行且无证据）。L0 与 L1 的误用变体在 CPU stub worker 上执行；L2 的证据来自 legacy 的静态拒绝、解码器负例、CM2 CPU C7，并在本工作区重跑 `cm2-cpu`。

## 6. 不声称什么

三级别共用 worker，因此 worker 自身的拒绝对所有级别一样；本实验只度量宿主侧差异。n=5 不支持尾延迟或最坏界。L1 是 SMWO1 的移植，不是 P4 owner 库的逐字复用。L2 的机制行数包含 CM2 路径之外的功能，报告时标明。模型 token 一致只说明首 token，不验证完整 logits。
