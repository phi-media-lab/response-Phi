# P4 C4b：动态读租约与静态借用寿命的分开验证

2026-10-29。状态：[本机支持探针](../results/batch5/lifetime-support-REPORT.md) 后，冻结 B1/B2 attempt-2 的 18 个客户端已全部编译收集：8 个真正 CPU 运行、10 个拒绝；独立全元素、事件、来源及精确诊断复核已完成，31 项检查器测试通过。**证据完整性通过，原必要控制仍失败**：推断数组实际为 Any 转换诊断而非预设 Escapable，因此原退出 1、`required_controls_passed=false`、`validation_passed=null` 不改；后续显式数组单探针的 Escapable 拒绝另列。正式结果见 [C4b REPORT](../results/batch5/lifetime-fixtures-REPORT.md)。六个支持 attempt 的默认模式失败、独占访问拒绝及显式子作用域正例全部保留；候选为 `@_lifetime` 加 experimental `Lifetimes`，不称默认能力。接续 [组合计划](P4_COMPOSITION_PLAN.md) 的 `escaped_dynamic_read_lease` 与 `read_lease_return_from_body` 门槛。C4a 的结果包装不可复制性不替代本实验。

## 情境与要区分的保证

A 写好 128 个 UInt32 后，两个消费者读取同一代数据；随后 B 要覆盖这份存储。现有 C2 `ReadLease` 可复制并可被保存，其正确性依赖逐次准入、epoch 撤销与 owner 保留实际扫描任务。本轮先把这个事实移到独立库/client 边界，再研究本机 Swift 的非逃逸借用能排除哪些路径。

动态租约与静态借用分别报告，不能把两者命名为同一种保证。前者允许异步消费者持有句柄，由 registry 管理实际访问；后者先使用同步、私有 Array 的 lifetime-dependent Span。不得把从共享 buffer 复制出的数组视图说成原 buffer 的 live 租约，也不把 Span 的局部寿命当作 GPU 完成证明。

## 本机支持的初步依据

SDK 的 `Swift.swiftmodule/arm64e-apple-macos.swiftinterface` 声明 `Span<Element>: ~Escapable, Copyable`，并给 Array.span 的 borrowing getter 标注 `@lifetime(borrow self)`。这是接口可见性证据；实际可编译语法、跨模块与 async 使用仍由后续 fixture 判定。应记录完整 SDK 路径、接口哈希、Swift 版本和所有 compiler flags。

主要语言材料：[SE-0446](https://github.com/swiftlang/swift-evolution/blob/main/proposals/0446-non-escapable.md)、[Apple WWDC25 Span 介绍](https://developer.apple.com/videos/play/wwdc2025/312/)。这些材料把不可复制与非逃逸区分，并允许在寿命约束满足时传递非逃逸值；本轮不凭文档预设所有 async client 的结果。公开提案与本机工具链的支持范围须分别记载。

## B1：现有动态 lease 的公开边界

仅从已冻结 C2 source 复制依赖，旧源码不改。可在同一新模块内包装 internal API，但 public wrapper 不泄漏 pointer、原 owner、identity 构造器或可写视图。

- 合法消费者实际扫描 A，返回复制摘要；B 新 epoch 完整写/读后摘要仍有效。
- 允许消费者保存动态 lease；消费者 body 及其已登记扫描真实结束后，旧 lease 的新 scan 必须动态拒绝，且没有新增物理读取。B 正常完成。
- scope 关闭而 scan 尚未结束时，原已准入扫描继续完成；新 scan 拒绝，B 仍 busy。沿用 C2 的明确 gate 顺序，不以等待时间作性能结论。
- 独立 client 不能构造 lease identity 或抽取存储。失败需是准确访问控制诊断，同模块正常调用不算独立 client 控制。

每次实际扫描保留全数组、checksum、visits 与 owner/epoch/lease/access 身份；不能借 C4a 的摘要记录宣称全元素证据。

## B2：私有值存储的静态借用候选

使用一个自有存储的 `~Copyable` value owner，公开借用只读 Span，并允许在借用结束后同步覆盖。数值仍为已知 128 元素递推；public API 不接受可别名的任意 backend，不返回裸 pointer。这里的 owner 不替代 C2 的异步 registry。

存储由 owner 自己生成，client 不能传入或抽出 Array 副本。即使如此，也不单凭不可复制 owner 或数值结果声称物理地址不变；Array 的存储身份与 COW 必须另有证据。先验证有效 getter 和 forward 正例，再分类 local-return 负例。除 detached task 外保留同签名非并发 escaping closure 控制，区分逃逸、泛型 Escapable 与 Sendable 诊断，不预设拒绝原因。

逐个独立编译并分类以下 client，不把任意编译失败算作预期拒绝：

| client | 要核实的事实 |
| --- | --- |
| local read / two local copies | 真正读取完整 Span；合法只读复制不会因不可复制 owner 被误拒 |
| copied summary then overwrite | 普通结果可保存；借用结束后 B 可覆盖并产生正确的新结果 |
| forward borrowed view | 在显式、真实支持的寿命标注下将借用传给另一函数；返回值依赖来源 |
| return view of local owner | 不能把局部存储的 view 返回到其来源寿命之外；记录具体 lifetime 诊断 |
| overwrite before final view read | 观察编译器是否拒绝仍被使用的借用与写入重叠；若通过，核查是否产生值复制，不能直接称原地安全 |
| store in escapable container | 区分容器 Escapable 限制与数据 Sendable、访问权限等其他诊断 |
| capture in detached task | 观察逃逸捕获的具体拒绝原因，保留有效类型/导入的正例 |
| local view across an awaited call | 实际检验跨悬挂点的合法边界；不能预设非逃逸值都不可用于 async |
| return view from callback | 分开记录泛型结果默认 Escapable 拒绝与真正来源 lifetime 错误 |

若 `@lifetime` 或自定义候选在本机需要额外 feature flag，先保存默认模式失败，再用明确不同的 attempt 测试；不覆盖失败或把实验 flag 路径称默认能力。不得使用 unsafe lifetime override 绕过待验证的约束。

## 来源与验收

准备和执行分开。执行时直接启动冻结 driver；外层记录预先绑定该 driver 哈希。所有 Swift 编译输入来自不可变 attempt，库模块与 client 分开编译。每次编译后立即记录 object、swiftmodule 与最终 binary 的哈希，再链接/执行；不以审阅时的 hash 反推历史输入。

正例执行保存实际数值、完整事件和退出码。负例保存全部 error 诊断并逐条分类，拒绝未知附加错误；同时保留同模块/类型的正常控制。独立审阅不导入被测 owner 或 oracle，重算数值及各访问身份的生命周期。编译或设备时隙仍由 root 调度，不能和 P1 GPU 测量重叠。

本轮完成也不等于完整 P4：B2 没有设备访问、异步 live-memory 借用或动态 revoke 的静态证明；B1 保留运行时账本。一般嵌套、prompt-detach、共享 Metal、接口成本与真实设备故障仍按原组合计划独立推进。

## 实测后的范围说明

B1 独立复核支持同一私有 CPU allocation 上的具体 reader/scan 登记与 drain 路径：动态 lease 可保存，但关闭或换代后新访问被拒绝；已登记 scan 在 body 返回、准入关闭后仍完成全部遍数，随后才归还资源。外部 wrapper 的显式 Gate 不等于一般 child registry。

B2 的五个运行正例通过完整数值复算；跨 `Task.yield()` 仅观察到 await 表达式前后，不证明实际暂停。Array 值/COW 不证明物理原地更新。detached 与非并发 escaping capture 都是寿命逃逸诊断，不单独归因为 Sendable；泛型回调的隐式 Escapable 与具体回调的 closure-capture 支持限制分别保留。原推断容器控制失败与补充显式容器拒绝不能合并为“原矩阵全通过”。

[独立 review](../experiments/swift-lifetime-fixtures/verification/independent-review-v1/REVIEW.md) 保存准确证据边界及复验入口；原 [RESULTS_DRAFT](../experiments/swift-lifetime-fixtures/RESULTS_DRAFT.md) 继续作为独立验收前历史，不覆盖。
