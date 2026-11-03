# P1：持续 decode 的无 B 基线与按观测成本分块

2026-10-30。状态：v5冻结来源完成57项CPU检查；六条件小shape真实smoke及共同来源／派生核验通过，67项独立检查器测试通过。S0六条件真实smoke与冻结revision-4独立核验也已通过，成本策略该条trial为32×32、31次minimum-progress fallback、29/32 slice超过名义预算，完整保留且不调整参数。S0矩阵36主＋36warmup已全部真实运行exit0，并通过完整独立核验与第二套事件统计复算；新增30项矩阵与7项复算测试通过，见[正式报告](../results/batch5/decode-cost-s0-matrix-REPORT.md)。矩阵cost的105个main slice无fallback/超预算，仍保留smoke差异；[anchor32六smokes](../results/batch5/decode-cost-a64-b1024-anchor32-smoke-REPORT.md)已真实执行并独立验收，[晚到达正式36主＋36warmup报告](../results/batch5/decode-cost-a64-b1024-anchor32-matrix-REPORT.md)全部真实执行并通过完整独立核验，后8间隔/区组配对另从raw复算一致，见[图形及来源](../results/batch5/decode-cost-a64-b1024-anchor32-figures-v1/artifact-manifest.json)。[第三形状A64/B4096/anchor4六smokes](../results/batch5/decode-cost-a64-b4096-anchor4-smoke-REPORT.md)已全部执行并通过原revision-4独立验收，cost实际109块/79 fallback/64超预算，保留原参数；其36主＋36warmup矩阵已通过独立预飞并启动，尚待完成和独立验收，全部8组合门槛保留。四次模型加载前失败及来源修复保留，未安装依赖或改变数值策略。证据见[小shape报告](../results/batch5/decode-cost-small-REPORT.md)、[S0 smoke报告](../results/batch5/decode-cost-s0-smoke-REPORT.md)和[批次准备工具](P1_BATCH_PREPARATION.md)。

## 已有证据和这次要回答的问题

已只读核对 [batch3/decode 报告](../results/batch3/decode/REPORT.md)、原始 manifest 与 source-snapshot.tar.gz 中的冻结驱动：run.py SHA256 `eec8ae8a87846eecb8a23e02811e453a4c3c07d156d3972c2ce569247a2292ab`，protocol.py `f3a0d656fe5d249f6f203df3f83d92b0b2cae11c1f12c62bd7dc2697d4bbbb6a`，均与清单相符。

旧实验在 A64 prompt＋40 decode、B1024 prompt、A index4 交付后到达的条件下，对四策略各测 5 次。32-token 块降低最长交付停顿，同时增加 A 中位 ITL、B TTFT 和总工作时间；32-token 条件还出现约 753 ms 的最长 A ITL。它没有无 B 性能基线，也没有按成本选择块长，因此不能回答“相对无干扰输出多付出多少”或“随实际成本变化的块长是否有更好的取舍”。旧数值不是本轮成本策略的拟合标签。

本轮先回答两个窄问题：

1. 同一实现、同一完整 A 输出下，B 各策略使 A 的交付间隔、完成时间增加多少？
2. 一个只依赖已完成块的简单成本估计器，能否在固定四种块长之间提供不同的 ITL/TTFT/总工作取舍？无改善、预测失准或始终选择最小块也必须保留。

延续单模型线程、真实 `mx.eval` 返回后切换请求。这里没有 GPU 抢占、同时执行 A/B、取消减少工作、CPU/GPU 放置变化或共享分支缓存；这些属于其他独立实验。

## 同一新实现中的六种条件

| 条件 | B 工作 | B 存在且 A 尚未结束时的规则 |
| --- | --- | --- |
| `no-b` | 不创建/准入/执行 B | 连续执行 A；保留共同 anchor 标记，但没有伪造 B 事件 |
| `whole` | 全部 prompt 为一块 | B 整块完成，然后继续 A |
| `fixed-32` | 最大块 32 | B 一块、A 一次 decode 交替 |
| `fixed-128` | 最大块 128 | 同上 |
| `fixed-512` | 最大块 512 | 同上 |
| `cost-200` | 下述因果成本策略 | 同上；只改变下一 B 块的输入数 |

所有策略共享同一模型调用、求值、token 交付、资源快照、页账本、正常清理和错误记录代码。差别集中在 B 是否存在、下一块有多大。不得为其中一项删掉快照/验证，或为 no-b 插入模拟 B 工作。策略选择及其日志成本计入实测时间。

最小主情境 S0：A prompt64、A decode40、B prompt1024、到达 anchor4，沿用旧实验的完整实际 A/B token ID fixture。实施时从冻结 raw 的 reference 提取并固化输入列表与 hash，逐格核对；不依赖“种子相同”代替输入一致。固定 greedy、固定输出长度，保留 EOS 而不提前结束。

| 工作口径 | no-b | 五个 B-present 条件 |
| --- | ---: | ---: |
| A 实际输入 | 64＋40＝104 | 104 |
| A 交付输出／head 求值 | 41 | 41 |
| B 实际输入 | 0 | 1024，全部完成 |
| B 交付输出／head 求值 | 0 | 1 |
| 总模型输入／输出／head 求值 | 104／41／41 | 1128／42／42 |

A 的最后一个输出不再作为输入送回模型。A prefill 和每次 decode 都求值 head；B 非最终块只物化实际 KV pools，最后一块才求值一个 token head。每次 model forward 仍可能构造惰性 head 图，须分别记录 constructed head、evaluated head 和交付数。不同块长/实际分块的 forward 数、物化形状、pool 增长和主机开销可变，固定输入/head 数不等于固定 FLOPs。

no-b 与 B-present 的总工作量不同：它是 A 干扰基线，不能把二者的总输入吞吐直接排序为调度收益。五个 B-present 条件之间才具有相同的 A/B 输入和输出目标。

## 首个成本策略：只用过去已完成工作的在线估计

先固定一个策略和参数，不在本轮扫预算或看结果后挑最优配置：

- 名义 B slice 预算 `budget_ns = 200_000_000`，不是 A ITL deadline。
- 候选最大 token 数为 `{32,64,128,256,512}`，最后按 B 剩余量截短并去重。
- 第一个 B 块取 `min(32, remaining)`，作为真实必需 B 工作的一部分；没有额外校准 forward、额外 token head、虚拟耗时或 sleep。
- 每个 trial 的估计器重置；warmup 的估计状态不能流入 measured trial，不跨条件/区组学习。
- `rho` 是估计的整数 ns/token；以已完成 B slice 的真实耗时更新，EWMA 权重 1/2，预测乘固定 5/4 裕量。使用整数上取整，避免 JSON 浮点边界使独立分析器选出不同块。

```text
rho = unknown
before each B block:
    candidates = sorted(unique(min(c, remaining) for c in [32,64,128,256,512]))
    if rho is unknown:
        n = min(32, remaining)
        reason = bootstrap
    else:
        predicted_ns(n) = ceil(5 * rho * n / 4)
        fitting = [n for n in candidates if predicted_ns(n) <= 200_000_000]
        n = max(fitting) if fitting else min(candidates)
        reason = largest_predicted_fit or minimum_progress_fallback
    execute these n required B input tokens, including its normal observation work
    actual_ns = step.yield_ready - step.started
    sample_rate = ceil(actual_ns / n)
    rho = sample_rate if previously unknown else ceil((rho + sample_rate) / 2)
```

新 `step.yield_ready` 在本步 eval、必要 token 交付、offset/页快照及可能的正常 request release 完成后记录。其与 step.started 的差包含该步构图/求值和后处理；策略自身的选择区间另记 `policy.decision_started/finished`，同样处于 A ITL 和总工作时间中。A 的 decode-call 服务时间仍单独保存，不能用 B service 或整体 ITL 替代。

这个最小模型有意不拟合复杂上下文曲线：它把近期总 slice 成本近似成线性 ns/token，不能区分固定开销、attention 上下文增长、页扩容复制和系统状态。它也没有独立的最终 head 成本项：此前非最终块是 pools-only，最终块额外求值 head，必须标出 `prediction_has_separate_head_term=false` 并单列最终块的预测误差。不得声称这已经是覆盖所有成本的模型。

最后不足 32 token 时仍完整处理；若最小块也预测超预算，照常执行最小块以取得进展，记录 fallback。实际超预算只是待解释的性能观察，不是数值/协议失败，也不能删样本。即使预测命中，A ITL 还包含自身 decode、调度和观察成本，因此 200 ms 不能称 A 响应上界。

A 先结束时，所有策略继续完成 B：fixed 保持原块长，cost-200 继续同一规则，不自动把 B 尾段合并成 whole。否则会把尾段合并收益与成本估计混为一个变化。所有后续块必须使用先前真实完成的观测；禁止读取未来耗时、隔离参考的速度或另一个 trial 的结果来决定当前块。

## 到达、数值参考和状态保证

全部条件在 A 第 anchor 次 decode token 实际交付且该步统一后处理完成后，记录共同 `anchor.reached`。B-present 随后按真实当前时刻记录一次 b.requested/admitted；no-b 只记录 anchor，不产生 B 的请求、准入、模型调用、token、资源或 TTFT 事件。它也不构造空 B cache 或保留 B 的配额。

到达是程序进度锚定，不是固定墙钟开放环流量。A 的数值前缀必须相同，但各 trial 的 anchor 墙钟时间可以不同；不回填成 A token 的旧时间戳。

每个进程在计时外建立隔离 A 全部 41-token 输出与 B whole 首 token 参考。为避免 setup 参考工作因 no-b 而改变，六种条件使用同一份输入 fixture 和相同的 A/B whole 数值参考准备；这部分明确位于 `reference/setup`，不出现于 no-b 的 trial 工作或指标。它不是 no-b 中执行 B 的业务请求，也不是无 B 的性能样本。

实际 adaptive 分块受计时影响，不能事先虚构它的同分块参考。每个进程 warmup 和 measured trial 完成后，在计时外按各自已保存的 **完整实际 B 分块序列** 生成隔离 B 参考；固定条件使用同一重放接口，no-b 没有该重放。对已相同的序列可以缓存数值参考，但必须记录 reference ID、序列 hash 与复用关系。

要求分开保存和检查：

1. 每个 trial 的 A 全部实际输出逐 token 等于共同隔离 A；A 每层 offset 从输入/自回归步数重算。
2. B 实际首 token等于按该 trial 实际分块序列得到的隔离参考。
3. 该分块参考等于 B whole 参考。若不同，保留数值可比性失败，不能直接称缓存损坏，更不能作为通过正确性的速度结果。
4. B 输入片段连续、无重叠/遗漏，最终恰好完成完整 prompt；非最终块 head=false，最终且仅最终 head=true。no-b 的 B 实际工作为零。
5. 每层 A/B 私有页不相交；预留覆盖全部 A decode 输入与完整 B prompt，实际 used/free/allocated 守恒；同一已被 A 使用的 pool 不可因 B 开始/结束而 reset。

normal measured trial 在真实设备工作与必要释放结束后结束 work_ms；安全 finally 同步、参考重放均另列时间。最后的完整进程 passed 必须等参考重放通过后才能为 true。重放开销和从测量结束到验证完成的延迟必须记录；它不是应用成本为零，也会影响下一进程的热状态。策略顺序平衡不能证明这种影响不存在。

所有策略采用同一充足逻辑容量配置，B-present 保留 A/B 两项全生命周期预留，no-b 只取得 A 的预留。底层 backing 可以增长；保存 capacity/nbytes、storage_growths/copied_bytes 和逐层页槽，不能把逻辑容量/峰值当系统物理内存或硬件并行证据。

异常保留顶层报告对象、已经发生的事件/token、原 traceback、失败检查和诊断错误。实际同步成功后才清理；同步失败/完成未知则保留 model/cache/ledger 根并停止后续 trial/参考重放。诊断快照再次失败也必须返回完整 failed trial，不抹去部分证据。不能继承旧驱动可能重建空失败报告的兜底路径。

## 指标和配对方式

保存并独立重算四种原始序列：A token 交付 ITL、A decode-call 服务时间、全局 step 完成间隔、全局 token 交付间隔。B 的中间块没有输出 token。B TTFT 从 b.requested 到 b.first_token，no-b 不填 0 或伪造 TTFT。

每 trial 至少报告：

- A 全 40 个 ITL 及 max/median/mean，A 完成时间；另报 anchor 后区间 `index anchor→anchor+1 ... 39→40` 的原始 ITL、max/median/累计时间。
- B TTFT、准入等待、首次计算等待，B 每块 service 时间；名义预算超出量/次数、预测误差、bootstrap/fallback 数、实际块长序列及块数。最终 head 块单列。
- work_ms 与 through_drain_ms，按真实工作分子重算输入/输出吞吐；B-present 保留 1128 inputs/42 outputs，no-b 为 104/41，不能合并分布。
- A 尚可运行时完成的 B token/块，以及 A 完成后 B 的尾段 token/块/时间。后者不作为持续交错公平性的证据。
- 逻辑页峰值、backing 快照峰值及增长/复制计数，声明其观察边界。

主统计单元是一个完整 measured trial，不能把 40 个相关 ITL 当 40 次独立重复。每个区组内：所有 B-present 条件相对 no-b 配对 A 指标；cost-200 相对 whole、32、128、512 配对 A 最大/中位 ITL、B TTFT 与 work_ms。先计算每对差/比，再给中位数、范围和所有配对值，不用两个边际中位数之差代替。

按输出 index 配对的 A 累计交付偏移也可保留为描述，但不能跨 trial 逐 token 伪造独立显著性。以整条 trial 的完整曲线和区组差解释。少量 pilot 不报告稳定 p95、最佳块长或最坏响应界。

## 分阶段执行门槛

**阶段 0：只实现/CPU 协议。** 新目录、独立 schema，例如 `tiny-decode-cost`；三种策略类别共用一步执行与清理代码。独立分析器不导入 harness/oracle/MLX，从实际事件重算 scheduler、估计器 rho 更新与每次选择。CPU 负例至少覆盖：no-b 造 B 事件、丢失/重复/跳过输入、A 输出未完成、B 中间块多算 head、使用未来样本、错误截断/最后小块、fallback 无进展、warmup 状态泄漏、晚到后 A 先结束仍应完成 B、错误时间原点、未知异常/同步失败、诊断失败丢报告。fake 数据仅作协议测试。

**阶段 1a：最小真实 smoke。** root 审阅并排设备后，A16＋8 decode/B64/anchor2，六条件各一次、无 warmup；验证 no-b 零 B 工作、两条输出参考、实际分块与资源守恒。它不是性能比较。再在 S0 六条件各一次确认完整形状路径，包括动态实际分块参考。

**阶段 1b：最小可比较主矩阵。** S0 六条件×6 区组＝36 个 measured trial，另 36 个匹配策略的 warmup。每个 cell 独立进程，1 warmup＋1 measured；输入、源码、依赖、模型、预算/候选/裕量和完整 schedule 在运行前冻结。

条件编号 `0=no-b,1=whole,2=fixed-32,3=fixed-128,4=fixed-512,5=cost-200`。采用六阶 Williams 首行 `[0,1,5,2,4,3]`，其余行把每个编号循环加 1（mod6），形成六区组；每个位置恰含各条件一次，区组内每个有序相邻条件对出现一次。用固定 seed20261029 只打乱六个完整区组的顺序。不根据测得速度追加、换序或选择预算。

完整计划/argv 与 raw 用独占新路径保存。失败即停止并保留原始输出；不能自动重试替换异常/慢样本。所有 GPU 工作串行，不与其他矩阵或重 CPU 分析并发。root 审核这一计划后才开始实现，后续 GPU 仍须其排程。

**阶段 2：保留的上下文/到达扩展门槛。** 阶段 1 完成不代表 P1 扩展全部结束。必须继续检验 A prompt `{64,1024}` × B prompt `{1024,4096}` × anchor `{4,32}`，A decode 固定40，六种条件不变；共8个形状/到达组合，S0只是其中一个。先每组合做正确性 smoke，再为各组合独立保留同样6区组对照。新形状不得与S0混合成一个中位数，执行规模/设备时隙由root逐阶段确认，不能把未测组合写成已覆盖。

八个形状的执行顺序现已单独冻结在 [context matrix v1](../configs/decode-cost-context-matrix-v1.json)：按 A 长度、B 长度、anchor 依次取上述列表顺序；S0使用已有矩阵，不重复计数。S0正式矩阵独立验收后，下一形状是 A64/B1024/anchor32，即 B 到达时 A 只剩8步decode，专门区分共同运行期与B尾段。此文件只固定原计划形状顺序，不表示后续七组已准备或测量；策略参数保持不变。

长输入 fixture 在旧共同 A64/B1024 列表后追加确定 token：A tail 使用独立 seed1701，B tail 使用 seed1801，范围沿用已验证模型的 `[100,min(30000,vocab_size−1)]`。所有形状取各自已冻结长列表的前缀，避免改变 A 长度时同时移动 B 的随机数流。完整列表/hash 必须入档。

anchor32 只剩 8 次 A decode；B 很可能在 A 结束后仍有大量尾段。需用实际事件区分两请求共存和尾段阶段，保留完整 B 工作，不把尾段吞吐当 A 响应性改善。较长 A 会改变 A decode/context 与 pool 背景，较长 B 会检验 ns/token 估计随 B context 增长是否失准；这是明确的外推门槛，不能省略。

本轮只用一个预算/估计公式；若阶段2失败或策略长期退化为最小块，先报告失败范围。更复杂的上下文模型、多个预算、离线校准、A deadline 联合预测、A结束后的尾段合并等均是另一个版本与预注册对照，不在这份最小实现里悄悄加入。

## 对研究主张的限制

这次补齐的是实测基线和因果可重放的调度策略：它可能帮助定位响应性成本，却不把经验成本估计提升为 PriML 式最坏界。已经提交的一块仍要实际完成，最小 token 块也可能超过名义预算。统一 PL 仍需单独表达完成义务、访问租约、可调度边界和成本假设；这些不会因加入一个 EWMA 策略自动获得。
