# P4 组合门槛：共享消费者、等待取消、嵌套清理与能力逃逸

2026-10-30。C1/C2已通过[独立验收](../results/batch5/owner-composition-REPORT.md)：不可变attempt-1编译/运行11个CPU情境，完整数组/事件/来源重放与50项检查器测试通过；另5项ledger自测仅有结果记录，不冒充独立设备故障轨迹。C4a已通过[独立审阅与正式归档](../results/batch5/capability-fixtures-REPORT.md)：14个独立module/client样例，8个真实CPU运行正例、6个明确语义编译拒绝，18项检查器测试通过；保留working driver未从snapshot重新执行、object/module原运行hash未单独保存及数值仅摘要的来源边界。C4b已完成[独立复核](../results/batch5/lifetime-fixtures-REPORT.md)：18例中8个CPU实跑、10个编译拒绝，完整数组/事件/来源/实际诊断及31项检查器测试通过；原推断数组诊断失配导致的required-controls=false、validation=null和退出1仍保留，显式数组Escapable单探针另列。B1动态live lease与B2实验性Array值借用分别报告，没有获得静态异步live-buffer寿命保证。C3首个有界CPU原型已通过[独立验收](../results/batch5/nested-obligations-REPORT.md)：冻结attempt-2的13个真实情境覆盖25个producer、19个有效完整数组及10个登记child，33项独立检查器测试通过；6个原execution_succeeded=false保留。13项Swift ledger另分10项可重放快照与3项bool-only自报，不作为设备unknown恢复证据。attempt-1因LocalCompletion原invocation绑定缺口保留未执行，修订后新增跨run真实等待。该验收只覆盖旧13情境；后续必要read-cleanup异常已由A0–A4独立验收补齐，父live存储正常读取组合随后由B0–B4独立验收；借用读取抛错C0已通过下述独立验收；未知设备访问与一般循环/多资源仍是后续门槛。prompt-detach八case已实际执行并独立验收，性能及Metal迁移仍未实现。未修改冻结Core/CPU owner/Metal owner/alias旧证据，没有GPU运行；设备smoke仍由root排程。

## 要隔离的问题

2026-10-30 后续进展：新 [swift-nested-access 第一轨](../results/batch5/nested-access-REPORT.md) 的 A0–A4 已通过独立验收，63 项检查器测试通过。17 个 owner run、80 遍完整读取（10,240 个值）、6 个注册 child 和七个原故障均重放；四个父请求及全部 owner 中九个业务 false 保留。它补的是 child 必须完成的真实异步 read-cleanup throw 与原错误传播，不把已有 C2 scan 异常重新称为首次实现。父 live lease 的第二轨 [B0–B4正式报告](../results/batch5/nested-borrow-REPORT.md) 已完成实际编译/五case、独立87遍完整读取及94项检查器测试，原B4业务失败保留；父存储借用实际抛错的新[C0两情境](../results/batch5/nested-borrow-fault-REPORT.md)已实际编译执行并独立验收：8个run/4child/45遍完整读取/5760值，故障例X/Y业务false保留，同一原故障贯穿Y逻辑身份及X资源身份，实际join/lease关闭后安全复用。21项driver与44项独立测试通过，首轮两项变异器TypeError保留；未知完成、Metal、一般循环等门槛不变。原 attempt-1 的 Python optional KeyError、独立 checker v1 的来源清单/字面 argv 失配及全部原始证据保留。

一个 producer 生成一份数据，两个消费者 C1/C2 需要它。用户取消 C1，C2 仍需要结果；资源 owner 随后要把相同存储交给 B。关键问题是：谁能够取消 producer，谁能够结束自己的等待，谁还在实际访问存储，谁必须等待这些访问和子清理完成。四种职责不能由一个 `cancelled` 或引用计数代替。

分三种共享对象，分别验收：

1. **已复制结果**：现有 `ReadOnlyResult` 只有 checksum、visits 等值，不含存储访问能力；任意消费者读取它都不延长 buffer 的访问期。
2. **同一未完成 producer 的结果句柄**：多个消费者等待同一个任务；等待者个数与实际 buffer 访问个数不是同一个量。最后一个等待者离开也不自动消除 producer 的完成义务。
3. **仍指向同一存储的只读访问租约**：多个消费者继续真实读取同一代 buffer；最后一个实际读取义务结束前，writer B 不能覆盖它。C2已有真实CPU存储及独立验收证据；Metal版本未实现。

这是一组独立 P4 协议／接口实验，不接入 P5 的布局／执行位置、P6 的设备控制循环或 P7 的模型运行时。P7 的共享页情境提供动机，但它的 Python refcount 结果不能替代 Swift 接口的组合证据。

## 已有接口实际保证了什么

| 依据 | 已有事实 | 本计划仍需补的门槛 |
| --- | --- | --- |
| [Core 的 Completion / ReadOnlyResult](../experiments/swift-obligation-scope/Sources/ObligationCore/Core.swift) | `Completion` 可复制且保存 producer；`value()` 直接等待 producer；scope 独立保留所有已提交句柄；结果为复制摘要 | `Completion` 同时公开 `cancelWork()`，普通结果消费者也获得 producer 取消权；没有 live-buffer 读租约 |
| [Core CPU tests](../experiments/swift-obligation-scope/Tests/ObligationCoreTests/ProtocolTests.swift) | 已测试两个 waiter，取消一个只标记该 task，仍等待结果，另一 waiter 和 producer 继续；也测试普通结果合法逃逸、动态撤销和隐藏 continuation | 不能把已有 waiter 测试写成尚未实现；但它不覆盖 owner 许可组合、prompt detach、嵌套清理或 Metal waiter 取消。进入等待函数的信号不等于 runtime 内部依赖边登记证据 |
| [CPU ResourceOwner](../experiments/swift-resource-owner/Owner.swift) | 私下构造数组/backend；独占 session 的 epoch 与 phase 动态检查；body 返回后拒绝新提交，完整 drain 后归还；忙时立即拒绝 | 没有共享 reader 账本或子 scope 义务树。公开 `ExclusiveSession` 是可复制引用，逃逸在运行时撤销，不是编译期禁止 |
| [MetalResourceOwner](../experiments/swift-resource-owner-metal/MetalOwner.swift) 与 [Ledger](../experiments/swift-resource-owner-metal/Protocol.swift) | 私有 buffer、每 session 最多一个命令；成功 callback 后还必须完成 CPU 全量读取和 drain；未知／错误完成保留 quarantine/unresolved | 四次 smoke 的取消对象是 producer，不是 waiter 或 scope task。新增读租约必须延长 callback→readDone→release 链，不能把 callback 当成所有消费者访问结束 |
| [alias 反例及最小修复设计](../experiments/swift-scope-alias/REPAIR_DESIGN.md) | 两份局部 scope 账本不能阻止同一 backend 别名；统一私有 owner 修复了已测试的交错 | 新接口必须继续私藏真实资源，不能重新允许任意 backend／buffer 注入多个 owner |
| [四个真实编译 fixture](../experiments/swift-obligation-scope/verification/compile-fixtures/report.json) | 本机 Swift 6.3.3 已验证 `~Copyable` 的显式 copy／重复 consume 被拒绝，单次 consume 与离开作用域时 drop 合法 | 这些是独立 token 示例，未接入资源协议。不可复制没有证明必须 await、异步清理、非逃逸或资源访问已完成 |

CPU Core 的异常回收依赖 `ResourceBackend.write` 在返回／抛错前已结束访问的约定。Metal 独立 ledger 刻意不使用“任意 throw 即可回收”的规则。组合实验不能把两条错误路径合并为一个无条件释放分支。

## C1：共享结果与单 waiter 取消，先固定权限和语义

先在新的独立目录实现很小的公开包装，底层只读调用已有 CPU owner。公开给消费者的 `SharedResult` 只提供 `value()`；producer 取消控制留在创建者／scope 一侧。包装的公开构造不能接受让客户端同时保留取消权的底层 `Completion`，也不暴露原 owner/session。这里验证的是 API 权限分离；如果底层句柄仍从其他公开入口泄漏，必须记为失败。

第一种语义沿用既有直接等待：取消 C1 只设置 C1 task 的取消状态；C1 继续等待同一 producer 的实际结果。C1/C2 都得到同一复制值；producer 的工作、必要读取和 owner drain 完整发生。记录 `waiter.cancel_requested`、取消状态采样、`wait.call_entered`、`wait.returned`，不要把取消请求受理叫作“等待已经结束”。这一步把既有事实移入 owner 公开边界，并验证消费者没有 producer 取消权限。

第二种语义是**另一个明确命名的候选接口**：C1 的等待调用可以在取消后以 `CancellationError` 返回，但共享 producer 与 C2 继续。不要悄悄改变 `value()` 的语义。它需要独立的 waiter 注册状态 `waiting → delivered | detached` 和一次性结果仲裁；取消与完成竞争时只能发生一个终态，不能重复 resume。取消早于登记、登记后但完成前、完成同时、完成后四种确定性顺序都必须保留。

对 prompt-detach 候选，owner/scope 必须继续保留 producer；即使全部 waiter 都离开，已登记访问也仍须收束。`wait` 返回和外层 `withExclusiveSession` 返回是两个边界：后者仍可为 drain 等待。不能因内层取消迅速返回就声称整个请求／scope 已完成。

2026-10-30，新的 [swift-cancellable-wait 设计](../experiments/swift-cancellable-wait/DESIGN.md)已固定八个CPU情境，28项Python检查通过；[冻结attempt-1](../experiments/swift-cancellable-wait/attempts/attempt-1/report.json)首次编译与八case共10stage均exit0、无diagnostics/timeout，运行侧检查及来源绑定通过，[完整独立验收](../results/batch5/cancellable-wait-REPORT.md)已通过32完整数组/4096读取、17waits/16cells、原故障和安全复用，97项检查器测试通过、113输入不变。首次版本banner和私有错误类型反射失配保留，实际Swift未重跑。两故障case的A业务false保留；竞争case本次取消获胜，不重跑取分支。保留 `value()` 的直接等待，另加 `waitCancellable()`。首版由已登记 producer 自身完成真实写入与两遍必要读取后发布复制 outcome，scope 继续实际 await 原 Task，因而没有桥接 Task。单 waiter 的取消／结果交付只仲裁一次；结果已经获胜后，不以调用者后来被标记取消改写原结果。两个等待者都实际返回取消且 body 已返回后，再检查必要读取仍 pending 时 B 被拒绝；另保留 producer 随后真实抛错、原错误由 scope 收束的情境。冻结前静审新增第八例：两个未取消的等待者和 scope 都接收同一个原 producer 故障，以实际覆盖失败交付，而非仅覆盖无人等待后的故障收束。固定 allocation 的下一 epoch 覆写后，旧完成结果仍可再次取得，且不得发生新访问。

独立验收应区分 continuation 的仲裁、调用 `resume` 前的日志、真实调用返回和 waiter 返回：`resume` 返回后的日志可能晚于被唤醒 waiter 的返回，不能强加不存在的先后关系。Hub 可以保留已 detached 的 cell 到结果发布，但保留的登记项不等于活跃等待者，也不证明存储访问仍在发生。当前只固定这些待验语义，没有取消响应时限或优先级传播结论。

需要一个桥接任务时，将它登记为明确义务，记录它实际结束的时刻；不能把未结束的 bridge 藏到游离 task 里。用会等待全部子任务退出的组合来竞速取消时，必须观察实际返回，不能仅凭取消了竞速输家就推断 prompt 返回。

直接 `Task.value`、producer-free continuation 通知与显式需求转发分别标记。prompt-detach 的 continuation 包装可能隐藏 producer 关系；它通过安全检查不代表保留了 priority 传播。既有 P3 已说明这一缺口，本阶段仅采样 actual priority／回调并保留依赖身份，不再人为 gate 等待提优后才放行工作，不做速度排序。也不假定取消 waiter 能撤销曾经发生的优先级提升。

## C2：共享 live-buffer 只读租约，补真正的访问组合

这一步需要新的 CPU owner 原型，不能通过给旧 `ReadOnlyResult` 改名来通过。公开构造仍自己分配并私藏数组；只读 API 返回复制扫描结果，不能暴露 pointer、mutable buffer、原 backend 或可由客户端任意写入的视图。

生产者完成写与必要初始化后，owner 可以在当前 epoch 内登记两个 `ReadLease`。每个租约带 owner 身份、epoch、generation、lease ID；入口在同一锁内验证并登记实际读取，锁内不 await。消费者用受检查的读方法进行真实 UInt32 扫描，完成后返回复制摘要。第一版可以在读操作内部用锁保证数组无数据竞争；证明的是共享租约的有效期组合，不宣称同时占用多个 CPU 核心或无锁读取。

准入单位明确为一次有界多遍读取调用：`scan(passes: 3)` 登记一次实际访问义务，其内部三遍扫描与扫描间的测试协调都属于这个已登记操作。owner 关闭准入后，它仍能履行余下遍数；不会把每一遍错误地当作新调用而切断清理。另一次新 `scan` 调用则必须重新检查准入，即使调用者还持有旧 lease。这样既能拒绝关闭后的新访问，也不会撤销已开始的访问义务。

状态中至少分开保存：

- `producer_accesses_unresolved`：尚无真实完成观察的生产者访问。
- `reader_leases`：已登记且尚未关闭的只读租约集合。
- `reader_accesses`：已经获准、尚未结束的实际读取集合。
- `child_obligations`：为本资源存续的子 scope／清理义务集合。
- `admission`：开放／关闭；epoch 与 generation 不因 waiter 取消而变化。

body 退出先关闭新访问准入，再 drain 已登记义务。只在 producer 访问已确认、必要 cleanup 已结束、读租约／在途读取／依赖子义务均为空时，才归还该 owner 的许可。关闭准入不能使已进入的读取凭空完成；取消一个 reader task 也不能直接减去它正在执行的访问计数。

第一批真实 CPU 数值选择固定 128 个 UInt32 元素，producer 每元素 31 次既有 LCG，producer cleanup 两遍，两个 reader 各三遍，B 使用不同 seed 重写同一数组并校验。完整读取总量预先列出；取消先采用标记后继续完成既定读取的语义，以保持工作固定。若后续允许 reader 提前停止剩余业务读取，必须单独记录实际完成遍数，不把缩减工作称为接口加速。

用 gates 形成可解释的安全顺序：producer 写完；C1 完成一次真实扫描后暂停于扫描外；C2 仍持有效租约；尝试 B 必须在写入前被 busy 拒绝；关闭 C1 不能使 B 获准；C2 全部读取结束并关闭后 B 才取得新 epoch。gate 只选择测试顺序，所有“已读”证据来自真实数组扫描，不用 sleep 或 gate 等待时长作性能结论。

租约首版由 `withReadLease` 的实际异步返回路径关闭，scope 注册并保留消费者义务；不用 `deinit` 伪装 async cleanup。普通复制结果可以返回到 scope 外。租约对象即使被复制或存到外部，也必须在租约关闭后、owner 关闭准入后或新 epoch 中拒绝**新**读取；已经被准入的读取继续由 owner 负责收束。

registry 不能保证任意用户异步 body 都会终止。若消费者无限等待或拒绝返回，安全策略可以继续保留租约而无法完成 drain；本阶段只为固定有界算术与明确放行的测试给出进展证据，不把资源安全误写成取消后必然有界返回。

对应 Metal 的迁移门槛另列，不在此批执行：成功 callback 只使 producer 访问结束；随后 reader 租约仍可延长同一 buffer 的不可覆盖期。必须同时保留真实 buffer 身份、generation、成功完成观察、最后一次实际 reader 结束与 B submit 次序。设备完成未知时，即使 reader 数量为零，也不能归还。CPU 状态负例只能验证这一拒绝规则，不能称为设备故障恢复。

## C3：嵌套异步清理，先区分两种嵌套

当前首批实测见 [C3 正式报告](../results/batch5/nested-obligations-REPORT.md)。N1的同owner准入前busy、N2的独立owner child完整run返回、N4的父取消/丢handle/先失败兄弟、N5的登记先于关闭与关闭先于登记两种确定顺序均有实际CPU事件与独立重放。N3保留X/Y body及X/Y/Z producer/backendAfterAccess共五个原身份，但backendAfterAccess发生在写访问结束后、正常read cleanup之前，**没有测试真正read-cleanup抛错**。不同owner有独立数组；child持父live lease没有在本批实现。下面完整门槛继续保留，不能用首个原型验收替代全部要求。

上述“没有 read-cleanup 抛错”限定于旧 13 情境。随后新增的 [nested-access A0–A4](../experiments/swift-nested-access/README.md) 已独立核验 child body 退出后的预登记读取、真实第二遍结束后的 throw/部分证据，以及父取消/失败兄弟与原 fault 身份。随后 [第二轨B0–B4](../results/batch5/nested-borrow-REPORT.md) 已实证 parent child/lease/read 同时登记、busy-after-commit 的零访问 abort/join，以及关闭后拒绝新登记事务。B3旧身份分类仅作诊断；本轮只有一次预登记读取，不声称通用新scan接口。A轨抛错与B轨父存储正常借用仍需真实组合，不能由两份独立成功记录推出借用读取抛错已验收。

实际C3复合trace与各owner局部Recorder分开：跨scope用组合事件索引连接注册、child实际run返回和父join，不跨owner相减局部时钟。底层CPU owner是私有Swift Array值，不宣称同一物理地址。13个ledger检查中10项有before/after快照、3项只有布尔自报；unknown-child是CPU状态拒绝规则，没有人工触发owner或设备访问未知。scope.owner_returned的原faults字段是最终merge前观察条数，本地await故障的2次报告与最终1个原身份/2个渠道分别核对。

**再次申请同一 owner**：保持已有忙时立即拒绝策略。外层持有独占许可时，内层 `withExclusiveSession` 不获得第二份许可，也不偷偷排队等待自己。记录拒绝发生在内层 producer 登记／backend 访问之前。多资源等待队列、任意获取顺序、公平性与死锁解决仍留在路线中，不用本次 busy 拒绝宣称这些问题已解决。

**两个私有 owner 的嵌套 scope**：先用 X/Y 两份独立 CPU 数组形成最小真异步例子。外层 X 的 body 启动内层 Y；内层 body 返回或抛错时，Y 的一个真实读取／收尾义务还没有结束。外层不得仅凭内层 body 结束就记 `child.drained`。被登记的 child drain task 必须真实返回，或者明确保留未收束状态。普通父子函数调用已经 await 的情形作为基线；把 child 包装成 detached task 后丢掉句柄作为待拒绝／待管理的误用。

新 registry 必须先原子登记 child obligation，再允许它启动可观察工作；父 scope 关闭时停止登记新 child，冻结已有登记集合并等待所有成员。一个 child 的失败不能跳过兄弟义务。父 scope 的调用 task 被取消时，剩余清理仍须执行。必要等待句柄保留 producer／child 身份；任意 continuation 再包装可能丢失需求传播，不能因结构叫作“nested scope”就认定继承了 P3 的性质。

错误输出使用结构化树或带 `scope_id / obligation_id / origin` 的列表，同时保留外层 body 错误、内层 body 错误、producer／reader cleanup 错误。两条同名错误若来自不同义务也不得按字符串去重。没有未完成访问时，失败的 scope 可以完成安全 drain 并把全部原错误交给调用者；“结果失败”与“资源是否可复用”分开记录。

若某个 child 的访问完成未知，复合 scope 不能报告全部 drained。独立且已经安全收束的其他 owner 可以各自归还，但必须保留那个 child 的根和 unresolved 身份，不能用其他资源已经空闲来覆盖它。如果 child 正持有父 owner 的共享租约，父 owner 也必须继续保持占用。这一条件先做 CPU ledger 拒绝测试；不人工诱发真实设备故障。

## C4：访问能力逃逸与真实 Swift 编译样例

把每个案例归入“编译期拒绝”“编译通过后动态拒绝”“合法使用”“当前接口不能局部约束”，而不是只统计负例有多少个。C4a已用独立module/client完成14个真实样例，具体8正6负与18项独立检查器测试见[报告](../results/batch5/capability-fixtures-REPORT.md)。C4b的18例及补充数组探针已独立复核，但原必要控制诊断不匹配仍保留；下面分别列已有证据与后续门槛，不能把两批称完整C4。每一例都需独立最小 client 文件，库模块与 client 分开编译，保留真实命令、返回码、诊断和 Swift 版本。

| fixture | 当前证据／待检验结果 | 允许声称的性质 |
| --- | --- | --- |
| `copied_result_escape` | C4a编译并运行成功，scope 关闭／下一 epoch 后摘要仍有效 | 复制普通值不依赖资源访问期 |
| `completed_result_handle_escape` | C4a在scope返回及B复用后再次 `value()`，均得到A摘要 | 结果等待权和新访问权不同；可能仍保留对象内存，另记代价 |
| `escaped_dynamic_session` | C4a编译通过；scope 后旧epoch submit实际抛expiredPermit | epoch 撤销，不是静态非逃逸 |
| `escaped_dynamic_read_lease` | C4b B1独立client可保存lease；scope后新scan实际closing、B换代后expired，完整数组/身份/无新增访问重放通过；准入关闭前已登记scan继续完成 | 动态逐访问授权与义务保留；不能声称类型保证词法寿命或一般child registry |
| `consumer_cannot_cancel_producer` | C4a结果facet无cancelWork成员而编译拒绝，正常value对照通过 | 公开 API 权限分离，不证明 producer 已完成 |
| `cannot_construct_or_extract_backend` | C4a仅验证SharedResult底层构造器与SharedResultOwner.owner私有成员拒绝；其余具体路径待验 | 模块封装；不包含 unsafe/FFI 绕过的证明 |
| `affine_copy` / `affine_double_consume` | C4a真实completion-backed包装的copy与重复consume被拒绝，单次消费正例通过 | 仅不可复制／使用次数约束 |
| `affine_drop_without_await` | C4a合法drop不await，实际producer/cleanup/drain仍由scope完成 | `~Copyable` 不能单独表达 must-await |
| `borrow_capture_in_detached_task` | C4a实际诊断为borrowed parameter被escaping closure捕获；本地borrow控制通过 | 区分借用寿命、Sendable、隔离与语法错误，不能把任一编译失败统称资源安全 |
| `read_lease_return_from_body` | C4b B1动态lease可合法返回并在后续访问时撤销；B2独立Array值的本地/复制/forward/借用结束后覆盖/跨await实跑正确，局部来源逃逸被寿命诊断拒绝 | B1动态live存储与B2实验性值借用不同；未证明真实suspension、一般callback或静态异步live-buffer寿命 |
| `hidden_continuation_wrapper` | C4a可编译并取得正确摘要／drain；本样例未独立测量producer需求关系 | 类型可接受不表示调度需求自动传播；不得由本fixture判定其保留或丢失 |
| `detached_child_handle_drop` | C4a仅有外部Gate；C3新registry已对真实Task.detached的drop/父取消/失败兄弟有界情境逐身份登记并join | 完成义务由新registry显式保留，不是Swift自动结构化任意detached任务；有界父live lease已由B0–B4验收，一般循环与借用读取抛错组合仍待验 |

C4a的 `AffineResult` 公开接受可复制 `SharedResult`，因此包装不可复制不等于唯一资源authority；多个包装仍可关联同一完成句柄。本批raw只保留实际摘要和事件，没有全数组元素。库/client来自attempt副本，但driver执行的是工作文件进程，object/module原运行hash未单独保存，完整边界见正式报告。

C4b已把真正live ReadLease的动态路径放到独立module/client边界，并另用私有Array值探索静态寿命；两者还未集成。原推断数组只产生Any转换诊断、必要控制仍失败；单独显式数组才给出Escapable拒绝。detached与非并发escaping控制同为寿命诊断，不能归因为Sendable；跨Task.yield的正例不证明实际suspension；具体callback还包含编译器未支持的capture note。下一门槛是先明确真正live存储、实际异步访问登记/结束与静态借用的连接，另保留真实suspension、一般callback、Metal及接口成本，不能用B2的Array/COW数值正确宣称同buffer原地保证。C3首个CPU原型已对明确登记的child完成、关闭、父取消、兄弟故障与原invocation传播提供独立证据；随后A0–A4已补齐必要read-cleanup抛错与child故障组合，B0–B4已验证child持父live lease；借用父存储时实际抛错的新C0已通过独立验收，实际unknown设备完成仍待验证。C4a/C4b的外部Gate和C3的CPU ledger负例不替代后续组合证据。

单纯把 `ExclusiveSession` 改成不可复制 token 可能仍允许把它移动出作用域，也可能误伤多个合法只读消费者。编译样例必须同时覆盖这些合法路径，显示实际取舍。`Sendable` 也不能直接充当独占、读写阶段或完成证明。新类型与具体 syntax 的支持结论以本机真实编译为准，不能引用旧四个独立 token fixture 代替。

## 最小误用与异常矩阵

| ID | 确定情境 | 应检查的独立事实 |
| --- | --- | --- |
| S1 | C1/C2 等同一 producer，取消 C1，producer 暂未完成 | C1 被标记但 retain-wait 未返回；C2、producer 未被隐式取消；一次 producer、两份相同结果 |
| S2 | 丢弃 C1 结果句柄，随后两个 waiter 都离开 | registry 仍有 producer 完成义务；不能按 waiter=0 归还 |
| S3 | prompt-detach 的取消早／晚／并发完成四个顺序 | 每 waiter 一个终态、无重复 continuation resume、producer 义务不减；取消后返回语义精确 |
| S4 | 普通结果消费者请求取消 producer | 新公开类型无该方法；旧 Completion 允许的行为保留为基线差异 |
| R1 | 两个真实 reader 持同代只读租约，C1 先关闭 | B 在任何写前被拒绝，C2 的全量读取仍正确 |
| R2 | 取消正在读的 C1 | 已准入读取完成前不能减计数／复用；首版完成固定遍数 |
| R3 | 只剩 escaped lease 对象，但其词法访问 scope 已关闭 | stale read 被拒绝且真实读取计数不增加；对象引用本身不冒充有效租约 |
| R4 | owner 关闭准入与新 read 同时竞争 | 在同一线性化点判定：要么读已登记并被 drain，要么提交前拒绝，无第三种漏记路径 |
| R5 | 最后 reader 结束，但 producer completion 未确认 | 仍不能回收；CPU ledger 保留 unresolved，明确不是实际 GPU 故障试验 |
| N1 | 外层持有 owner，再嵌套申请同一 owner | 明确 busy，内层零提交；不引入隐性自等待 |
| N2 | X/Y 两 owner，inner body 返回但真实 child cleanup 未完 | child 与 parent drain 分别记录；复合返回不能早于已登记 child drain |
| N3 | outer body 错误、inner body 错误、多个 cleanup 错误 | 全部错误身份保留，其他义务仍完成；安全归还与业务失败分别输出 |
| N4 | 父 task 取消、child handle 被丢弃、一个兄弟先失败 | registry 不丢义务；所有已登记身份各有终态，未确认的保留占用 |
| N5 | child 新登记与 parent closing 同时竞争 | 登记成功则计入最终 drain，失败则不得启动访问；计数相等不能替代身份集合相等 |
| E1 | 旧 owner epoch、旧 generation、另一 owner 的 permit | 动态拒绝在实际访问之前；不能仅比较整数 generation |
| E2 | 复制值／完成结果合法逃逸，对照 session／lease 逃逸 | 不误报普通值；资源能力失效不破坏已复制结果 |
| E3 | 非复制 token 被合法 drop、或移动后逃逸 | 实际编译分类；不把编译通过改写为类型已保证 drain |
| E4 | 完成错误／超时／诊断失败被包装成正常成功 | 原错误和 unresolved 保留；无伪造 release/drained；外层报告不因诊断二次异常丢证据 |

这些都是正常／误用路径，不能用“这是 trusted backend”作为共享或跨 scope 漏记的结束解释。可信边界限定为私有构造和实际访问完成回报；在这个边界内仍必须证明登记、关闭、复用和异常组合成立。现有 Metal 的真实设备故障、watchdog 触发及多资源队列门槛继续保留，CPU 负例不替代它们。

## 事件、独立核查与交付顺序

新增实验独立目录建议 `experiments/swift-owner-composition`，仅只读引用所需冻结实现；新共享 ledger 与候选接口放在新来源中。结果使用新 `experiment="swift-owner-composition"`、schema v1；每份原始记录包括真实源码／只读依赖／binary hash、编译 attempts、固定 work、原始结果、errors 与未完成义务。旧源码和 binary 不重链接。

事件的身份至少包含 `owner_id / epoch / generation / scope_id / obligation_id`；waiter 和 lease 各有独立 ID。显式记录登记、实际访问开始／结束、取消请求与所标记对象、等待返回、关闭准入、child drain 开始／结束、逐义务结果、permit 归还和 B 准入。`wait.call_entered` 不叫 runtime dependency installed。不能只用最终 active=0 代替逐身份核对。

归还不变量为：本 owner 的相关实际访问全部确认结束，必要读取全部完成，所有登记读租约及读访问已结束，依赖的 child 义务已收束，准入关闭，且 epoch 匹配。对每个 scope，登记集合必须等于“成功／失败且安全结束集合”与“明确 unresolved 集合”的不交并；未声明完成的义务不能消失。出现 unresolved 时，不报告该复合范围 fully drained。

独立检查器用标准库重放身份与状态，并独立计算小数组递推、每次真实扫描 checksum 和访问次数；不 import 被测 ledger／oracle。编译测试另保存正例和负例来源、完整诊断，防止把 syntax/import 失败误认作能力约束。原始数值错误不能被“预期误用”外层标签洗成原请求成功。

建议依次交付：

1. C1 的 retain-wait、权限拆分与普通值／句柄逃逸，复现已有 Core 语义并增加 owner 边界。先不做 prompt-detach。
2. C2 的真实只读租约与 R1–R5，配合 E1/E2 的动态撤销；共享已复制结果不能替代这一批。
3. C3 的同 owner 拒绝、独立 owner 有界嵌套及错误／义务保留已有首批 CPU 证据；真实 read-cleanup 异常的新增 A0–A4 已通过独立验收。child 持父 live lease 已由 B0–B4 验收；C0借用读取抛错组合已独立验收，继续实际unknown完成边界；prompt-detach已另做八case真实竞态/故障执行并独立验收，不藏在共享API中。
4. C4 的独立 Swift 编译正负例与误报报告。任何非逃逸／effects 候选先给真实支持证据，再判断是否值得进入编译器改造。
5. 正常手工／封装开销对照另开同 binary、同数据、同调度策略的预注册运行；报全部记录／控制成本。设备迁移待 CPU 协议验收后申请小规模 Metal 时隙，原真实完成与 quarantine 条件不放宽。

当前计划不缩掉 P4 的完整门槛：接口成本、一般嵌套、共享 live 存储、等待队列／多资源顺序、真实设备故障和候选类型表达仍需分别交付。只有真实证据支持的构件才讨论组合成更统一的语言机制；这份计划不预先拼成新的通用运行时。
