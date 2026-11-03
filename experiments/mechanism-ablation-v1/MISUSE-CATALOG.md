# MA1 误用目录

每条误用给出定义、在各级别如何构造、预期判定点。实际观察写入 `results/mechanism-ablation-v1/misuse/REPORT.md`，与预期不符时以实际为准并保留原始输出。

判定层次记号：C 编译器拒绝；S 前端静态检查拒绝；H 宿主运行时拒绝；W worker 线级拒绝；X 由构造不可表达；N 无任何拒绝。状态记号：crash 崩溃；sticky 进入 Unresolved 且不再准入；silent 继续运行且没有证据表明安全。

| 编号 | 误用 | L0 构造 | L1 构造 | L2 构造 | 预期 L0 | 预期 L1 | 预期 L2 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| M1 | 归还前未 join：看到 EVAL_RECEIPT 帧就发 RELEASE，原 producer Task 尚未发布返回 | `--scenario misuse:M1`，独立任务在收到 receipt 帧时直接发 RELEASE | 客户端直接调用内部 `session.release(receipt)`，不经 `owner.drain` | 源程序无法发 RELEASE；Ledger `requireBaseSafe` 要求 ownjoin | N silent | H 于第二次归还时由 W 拒绝，sticky | X 加 H |
| M2 | 归还前提交 B：A 的租约仍活着就发 EVAL B | `misuse:M2` | `owner.begin(B)` 早于 `drain(A)` | Owner epoch 非 nil 时准入被拒 | W sticky | H（owner busy）继续 | X 加 H |
| M3 | 重复归还：同一 receipt 发两次 RELEASE | `misuse:M3` | `drain(A)` 两次 | β 只存在一次 | W sticky | H（drain once）继续 | X |
| M4 | 把等待者取消当完成：W 取消返回后直接提交 B，不 join A 不归还 | `misuse:M4` | 跳过 drain 直接 begin(B) | 源程序 scope 退出强制 join | W sticky | H 继续 | X 加 H |
| M5 | 遗忘 A：W 取消后直接 SHUTDOWN | `misuse:M5` | 不 drain 直接 shutdown | scope 退出要求 ownjoin 与 β Safe | W sticky | H（phase leased）sticky | X 加 H |
| M6 | 不核 ACK 身份：接受 lease epoch 错误的 ACK 后提交 B | `misuse:M6` 加 stub `wrong_release_epoch` | stub `wrong_release_epoch`，`validateRelease` 应拒绝 | `validateMLXReturn` 应拒绝 | N silent | H sticky | H sticky |
| M7 | 丢 ACK 后仍准入 B：宿主丢弃 ACK 帧并继续 | `misuse:M7`，宿主侧丢帧注入，worker 正常 | stub `lost_release_ack`（worker 退出 74）或宿主丢帧 | CM2 CPU C7 | N silent（worker 已归还，B 被接受，宿主没有证据） | H sticky | H sticky |
| M8 | 逃逸的会话凭据：归还后用旧 lease 再发 EVAL | `misuse:M8` | 保留旧 `LeaseID` 再 `session.eval` | 旧 epoch 的 access 被 `validAccess` 拒绝；静态 Stable 规则 | W sticky | H 或 W sticky | S 加 H |
| M9 | 同一后端两个 owner | 不适用（无 owner 概念） | 两个 `KVLoopOwner` 共用一个 session，各自 begin | 第二个 Owner 被 BackendOwnerBusy 拒绝 | X（不可表达为误用） | H sticky（整个 session 被隔离） | H |
| M10 | 非创建者取消 producer | 不适用（任何代码都可取消任何 Task） | 消费者句柄没有取消方法 | Control 必须显式授予 | N | C | S |

补充说明：

- M1 在 L0 上是真正的静默不安全：worker 允许归还，因为它看不见 Swift Task；宿主结果可能在归还之后才发布。这正是 worker 与宿主责任的分界。
- M7 在 L0 上需要宿主侧丢帧注入，因为 stub 的 `lost_release_ack` 会让 worker 退出，无法演示"worker 已归还而宿主无证据仍前进"。
- M9 与 M10 在 L0 上没有对应物，这本身就是结论：没有机制的地方，误用无从定义。
- 每级别每条误用的实际输出保存在 `results/mechanism-ablation-v1/misuse/<level>-<id>/`。
