# P7 独立分支：用户意图换代、工具动作准入与效果观察

2026-10-29。状态：首轮六个真实文件情境已通过[root独立验收](../results/batch5/intent-effects-REPORT.md)，106事件与实际文件/回执/来源一致；19项协议、39项检查器及8项真实上游API合约测试通过。F保留真实文件已改／receipt写入失败及execution_succeeded=false；没有模型/GPU运行。原计划与下述后续范围保持不变，冻结attempt保存实施前版本。这里独立于已完成的[共享KV前缀实验](../results/batch4/shared-branch-REPORT.md)，不把两组证据拼成已经完成的统一agent。

以下保留 `experiments/tiny-intent-effects` 首轮设计与已验证范围：脚本固定提案与控制顺序，调用上游真实 `Workspace.execute` 修改本 workspace 下的临时沙盒文件；其结果不要求模型生成提案。下一子批按 [真实模型与工具计划](P7_MODEL_EFFECTS_PLAN.md) 在独立 `tiny-model-effects` 目录接入真实生成、严格解析和真实观察后的第二次生成，当前仅开始源码实现，尚未测试或运行。完整共享 KV／工具组合门槛仍保留；不引入 `run_command`、网络、消息发送或真实性无法验证的外部服务。

## 已有实现支持什么

本机只读参考仓库 HEAD 为 `da3e841ce4fb70d341e6e799648cb703ca96a96e`，working tree 有既存修改；执行时必须固定实际文件 hash，不能仅引用 commit。

| 已读来源 | 具体 API／负载事实 | 本计划据此保留的边界 |
| --- | --- | --- |
| [protocol.py](../../tiny-llm/src/tiny_llm_ref/agent/protocol.py) | `parse_action` 严格校验单个 JSON；`ToolAction` 只有 tool/arguments，没有 intent epoch 或调用 ID | 新增意图 envelope 与调用 ID 属于 harness；不能归因于上游 parser |
| [workspace.py](../../tiny-llm/src/tiny_llm_ref/agent/workspace.py) | `Workspace.execute(action, tool_call_id=None)` 在 247 行；先查已有 receipt，再 preflight／approval，执行真实文件工具，最后写 receipt | 单次调用本身没有异步取消、意图换代或动作队列；缓存 receipt 返回也不重新进入 approval |
| 同文件的 `read_file`／`_current_observed`／`_commit` | 先读内容 hash；批准后再次校验原内容；同目录临时文件经 `os.replace` 替换，随后更新 observed/modified | 防止既存字节过期不等于防止用户意图过期。文件替换先于效果回执，两者没有共同事务；普通替换不是崩溃持久性保证 |
| [receipts.py](../../tiny-llm/src/tiny_llm_ref/agent/receipts.py) | `ReceiptStore.put` 对相同 call ID／相同 receipt 去重；JSONL 可重新加载并验 digest，冲突拒绝 | 有已完成 receipt 时可以不重复执行；没有预写日志、fsync／目录同步或文件效果与日志的原子提交；不能推导 crash exactly-once |
| [loop.py](../../tiny-llm/src/tiny_llm_ref/agent/loop.py) | `_continue_agent` 同步调用 `workspace.execute`，返回后建立 `AgentEvent`，追加 assistant action／`Tool result`；之后才可 checkpoint | “动作执行中”与“工具观察已进入对话”确实是不同阶段；上游 loop 没有迟到结果分类或异步观察队列 |
| [checkpoint.py](../../tiny-llm/src/tiny_llm_ref/agent/checkpoint.py)、[steering.py](../../tiny-llm/src/tiny_llm_ref/agent/steering.py) | `run_to_checkpoint` 保存完整工具观察；`resume_with_steering` 验证该边界、恢复 generator，再追加一个可见 `Operator steering` 消息 | 原 API 的 steering 是安静暂停点恢复，不是 mid-tool interruption。`ModelCheckpoint` 是课程 fake-model 元数据，不含文件回滚、receipt store 或正在运行的工具 |
| [Day 3 tests](../../tiny-llm/tests_refsol/test_week_4_day_3.py)、[Day 4 tests](../../tiny-llm/tests_refsol/test_week_4_day_4.py)、[Day 6 tests](../../tiny-llm/tests_refsol/test_week_4_day_6.py) | 实际负载是 `app.py: answer = 1 → 2`，先读再精确 edit、留 receipt、完整观察后恢复；重复 call ID、文件观察过期和 steering 顺序有已有测试 | 新实验复用这份小型真实文件工作，不人为制造“GPU 工具”或远端任务 |
| [week4-capstone.py](../../tiny-llm/week4-capstone.py)、[课程拆分](../../tiny-llm/docs/week4-day-split.md) | capstone 使用脚本 generator、实际 workspace／receipt；各分支复制自己的 workspace 与 receipt log | 不把 effect-isolated 分支当作共享外部效果的通用 fork，也不把脚本生成器称为真实 LLM 推理 |

[Day 6 文档](../../tiny-llm/book/src/week4-06-steering.md) 明确限定完整观察之后的安静暂停点，不含后台工具、并发中断或 exactly-once reconciliation。[Day 3 文档](../../tiny-llm/book/src/week4-03-safe-editing.md) 也明确文件替换不构成 durable journal。

## 同一个文件、两代意图

每个 case 使用独立新目录，限定在本仓库 `results/.../<attempt>/sandbox/` 下。初始唯一工作文件为 `app.py`，精确 UTF-8 bytes 是 `answer = 1\n`。receipt 与原始事件放在工作文件根之外的本 attempt 目录，避免被模拟工具当作待编辑目标；保留完整最终文件与日志，便于审计。

- intent epoch 0：读取 `app.py`，提案 P0 是 `edit_file(path="app.py", old="1", new="2")`。
- intent epoch 1：用户改为“停止继续编辑，只报告当前文件内容；如果旧编辑已经发生，保留并如实说明”。
- P0 的工具 JSON／参数固定；不同 case 只改变意图换代相对于准入、开始、完成、观察消费的顺序。
- 新意图的最终事实由真实 `read_file` 和完整文件 bytes 校验，不由脚本的 final prose 自证。case 之间不比较吞吐，也不要求副作用次数相同：拒绝旧动作本来就应该减少已执行效果。

工具策略是显式 root、allow_writes=true、allowed_commands=()；harness 附加固定 `app.py` 与精确 edit 参数白名单。上游 approval callback 仅批准已授权的该沙盒动作。实验不需要再次向真实用户询问每个模拟提案；intent 换代是 fixture 控制事件，并非试图推断用户此刻的意图。

## 最小协议：先定义选择，再验证

引入四个相互独立的身份：`intent_epoch`、`proposal_id`、单次消费的 `ticket_id`、稳定的 `tool_call_id`。proposal 保存规范 JSON bytes 与 digest，不能因 `ToolAction.arguments` 是可变 dict 而在准入后偷换动作；start 时从被固定的 bytes 重建并再次核对实际参数。

同一个 controller mutex 排序 `intent.advanced`、`proposal.admitted`、`action.start_claimed`，并在锁内分配递增 event sequence。只有一个工具 worker，至多一个 started 动作；不在该锁内执行文件操作或等待 gate。

| 当前阶段 | epoch 改变后的首轮规则 |
| --- | --- |
| proposed，尚未准入 | `admit` 比较 proposal epoch 与 current epoch，不一致就拒绝；没有 ticket、Workspace 调用、receipt 或文件效果 |
| admitted，尚未 start | `start` 再检查 epoch；过期 ticket 转为 `revoked_before_start`，禁止调用 Workspace。准入不是永久执行许可 |
| start 已 claim | 保留该动作的完成／收尾义务，不把 stop 改写成“效果未发生”。本轮允许它按原授权完成，不补偿、不撤销已落盘文件 |
| 工具已返回，观察尚未消费 | 按原 call ID／epoch 保存结果、文件证据和 receipt；消费时标记 `historical_effect`，不能计作 epoch 1 的目标完成 |
| 已消费的完整观察 | 作为历史事实保留；正常 checkpoint／steering 可在它之后继续，新指令不能改写已经发生的事实 |

`start_claimed` 是本协议选择的线性化边界，先于真实 `Workspace.execute` 入口；它不是 `os.replace` 的时间点。记录两者，避免把任务被派发当作文件已经修改。本轮不承诺“意图改变后绝无旧效果落盘”，只承诺“过期提案／未开始 ticket 不再开始”和“已开始动作的效果不失账”。也不声称已 start 的文件工具客观上不能协作停止：那是另一种需要单独比较的策略。

新意图的后续文件操作等待旧 started 动作完成并完成记账，避免跨线程共享上游 `Workspace._observed` 的未定义行为。intent ACK 可以先记录，不能将 ACK 冒充工具已结束、receipt 已写入或当前结果已经可用。

建议首轮只需这几个调用：`propose`、`admit`、`advance_intent`、`start`、`finish`、`consume_observation`。不引入 effect runtime、事务系统、全局调度器或可回滚文件树。

## 固定 trace 与可判定结果

| Case | 事件顺序与 gate | 应观察到的真实结果 |
| --- | --- | --- |
| E0 stale-before-admission | P0 发布 → epoch 1 → 尝试 admit | stale 拒绝；0 次 effect Workspace 调用／commit／receipt；`app.py` 仍为 1 |
| E1 admitted-before-start | admit P0 → epoch 1 → 尝试 start ticket | admitted 事实保留，但 ticket 被撤销；0 次 effect 调用／commit／receipt；文件仍为 1 |
| E2 started-before-commit | start claim → 进入真实 Workspace/preflight/approval → `_commit` 入口 gate → epoch 1 → 放行原 `_commit` | 真正编辑到 2，恰一次 commit 与 receipt；原 epoch 结果作为历史效果留存，epoch 1 后续读取为 2；不伪造撤销 |
| E3 completed-before-observation | 正常执行 edit、receipt 写入、Workspace 返回 → observation-consume gate → epoch 1 → 消费结果 | 文件与 receipt 在换代前已经完成；迟到的是观察消费。原结果／receipt 全部保留，`historical_effect` 不使当前意图自动成功；后续读为 2 |
| R normal-checkpoint-steer | 上游 run_to_checkpoint 在 read+edit 两次完整工具观察后暂停 → 新脚本 generator + fresh Workspace／重开 ReceiptStore → resume_with_steering → read + final | 原始 edit 完整观察与 steering 在 transcript 中顺序稳定；恢复不重放 edit；全程1次 commit／1条效果 receipt；最终 bytes 仍为 2 |

E2 在已进入工具、再次 preflight／批准之后且文件替换尚未开始的明确 hook 暂停。gate 只确定顺序：不能声称意图改变发生在内核文件写的中途、不能将等待 gate 的时长当作实际服务耗时。E3 不插手原工具执行，只在结果返回后的新 harness 观察队列暂停。两者区分“工具尚未结束”和“工具已结束但新控制状态尚未消费结果”。

R 使用真实 `run_to_checkpoint`／`resume_with_steering`，其脚本 generator 的 fake checkpoint 按上游 Day 4/6 测试构造，明确没有模型/KV 计算。序列固定为 read、edit、read、final；after_tool_calls=2，恢复到 response index 2，保留旧观察并追加一个 steering。它是已完成边界的正常恢复，不是异步 E2 的上游实现，也不是掉电恢复。

另保存一个小型失败反例 F，不混入以上正常 case：给实际 `ReceiptStore` 指定 attempt 内尚不存在的父目录；真实 edit 成功后，原 `ReceiptStore.put` 的 append 因缺少父目录失败。`Workspace.execute` 会返回 `error: could not append receipt log`，但 `app.py` 已经是 2、commit 次数为 1、receipt 缺席。记录为 `effect_committed_receipt_failed`，禁止静默重试；`execution_succeeded=false` 与“预期反例被正确识别”的 `validation_passed` 分开。这是可控文件 I/O 失败，不是 crash 注入。未知错误或未到达指定失败点一律不通过。

## 如何接入真实代码而不加载模型

已经用系统 Python 在私有 package 名下导入未修改的七个模块：protocol、generation、checkpoint、receipts、workspace、loop、steering；只创建模块定义，没有创建 Workspace、执行工具、import MLX／NumPy 或载入模型。直接 import `tiny_llm_ref` 会先执行其包初始化并导入模型，因此下一步 loader 应按固定源码 snapshot 为这七个模块提供私有 package 的相对导入环境，不执行原包 `__init__`。这是 harness 的加载方式，不是把课程接口改写一份。

工具体必须调用上游原方法。观测用小 subclass：`ObservedWorkspace._commit` 只在入口放 gate，随后调用 `super()._commit`，并在返回后记录完整文件 bytes/hash 与 commit 计数；不复制／替换提交算法。该返回点证明文件替换已经返回，但仍不是精确的内核 commit timestamp。ReceiptStore 的观测 wrapper 调用原 `put`，保留 append 的成功或原始异常；不伪造 receipt。

控制线程只换代、开 gate；worker 单线程拥有 Workspace／ReceiptStore。gate 用 `threading.Event`，有界 watchdog 保留部分状态，finally 放行并 join 已存在 worker。禁止 `sleep` 伪造计算；不执行任何性能测试。错误之后不能只写“passed:false”而丢失已写文件或原异常。

## 证据、轻 CPU 负例与完成门槛

新 experiment 可命名为 `tiny-intent-effects`，schema v1。raw 至少保存：

- 固定 fixture／case、source snapshot manifest、Python 与实际导入路径；外部参考源码只读，attempt 在执行前固化，后改 working README 不影响既有证据。
- 严格递增 event sequence、host monotonic timestamp、thread、epoch、proposal／ticket／call ID 与 canonical action hash。以 sequence 判定先后，不能只靠可能相同的时间戳。
- proposed/admitted/rejected/start_claimed/execute_entered、approval、commit entered/returned、receipt attempted/recorded/failed、execute returned、observation produced/consumed、intent advanced、worker joined。拒绝事件附原因，不能凭最终无文件差异反推“从未执行”。
- 每个关键稳定边界的精确 `app.py` bytes/hash、实际 receipt JSONL bytes、receipt canonical digest、修改记录、effect call／commit／receipt append／观察消费次数。临时文件清理与 worker join 不会撤销目的文件的既存效果。
- E2/E3 的两个 epoch 均保留；旧结果是否被当前意图采用单独记录，不通过改写结果字符串抹掉它。R 保留完整 checkpoint、恢复前后 transcript、下一 response index、receipt 重开前后 bytes。
- F 的原始错误、effect/receipt 分离状态与禁止重试；其它失败保留所有已有事件和文件 evidence，不把未知错误分类成预期拒绝。

轻 CPU 协议负例优先覆盖：同 epoch 双 start／重复消费 ticket；准入后参数被改；epoch 先换代但旧 start 被允许；旧观察被标为当前意图成功；已开始动作在账本中被直接删除；已有 receipt 的同 call ID／同参数返回历史结果而不重做、不同参数冲突；receipt 字节被改；未完成工具观察就 checkpoint／steer；resume 重放 edit；F 未真正造成 append 错误或把文件已改误记为未发生。事件重放器还应拒绝相同时间戳下把事件索引顺序倒置的伪 trace。

第一轮验收依靠明确的文件、回执与事件关系，不报告 ACK、工具服务时间或恢复速度的性能差异。gates 是定序实验设施。只有上述关系成立，才考虑真实模型提案、工具结果进入新 prompt 后的首个有效输出，或者与共享 KV 分支组合。

## 来源指纹与后续范围

本次只读导入探针绑定的七个实际文件 SHA256：

| 文件 | SHA256 |
| --- | --- |
| protocol.py | `c9a7c15a8fd6d8e59d317253159405426381496a1ecbbea0be017bdbb7a9b464` |
| generation.py | `482316433b8976e08dc4b357e2c5fe296e3da09f465e89c017f413885f442a99` |
| checkpoint.py | `ae814fd810c058b306d482a940638f1af72762f542f9de1c1c445cf871143401` |
| receipts.py | `a267493f08aee67dcd43de5f707ed1ae772bad7fd7aecae25477d1a04cd23909` |
| workspace.py | `ad26c64f7d11518f1fedcc171e49c8cd605926255a576fd752e0efb7c75d0153` |
| loop.py | `f19347b610955b28d404e4d825d6aac755b36c47a9ed38abc4427d908edbb93a` |
| steering.py | `28a5e8dc4e65c0c01de1f1d909be945d26a7e8466eb824ed72707378f56a7c94` |

仍未覆盖：敌对文件系统/外部写入竞态、通用子进程取消、第三方服务幂等键、多进程 receipt 并发、掉电后 reconciliation、事务文件系统或补偿动作。读取 receipt 证明历史记录存在，不证明文件此刻仍有相同内容；本轮单写者假设下也要重新检查真实 bytes。

与共享 KV 实验可以讨论的共同点是“停止意图不自动消除既存义务”。不同点必须保留：KV 最后一个访问结束后能归还 slot；文件效果已经改变应用世界，释放句柄不能将它撤销。PL 候选需要分别表达新动作许可、已开始效果的完成义务、事实观察的来源与当前意图的关系；本阶段只验证最小动态协议，不宣称静态保证或端到端响应性定理。
