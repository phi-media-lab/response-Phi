# L1 相对 SMWO1 的改动

L1（动态账本库级别）从 SMWO1（`legacy/experiments/swift-mlx-wait-owner-v1`）移植。字节级来源见 `ORIGIN.json`。Owner.swift、Wait.swift、Support.swift 按字节原样导入；Session.swift 做线协议移植；Main.swift 作为情境程序重写；CPU.swift 不导入（它是 SMWO1 专有的 13 项控制套件，与 MA1 情境无关）。

## Session.swift 的改动（约 52 行差异）

1. **Inputs 读 `cm1-inputs-v1` 而非 `smwo1-inputs-v1`**。SMWO1 读 6 个显式 request（ref/N/D 各 A/B）；MA1 与规范 worker、受检 PL 共用同一份输入清单，只含 `a32`、`b_prefix7`。A 由 a32 派生（Prefill32，lease_epoch 1）；B 由 `bSpec(aToken:)` 在 A 完成后派生（b_prefix7 加本会话 A 的实际 token，Prefill8，lease_epoch 2）。理由：三个级别必须吃同一份输入才能比较，且 B 第八项取真实 A token 是 CM1/CM2 固定规则。
2. **EVAL 线字段补齐到规范 worker 的精确集合**。新增 `operation`、`purpose="native-reference"`、`source_context=null`、`native_context={request_id,task_id,purpose}`。SMWO1 的 worker 不要求这些；规范 worker 的 `command()` 做 `set(cmd)==fields` 严格校验，缺字段即拒。
3. **RELEASE 同样补齐** `operation`（取自 receipt 回显）、`purpose`、`source_context`、`native_context`。规范 worker 的 `release` 逐字段比对 active 请求，必须一致。

Owner/Wait/Support 未改一字：owner 状态机（idle→leased→joining→releasing→idle，epoch 自增）、可取消等待 hub（一次性仲裁、continuation 锁外 resume）、会话健康检查与有界 OS 清理都原样复用。这正是 L1 要度量的"机制"。

## 代码量

| 类别 | 文件 | 行数 |
| --- | --- | --- |
| 情境程序（用户为本情境写的） | Main.swift（N 加 10 个误用分支） | 295 |
| 机制（用户信任但不写） | Owner.swift | 118 |
| 机制 | Wait.swift | 129 |
| 机制 | Session.swift（移植后） | 525 |
| 机制 | Support.swift | 209 |
| 机制合计 | | 981 |

N 情境本身的情境程序约是 Main.swift 的 `runN` 加三个辅助函数（约 70 行）；其余 220 行是十个误用分支与 @main 外壳。与 L0 的全手写、L2 的 `normal.json` 源程序对照时，用这个 70 行的 N 情境数。

## 不声称

L1 是 SMWO1 的移植，不是 P4 owner 库（swift-owner-composition）的逐字复用；两者都是"动态账本库"，但 API 形状不同。M10 的编译负例在单目标包内进行，没有独立模块的 public client 边界，这一点与 C4a 的局限相同。
