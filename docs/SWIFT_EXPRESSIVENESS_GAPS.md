# Swift 表达缺口：C4a/C4b 给出的四条核心命题

2026-11-03。本文只整理已经保存在 `legacy/` 下的真实编译与运行证据，不新增编译、不运行 GPU。工具链固定为 Apple Swift 6.3.3、swift-driver 1.148.6、target `arm64-apple-macosx26.0`。每条引用的文件及其 SHA256 见同目录 `SWIFT_EXPRESSIVENESS_GAPS.evidence.json`。

## 0. 为什么这四条是"Swift 缺什么"的核心命题

研究方向文档把改造层次的选择规则写得很明确：现有 Swift 与库接口足以局部表达并维持契约时先保留实现；只有当调用者反复需要维护容易遗漏的状态关系，才考察类型、effects 或编译器诊断能否排除非法组合（`/Users/fbsh/swift/RESEARCH_DIRECTION.md` 第 147 至 151 行）。因此"语言层是否必要"的论证必须由一组事实构成：某个必要的安全关系，在本机 Swift 里既不能由类型拒绝，也不能由库封装拒绝，只能靠运行时账本兜底。

P4 的 C4a 与 C4b 恰好产出了这组事实。C4a 用独立 module/client 编译了 14 个样例，8 个编译并运行、6 个被明确语义错误拒绝（`legacy/results/batch5/capability-fixtures-REPORT.md`）。C4b 编译了 18 个客户端，8 个运行、10 个拒绝，并保留了一个未命中预设诊断的必要控制失败（`legacy/results/batch5/lifetime-fixtures-REPORT.md`）。这些样例每一个都对应一条 Swift 当前接受或拒绝的具体程序，诊断原文被完整保存。

它们比 P8 的大部分机制更能支撑语言方向，原因有三。第一，P8 的四种职责（Shared、Control、Lease、scope 持有的 Obligation）正是对这四条缺口的逐条替代，而缺口本身才是替代必要性的证据；没有这些反例，P8 只是一套未经比较的协议。第二，这四条缺口的证据形式是编译器诊断和事件轨迹，可以在任何有同版本工具链的机器上复核，不依赖本项目自造的检查器。第三，它们直接决定扩展应落在哪一层：缺口 1、2 是类型系统缺少线性与唯一性，缺口 3、4 是非逃逸值跨 async 边界的寿命支持尚未稳定，各自对应不同的最小扩展。

下面四节的结构固定为：命题、最小样例、Swift 实际接受或拒绝了什么、P8 用什么替代、最小语言扩展需要什么。

## 1. 唯一关闭权限无法强制

**命题。** Swift 不能在类型上表达"恰好一个持有者拥有关闭或归还某份资源的权限"；现有手段只有模块可见性加运行时 epoch，封装边界可以阻止伪造，但不能证明唯一。

**最小样例。** 反例来自 `legacy/experiments/swift-scope-alias`：两个 `ScopedSession` 持有同一个 `ResourceBackend`，各自的局部账本都认为自己拥有 generation 1，各自 drain 完毕，但 A 在必要扫描时读到了 B 覆盖后的值（`legacy/experiments/swift-scope-alias/README.md`）。保存的结果里，`serial-control` 试验 `all_requests_numerically_passed=true`，`interleaved-alias` 试验 `all_requests_numerically_passed=false` 且 `expected_counterexample_observed=true`（`legacy/experiments/swift-scope-alias/verification/counterexample/stdout.json`）。两个 scope 都正确，资源仍然不安全，问题出在没有任何类型把"关闭权限"绑定到唯一的资源身份。

C4a 用三个编译负例探了封装能挡住什么：

```swift
// legacy/experiments/swift-capability-fixtures/attempts/attempt-1/clients/consumer_cannot_cancel_producer.swift:4
func invalid(_ result: SharedResult) { result.cancelWork() }
// legacy/experiments/swift-capability-fixtures/attempts/attempt-1/clients/cannot_construct_result.swift:4
func invalid(_ completion: Completion) -> SharedResult { SharedResult(completion) }
```

**Swift 实际做了什么。** 三条诊断原文分别是 `value of type 'SharedResult' has no member 'cancelWork'`、`'SharedResult' initializer is inaccessible due to 'fileprivate' protection level`、`'owner' is inaccessible due to 'private' protection level`（`legacy/experiments/swift-capability-fixtures/attempts/attempt-1/fixtures/consumer_cannot_cancel_producer/compile/stderr`、`.../cannot_construct_result/compile/stderr`、`.../cannot_extract_owner/compile/stderr`）。这些都是访问控制诊断，不是所有权诊断。C4a 报告自己写明：结果接口不公开取消权，"不代表持有其他控制接口的调用者也无取消权"；而 `AffineResult` 的公开初始化器接受可复制的 `SharedResult`（`legacy/experiments/swift-capability-fixtures/Module.swift` 第 6 至 8 行），"多个包装仍可关联同一完成句柄"，因此不可复制包装"没有建立唯一底层资源权限"（`legacy/docs/P4_COMPOSITION_PLAN.md` 第 115 行）。

对别名反例的修复同样是动态的。`swift-resource-owner` 让 owner 自己创建并私藏 backend，用"不可伪造的公开构造边界"阻断注入，但 README 明确"epoch 和状态都是动态检查；没有声称静态线性、不可丢弃或必须 await"（`legacy/experiments/swift-resource-owner/README.md`；归档报告 `legacy/results/batch3/resource-owner/REPORT.md`）。修复设计也写明 lease 是"可动态撤销的许可，不先声称有静态线性保证"（`legacy/experiments/swift-scope-alias/REPAIR_DESIGN.md`）。

**P8 的替代。** 提案把 `Control<p>` 定为"可复制，但只能显式授予"，把 Obligation 定为"不是普通源语言值；不能由用户复制/丢弃/消费"（`legacy/docs/P8_LANGUAGE_SUBSET_PROPOSAL.md` 第 20 至 22 行）。运行时则干脆只允许一个 Owner：`allocate` 在 `owners` 非空时抛 `BackendOwnerBusy`，且 `size == 1`（`legacy/experiments/source-response-parent-host-close-v1/runtime/Sources/RuntimeCore/Ledger.swift` 第 1224 至 1225 行）。唯一性是账本里的一个计数，不是类型。

**最小扩展。** 需要两件 Swift 现在没有的东西：一是线性（必须恰好消费一次）的 token，`~Copyable` 只给"至多一次"；二是把 token 的身份与资源身份绑定的不可伪造构造，使同一 backend 不能被两个 owner 各自封装。前者是类型系统扩展，后者在库层可以用私有构造达到"不可伪造"，但"不可重复"仍要靠运行时。本条的编译负例最薄弱：仓库里没有任何一个从公开 client 尝试为同一 backend 建两个 owner 的 fixture，别名反例是在内部 Core 上直接注入 backend 构造的。

## 2. 不可复制不等于必须等待

**命题。** `~Copyable` 只约束使用次数上限；一个合法程序可以丢弃不可复制的完成句柄而从不 await，完成义务因此只能由 scope 在运行时保留。

**最小样例。**

```swift
// legacy/experiments/swift-capability-fixtures/attempts/attempt-1/clients/affine_drop_without_await.swift:7-11
let answer = try await owner.withSession { s in
    let handle = AffineResult(try s.submit(fixtureWork("A")).result)
    _ = consume handle
    return 17
}
```

**Swift 实际做了什么。** 这个客户端编译并运行成功，`facts.body_value = "17"`。保存的 20 个事件显示 body 返回后 owner 进入 `owner.draining`，随后 `producer.started`、`backend.physical_write_ended`、两遍必要读取 `backend.read_started/read_ended`、`resource.released`、`producer.done`、`session.closed`、`drain.ended`、`owner.permit_returned` 依次发生（`legacy/experiments/swift-capability-fixtures/attempts/attempt-1/fixtures/affine_drop_without_await/execution/stdout`）。也就是说，用户代码丢掉句柄之后，全部实际工作由 scope 完成。作为对照，同一类型的 `copy handle` 得到 `'copy' cannot be applied to noncopyable types`，重复消费得到 `'handle' consumed more than once`（`.../fixtures/affine_copy/compile/stderr`、`.../fixtures/affine_double_consume/compile/stderr`）。更早的四个独立 token fixture 给出同样的四分格：`consume_once` 通过、`consume_twice` 拒绝、`copy_token` 拒绝、`drop_is_legal` 通过，报告的 `limitation` 字段直接写着 "legal drop disproves must-await"（`legacy/experiments/swift-obligation-scope/verification/compile-fixtures/report.json`）。

运行层面的对照来自 C1：取消等待者后 owner 仍保留原 producer Task，完成两遍必要读取并实际 join 后才让 B 复用存储；两个等待者都离开时 `registry_entries` 仍为 2，报告指出这两个计数"都不能充当资源回收证明"（`legacy/results/batch5/cancellable-wait-REPORT.md`）。C1/C2 的验收也写明"丢弃所有结果句柄及取消外层 scope，不会移除 owner 已登记的 producer"（`legacy/results/batch5/owner-composition-REPORT.md`）。

**P8 的替代。** Obligation 由 owner 持有而不是用户值；`RUN/SCOPE` 在隐式 finally 中收束（`legacy/docs/P8_LANGUAGE_SUBSET_PROPOSAL.md` 第 22、113 行）。运行时 `commitScopeExit` 要求 `Set(s.local) == s.joined` 才允许 scope 结束（`legacy/experiments/source-response-parent-host-close-v1/runtime/Sources/RuntimeCore/Ledger.swift` 中 `commitScopeExit`）。

**最小扩展。** 一个"禁止隐式析构"的标记，即线性类型：被标记的 `~Copyable` 值离开作用域而未被指定操作消费时报编译错误。Swift 当前为不可复制值提供隐式 `deinit`，这正是 `drop_is_legal` 通过的原因。有了该标记，`affine_drop_without_await` 会在编译期被拒绝，而 `affine_value_once` 保持合法。这是四条里最小的扩展，并且不涉及异步寿命。

## 3. session 可逃逸 scope

**命题。** 由 `withSession` 闭包提供的 session 或 lease 值可以被返回或存到闭包外，Swift 不能把"只在此闭包内有效"做成类型，只能在旧 epoch 被使用时运行时拒绝。

**最小样例。**

```swift
// legacy/experiments/swift-capability-fixtures/attempts/attempt-1/clients/escaped_dynamic_session.swift:7-12
let escaped = try await owner.withSession { s in
    _ = try await s.submit(fixtureWork("A")).result.value(); return s
}
var rejection = "missing"
do { _ = try escaped.submit(fixtureWork("B")) }
catch PermitError.expiredPermit { rejection = "expiredPermit" }
```

**Swift 实际做了什么。** 编译通过，运行后 `facts.rejection = "expiredPermit"`，事件流中 `owner.permit_returned` 之后出现 `owner.expired_rejected`，随后 B 在新 session 正常完成，checksum 1918236676858216040、visits 256、mismatches 0（`legacy/experiments/swift-capability-fixtures/attempts/attempt-1/fixtures/escaped_dynamic_session/execution/stdout`）。C4b 把同一事实搬到真正的读租约上：`dynamic_escaped_lease` 把 lease 存进 `FixtureBox`，scope 结束后再 scan 得到 `closing`，B 换代后得到 `expired`（`legacy/experiments/swift-lifetime-fixtures/clients/dynamic_escaped_lease.swift` 第 4 至 14 行；预期写在 `legacy/experiments/swift-lifetime-fixtures/fixtures.json` 的 `rejections: ["closing","expired"]`）。facade 里 `DynamicLease` 是一个 `Sendable` 结构体，初始化器 `fileprivate`，所以 client 不能构造，但可以任意保存（`legacy/experiments/swift-lifetime-fixtures/DynamicFacade.swift` 第 16 至 19 行）。C4b 报告的结论是"值可逃逸与访问权仍有效是两件事；权限由动态 phase/epoch 控制"（`legacy/results/batch5/lifetime-fixtures-REPORT.md`）。

**P8 的替代。** `Stable` 谓词：普通复制数据、Owner 的封装申请句柄、Control、payload 同样 Stable 的 Shared 可以逃逸；live lease、正在执行的 Read/Write、包含它们的积或闭包不可以。提案明确说这是比动态 Swift lease 更严格的静态前端，"现有合法'把旧 lease 保存到 scope 外再观察 expired'程序会被拒绝"（`legacy/docs/P8_LANGUAGE_SUBSET_PROPOSAL.md` 第 113、115 行）。

**最小扩展。** 让 session 和 lease 成为 `~Escapable` 并把寿命依赖绑定到 `withSession` 的借用参数。这在语义上就是 SE-0446 一类的非逃逸类型，但要用在这里有两个前提：非逃逸值必须能作为 `async` 闭包的参数在挂起点两侧存活，并且不能被 `T: Sendable` 这样的泛型约束隐式要求为 Escapable。facade 的 `withSession<T: Sendable>` 签名（`legacy/experiments/swift-lifetime-fixtures/DynamicFacade.swift` 第 45 至 46 行）正是这类约束。两个前提都落在缺口 4。

## 4. live 租约跨 async 边界只有实验特性

**命题。** 让一个指向真实存储的视图由编译器保证寿命，在本机只能通过 `@_lifetime` 加 `-enable-experimental-feature Lifetimes`；即便启用，detached 捕获、callback 返回和泛型容器仍被拒绝，跨 await 的正例也没有证明真的发生了挂起。

**最小样例。**

```swift
// legacy/experiments/swift-lifetime-fixtures/LifetimeValues.swift:13-19
public struct ValueOwner: ~Copyable {
    private var storage: [UInt32]
    public var view: Span<UInt32> {
        @_lifetime(borrow self)
        borrowing get { storage.span }
    }
}
// legacy/experiments/swift-lifetime-fixtures/clients/value_detached_capture.swift:3-6
func probe(_ owner: borrowing ValueOwner) {
    let view = owner.view
    _ = Task.detached { _ = view.count }
}
```

**Swift 实际做了什么。** 默认模式下库本身就不能编译。v2 用 `@lifetime` 得到 `a method cannot return a ~Escapable result` 和 `'@lifetime' attribute is only valid when experimental feature LifetimeDependence is enabled`；v5 用 `@_lifetime` 不加 flag 得到 `'@_lifetime' attribute is only valid when experimental feature Lifetimes is enabled`（`legacy/results/batch5/lifetime-support-probe-v2/library/stderr`、`legacy/results/batch5/lifetime-support-probe-v5/library/stderr`）。v6 加上 `-enable-experimental-feature Lifetimes` 后库编译无诊断、正例运行退出 0、返回局部 owner 视图的负例得到 `lifetime-dependent value escapes its scope`（`legacy/results/batch5/lifetime-support-probe-v6/report.json`、`.../escape-compile/stderr`、源码 `.../source/Views.swift`；汇总见 `legacy/results/batch5/lifetime-support-REPORT.md`）。

启用后的边界由 C4b 的 attempt-2 逐条记录（`legacy/experiments/swift-lifetime-fixtures/attempts/attempt-2/report.json`，分类见 `legacy/experiments/swift-lifetime-fixtures/verification/attempt-2-diagnostics.json`）：

| 客户端 | 实际诊断或结果 |
| --- | --- |
| `value_detached_capture` | `lifetime-dependent variable 'view' escapes its scope`，note "it depends on the lifetime of argument 'owner'" |
| `value_nonconcurrent_escaping_capture` | 同一错误，没有 Task，证明拒绝来自寿命而非 Sendable |
| `value_across_await` | 编译并运行，数值正确，但 "runtime suspension not demonstrated" |
| `value_callback_generic_result` | `global function 'withLocal' requires that 'Span<UInt32>' conform to 'Escapable'` |
| `value_callback_local_dependence` | 两处寿命逃逸错误，附 "closure capture; this is not yet supported" note |

C4a 在默认模式下给出同类的借用捕获拒绝：`'handle' cannot be captured by an escaping closure since it is a borrowed parameter`（`legacy/experiments/swift-capability-fixtures/attempts/attempt-1/clients/borrow_capture_in_detached_task.swift` 第 4 至 5 行；诊断见 `.../fixtures/borrow_capture_in_detached_task/compile/stderr`）。C4b 报告对这组的定性是：B1 的运行时可以保留已登记访问并在换代前等待它们，B2 的编译器可以约束某些值视图的来源寿命，"尚没有把两者接成一个覆盖异步设备访问的保证"（`legacy/results/batch5/lifetime-fixtures-REPORT.md`）。候选计划也只把 SDK 接口里 `Span: ~Escapable` 的声明当作"接口可见性证据"（`legacy/docs/P4_LIFETIME_CANDIDATE_PLAN.md`）。

**P8 的替代。** 普通 `fork` 不得隐式捕获 Lease/Read/Write；`FORK-READ` 必须显式声明有效 lease，child 得到新的 α-branded Read，其他 live 能力仍被拒绝（`legacy/docs/P8_LANGUAGE_SUBSET_PROPOSAL.md` 第 109 行）。运行时对应 B1 的逐访问登记：scan 在同一锁内核对 owner/epoch/generation/lease 身份后才允许开始。

**最小扩展。** 把寿命依赖推广到结构化并发：允许 `~Escapable` 视图被 `async let` 或 task group 的子任务捕获，条件是父任务对 owner 的借用覆盖子任务的整个生命周期；继续拒绝 `Task.detached`。另外需要非逃逸的泛型容器或 `T: ~Escapable` 的回调返回。这是四条里最大的扩展，并且依赖上游 `Lifetimes` 特性的稳定，不是本项目能单独完成的。

## 5. C4b 的失败说明了什么

C4b 预设推断数组 `let views = [owner.view]` 会被 `Escapable` 诊断拒绝，实际得到的是 `cannot convert value of type 'Span<UInt32>' to expected element type 'Any'`（`legacy/experiments/swift-lifetime-fixtures/clients/value_store_in_array.swift` 第 3 行）。原 attempt-2 因此 `required_controls_passed=false`、`validation_passed=null`、外层退出 1，全部保留。随后单独编译的显式容器 `let views: [Span<UInt32>] = [owner.view]` 才给出 `type 'Span<UInt32>' does not conform to protocol 'Escapable'`（`legacy/experiments/swift-lifetime-fixtures/probes/array-explicit-1/source/ExplicitArray.swift` 第 4 行；诊断 `.../probes/array-explicit-1/stderr`；命令与返回码 `.../probes/array-explicit-1/compile.json`）。

这件事说明三点。第一，非逃逸值的误用诊断依赖类型推断路径：同一误用在有无类型标注时报出两种不同错误，所以"Swift 拒绝了什么"必须逐条读诊断而不能数编译失败，这正是 `legacy/experiments/swift-lifetime-fixtures/README.md` 坚持的分类方式。第二，方法上，预设诊断不匹配时没有改写预期，而是保留失败并另立探针，这个纪律应当延续到消融实验。第三，对语言论证而言，容器限制是真实的：非逃逸值不能进入 Escapable 泛型容器，这堵死了多 reader、多资源情境自然需要的"租约列表"写法。P8 为此选择了动态 registry，也就是再次退回运行时；一个静态方案必须提供非逃逸容器，否则缺口 4 无法在多资源情境下闭合。

## 6. 这组论证尚未覆盖什么，以及最便宜的补证

尚未覆盖：

- 缺口 1 没有编译负例。没有任何 fixture 从公开 client 尝试为同一 backend 建立第二个 owner；别名反例是在内部 Core 上注入 backend 构造的。唯一性目前只有"私有构造加运行时计数"的证据。
- 所有编译负例都是对本项目自己的 facade 模块（Capabilities、DynamicLeases、LifetimeValues）用 `swiftc -emit-module` 直接编译，没有在一个独立 Swift package 的 library product 上由外部 public client 复现。C1 报告自己也写"内部 factory 还不是独立编译的公有能力边界"（`legacy/results/batch5/cancellable-wait-REPORT.md`）。
- 没有与 Swift 演进提案逐条对照。候选计划只引用了 SE-0446 和 WWDC25 作为接口可见性依据；缺口 1、2 对应的线性类型，缺口 3、4 对应的非逃逸类型跨 async 和 `Lifetimes` 特性的稳定状态，都没有一张"提案状态对缺口"的表。
- `value_across_await` 没有证明实际挂起；C4a 的 driver 没有从冻结快照重新执行，library object/module 的原运行 hash 未保存（两份报告各自写明）。
- 动态替代的成本没有测量。epoch 检查、逐访问登记和 Stable 检查相对手写协议的开销没有数字，P4 各报告的接口开销项都标为未完成。
- 全部证据只来自 Swift 6.3.3 和 macOS 26 一台机器。

最便宜的补证实验，按成本从低到高：

1. 提案对照表。纯文档，把四条缺口各自映射到 SE-0390、SE-0427、SE-0446、SE-0447 与 `Lifetimes` 实验特性的状态，标明哪些是已接受语义、哪些是实验特性、哪些根本没有提案。无需运行。
2. 缺口 2 的线性探针。一个 `~Copyable` 结构体加 `deinit` 内的陷阱，验证本机只能把"未消费"变成运行时崩溃而不能变成编译错误；同一 swiftc 会话完成。
3. 缺口 1 的公开 client 双 owner 探针。从独立 client 尝试用公开 API 为同一资源建两个 owner，记录是构造不可能还是运行时 busy；再加一个 package 级 library product 的复现，一并回答"独立 package 边界"的缺口。
4. 缺口 3、4 的 `~Escapable` session 探针。在 `-enable-experimental-feature Lifetimes` 下把 `withSession` 的 session 类型改为 `~Escapable`，返回类型放宽到 `T: ~Escapable`，看 `async` 闭包加 `Sendable` 约束是否接受；只需编译，不需要运行。
5. 真实挂起对照。用从另一线程恢复的 continuation 替换 `Task.yield()`，保留恢复前后的线程身份，证明视图跨过了真实挂起点。这是 CPU 实验，不涉及 GPU。
