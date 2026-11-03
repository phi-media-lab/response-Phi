# P8：带完成义务的最小异步语言子集提案

2026-10-30。**设计提案；未实现类型检查器或解释器，未证明定理，未运行测试。** 本文只新增候选语言规格，不改变已冻结的有限模型、Swift 实验或其结论。待审的是语言的取舍，不是又一份通过状态表。

依据为 [P8 首个模型计划](P8_PROTOCOL_MODEL_PLAN.md)、[独立报告](../results/batch5/protocol-model-REPORT.md)、[P4 组合门槛](P4_COMPOSITION_PLAN.md)以及新 [可取消等待设计](../experiments/swift-cancellable-wait/DESIGN.md)。原两代模型的 836 个状态与 2704 条边不是本语言的类型健全性证明。新的可取消等待C1已实际编译并运行八case、[完整独立事件/来源验收](../results/batch5/cancellable-wait-REPORT.md)通过；以下 `await?` 仍是候选语言语义，不能由实现样例倒推类型健全性。

## 1. 选定的问题与最小范围

一个 producer 写私有存储并完成必要读取；多个消费者取得复制结果，另一些注册任务继续读取同代存储。消费者可结束自己的等待，创建者可请求取消 producer；scope 最终关闭新准入、收束已登记工作，再允许下一代写入。我们希望程序能表达这些差别，也希望类型和语义能指出它没有表达什么。

首个子集包括普通不可变值、函数/顺序组合、条件、输入决定的有界循环、注册异步任务、嵌套 scope、一个 owner 每次一个 epoch 的写后读协议、复制结果和可取消等待。每次运行可以有任意有限个 owner、epoch、任务和读操作，数量不固定为 A 两次/B 一次。第一版一次借用注册只涉及一个资源 owner；不同 owner 可嵌套，原 owner 忙时立即拒绝。

它不包含任意原始指针、未登记 detached task、任意 continuation/FFI 回调、写入中的共享读取、多 writer、多资源原子获取、一般递归或恢复未知设备访问。这些省略是明确的表达能力限制。后端 kernel 是有类型的外部原语，不在这里设计另一套 shader 语言；CPU/Metal 可以分别实现其完成契约。P5 布局、P6 fusion、P7 工具意图也不成为这个核心的隐式运行机制。

## 2. 四种职责不对应四个必须线性的对象

| 类型/语义对象 | 复制与使用 | 能做什么 | 不能据此做什么 |
| --- | --- | --- | --- |
| `Shared<p, T>` | 可复制、可丢弃、可重复等待；`T` 必须无 live 能力 | 取得 p 的同一不可变 outcome；完成后可跨 epoch 使用 | 取消 p、获取存储指针、证明 scope 已 join |
| `Control<p>` | **可复制，但只能显式授予** | 设置 p 的单调取消请求位 | 删除义务、确认访问结束、取消另一个 ID 的任务 |
| `Lease<s, ρ, g, ℓ>` | 在有效区域内可复制；可重复申请读操作 | 向当前 owner 注册新的只读工作 | 直接读取内存、关闭 lease、在 scope 关闭后新增工作 |
| owner 持有的 `Obligation<o>` | 不是普通源语言值；不能由用户复制/丢弃/消费 | 保留实际任务、访问和完成证据，支持 scope drain | 用引用计数为零、waiter 返回或取消请求代替完成 |

此外，已经准入的读工作取得 `Read<α, ρ, g>`，它只在该工作内有效；可反复执行真实只读原语，不需要为每个元素或每一遍扫描重新申请 lease。独占写权限 `Write<ρ, g>` 才使用仿射规则；关闭/归还 authority 由 scope 内部唯一持有，不放进复制的结果、控制或读 facet。

把 `Control` 强制仿射并非安全所必需：多个已获授权者设置同一个幂等请求位，不会新增写权限或减少义务。普通消费者没有控制权，与控制权是否可共享是两个问题。同理，可复制读入口不是可复制关闭权；真正延长存储寿命的是已经登记的访问，而非还剩几个 lease 对象引用。

## 3. 表面语法与核心语法

以下使用显式 `let` 的命令语法；省略普通整数、积/和类型、不可变向量和纯函数应用的常规规则。

```text
T ::= Unit | Bool | Nat | Word | Snapshot<n,Word>
    | T × T | T + T | Owner<ρ>
    | Shared<p,T> | Control<p>
    | Lease<s,ρ,g,ℓ> | Read<α,ρ,g> | Write<ρ,g>

c ::= return v | let x = e in c | if e then c else c
    | for i < e do c ; c | throw e | rethrow f
    | new_owner n as o in c
    | scope s { c }
    | run o as s { c }
    | let (h,k) = produce s K(args) cleanup (r => cR) in c
    | let (h,k) = fork s { cChild } in c
    | let l = lease s h in c
    | let (h,k) = fork s read l as a { cRead } in c
    | let x = load a e in c
    | let x = await h in c
    | let x = await? h in c
    | request_cancel k ; c | drop v ; c
```

`run o` 是有私有 owner 的 scope：原子申请新 epoch，忙时返回明确的准入前故障，不排队等待自身。每个 epoch 首版只接纳一个 `produce`，其后为读阶段；下一次写必须进入下一 epoch。普通 `scope` 可注册不持有存储的消费者和 child。

`K` 是后端签名中的写程序。它可以包含输入决定的有限个设备命令，必须逐命令给出实际提交/完成/观察身份；不能把一个 descriptor 的存在当其全部命令已完成。`cR` 是预登记的必要只读清理，可做有界读取和等待，返回复制值。无论结果句柄是否有人保留，这项清理都不成为可省略的业务代码。只有写入完成证据满足时才授予 `r`；否则必须明确决定未开始或未收束，不能读取未初始化存储。

`lease s h` 要求 h 确实属于 s 的 producer；首版等待其成功发布且 owner 已进入只读阶段，再创建 lease。故障后的数据抢救及 producer 清理与额外 reader 重叠不是这一版接口。此限制换取简单的写后读规则，需要列为表达能力代价。

`fork s read l as a { cRead }` 是**一个已注册读程序**，不是让 child 在未来任意取得新 lease。它原子提交 child、lease pin、访问 α 和实际任务身份，再放行任务；内部任意有限次 `load a` 属于该操作。它可以在父 scope 关闭之后才开始或继续这些读取。若改为关闭后执行一次新的 `fork ... read l`，必须拒绝，且没有新任务开始访问。

一次注册的读程序可在自身内部运行另一 owner 的 scope。因此能表示“Y 的 child 持有 X 的只读访问”，并保持 `origin invocation=Y` 与 `resource=(X,g,ℓ,α)` 不同。首版保守地将 X 的 pin 保留到该整个 child 终态；读早已结束、Y 的其他清理尚未完成时也不提前归还 X。这不证明最小必要等待。

## 4. 类型、区域与效果判断

判断形式为：

```text
Ψ ; Γ ; Δ ⊢s c : T ! ε ▷ Δ′
```

`Ψ` 保存 generative owner/epoch/scope/任务的身份和包含关系；源程序不能用任意字符串构造相同身份。`Γ` 保存可复制值，包括显式的 `Control` 和在区域内有效的 `Lease/Read`；`Δ` 保存仿射写权限等独占权限。**位于 Γ 并不自动表示可以逃逸区域。** 函数类型同时记录捕获的 live 区域；返回一个捕获 lease 的 closure 与直接返回 lease 一样接受逃逸检查。

效果行最少为：

```text
ε ⊆ { Issue(ρ), Read(ρ), Register(s), Need(p), WaitDetach(p),
      RequestCancel(p), Raise }
```

`Need(p)` 描述语义依赖，不承诺某个 Swift continuation 会向 p 传播 priority。`WaitDetach(p)` 区分可取消观察；它也不表示取消 p。效果行不包含“该工作已经完成”这个可由源程序声称的效果。

关键规则的候选形式如下；相同的 Γ 可用于顺序命令和只读分支，Δ 在并发捕获间不能复制。

```text
(CANCEL)
  Γ(k) = Control<p>
  ───────────────────────────────────────────────────
  Ψ;Γ;Δ ⊢ request_cancel k : Unit ! {RequestCancel(p)} ▷ Δ

(AWAIT)
  Γ(h) = Shared<p,T>       Stable(T)
  ───────────────────────────────────────────────────
  Ψ;Γ;Δ ⊢ await h : T ! {Need(p),Raise} ▷ Δ

(AWAIT-CANCEL)
  Γ(h) = Shared<p,T>       Stable(T)
  ───────────────────────────────────────────────────
  Ψ;Γ;Δ ⊢ await? h : Delivered<T> + WaitCancelled
                       ! {Need(p),WaitDetach(p),Raise} ▷ Δ

(LOAD)
  Γ(a) = Read<α,ρ,g>       α is this registered read job
  ───────────────────────────────────────────────────
  Ψ;Γ;Δ ⊢ load a i : Word ! {Read(ρ),Raise} ▷ Δ
```

`await` 遇到 producer 原故障时传播原身份；`await?` 的本地 `WaitCancelled` 不变成 producer 的故障。一次交付已赢得仲裁后，不能用后来的 caller cancellation 改写它。

`FORK` 检查 child 具有 `Stable(T)` 返回类型，并把其启动/完成交给 s。普通 `fork` 不得隐式捕获 `Lease/Read/Write`。`FORK-READ` 必须显式声明一个有效 lease；child 获得新的 α-branded `Read`，其捕获中的其他 live 能力仍被拒绝。已有 `Read<α>` 也不能被普通 fork 再传播给未登记异步任务。想表达进一步借用，需要一个新的已声明注册事务，而不是复制 closure 绕过登记。

`PRODUCE` 为 K 引入唯一 `Write<ρ,g>`，不给源语言 body 留下第二份写权限；同 epoch 不允许另一个 producer。K 的外部签名必须只写其声明资源，不把 pointer 或访问权包进返回值。预登记清理只能得到 `Read`，不能调用写原语。

`RUN/SCOPE` 要求 body 以及最终返回值的 `Stable` 条件，并在隐式 finally 中关闭/收束。`Stable` 是明确的结构性谓词：普通复制数据、`Owner` 的封装申请句柄、`Control`、payload 同样 Stable 的 `Shared` 可逃逸；live lease、正在执行的 Read/Write、包含这些能力的积/闭包不可以。结果 p 可以经存在量化隐藏身份后逃逸，不要求错误地按 owner 当前 epoch 撤销历史结果。

这是比当前动态 Swift lease 更严格的一种**候选静态前端**：现有合法“把旧 lease 保存到 scope 外再观察 expired”程序会被拒绝，而重复结果等待和多个合法只读消费者不会被拒绝。若保留 escaped dynamic lease 兼容模式，应另给所有访问必经 guard 的规则与精化证明，不能悄悄取消此处的 `Stable` 检查后继续宣称同一静态性质。

## 5. 运行状态与几个决定性的转移

无限状态小步机器使用动态新鲜 ID；这不是把两个操作换成更大的固定枚举。

```text
C = ⟨ store, owners, scopes, jobs, accesses, waits, obligations, faults ⟩

owner ρ = (epoch g, phase idle|writing|readable|closing|quarantined,
           private allocation, current scope, lease/pin IDs)
scope s = (open|closing|joined|unresolved, registered job IDs,
           body outcome, recorded fault IDs)
access α = (resource identity, origin invocation, registered|started|
            ended|aborted-before-start|unknown, actual task identity)
wait w = (p, caller, created|attached|skipped, undecided|delivered|detached)
```

任务 outcome 发布、实际 task 终态、访问结束观察是三个不同字段。`obligations` 保留每个已发行义务的原身份与证据；未完成/unknown 的任务和资源根不能只存在于用户 h 的引用中。

| 规则 | 前置条件与实际变化 |
| --- | --- |
| `ACQUIRE` | owner idle；分配新 epoch/私有写阶段和 scope 关闭权。忙时拒绝，不创建 producer/access。 |
| `REGISTER` | s 开放；一个线性化点分配 job/义务、保存实际任务、提交完整身份集合，然后才开 start gate。先有记录但未提交的中间状态不得放行任务。 |
| `REGISTER-READ` | s 与 lease 新读准入均开放、owner/epoch 相符且只读 ready；同一事务再登记 pin 与 α。关闭竞争要么完整成功，要么零访问拒绝。 |
| `ISSUE/OBSERVE` | 后端提交逐命令建立义务；实际完成观察只更新相应 ID。完成通知不自动结束后续 CPU 读取，也不等于 producer Task 已 join。 |
| `PUBLISH` | producer 有合法复制 outcome；将它缓存并仲裁 waiter。即使已发布，owner 仍保留需要实际 join 的 producer。 |
| `WAIT-DETACH` | 当前 waiter 的取消赢得一次性仲裁；只移除/终结观察关系。producer、α、pin、已发行义务均不改变。 |
| `BODY-EXIT` | body 实际返回或抛错后进入同一关闭路径；先关新登记，再遍历全部已登记身份。仅将 caller 标记取消不触发此转移；若 body 观察取消并因此退出，才由实际退出进入关闭。一个失败不能跳过兄弟。 |
| `JOIN` | 观察实际 task 返回，合并该义务原故障与证据。已登记但从未进入访问的任务也需明确 abort-before-start 并实际终结；不开 gate 永等不叫 abort。 |
| `RELEASE` | 准入已关、所有相关实际访问已确认结束/确实未开始、必要清理和子义务均安全 join、pin 已收束、epoch 匹配；才能归还并允许下一代写。 |
| `UNKNOWN` | 完成证据未知，保存 unresolved 身份/故障及根；没有 release 或 fully-joined 规则，错误/timeout 不能补造完成。 |

两种“关闭”必须区分。scope 关闭的是**新 job/lease/access 登记**；已登记 producer 的剩余既定工作与已登记读程序继续履行。后端 producer 的 `close_submissions` 则关闭**新设备命令提交**。旧 P8 模型的 `close_A` 属于后者；不能在新语言里不加说明地用前者替换它。一个取消请求只要求相应 producer 的明确策略处理：首个 C1 候选保留全部既定工作；若以后允许在未提交边界停止，必须另外给实际已提交集合、剩余工作和关闭点。已经 commit 的批次不能被类型规则凭空撤回。

wait 注册/缓存发布/取消的执行规则需让每个 w 恰有一个终态、一次交付；取消早到保存待兑现状态。抽象规则只给线性化语义，实际实现还须证明 continuation 安装/选择/resume/真正返回的精化关系。`resume` 调用后的日志不必先于被唤醒 waiter 返回；语言规则不为方便检查器虚构此顺序。

## 6. 故障身份与 unresolved

每次原始 throw 分配不透明 `FaultId=(origin invocation, obligation, local ordinal)`，保存原因及涉及的实际资源身份。`rethrow` 和父 scope 收集只增加 `reported_via`，不重新命名 origin。两条同文案不同义务的故障仍是两条；同一故障经本地 await 和 scope join 两路报告仍是一个原身份、两个渠道。新 wrapping error 可以有新 ID，但须保留 cause，不可把它伪装成原故障。

`safe + faults` 与 `unresolved + faults` 是不同完成状态。读取后实际 throw 可以是前者；设备完成未知即使 task 自身已经返回也不是前者。任何统计 `active=0`、waiter=0 或结果失败都不能抹掉未知访问。首版允许观测 unresolved 诊断，但没有把它作为普通成功 scope 返回或恢复资源的规则。安全终结的独立 owner 可各自归还；依赖未知 child 访问的 owner 必须继续持有。

## 7. 真实程序形状与应拒绝的程序

结果共享的合法程序形状：

```text
run owner as s {
  let (r, producerControl) = produce s K(data) cleanup (a => checksum(a))
  let (c1, caller1Control) = fork s { await? r }
  let (c2, caller2Control) = fork s { await r }
  request_cancel caller1Control
  drop producerControl
  return r
}
```

c1 可先返回等待取消，c2 继续；body 立即返回也先触发 scope close/drain。scope 返回的 r 可在下一 epoch 后再次等待。这不要求 producerControl 仿射，也不以所有 waiter 离开作为释放依据。

成功 producer 后可重复注册两个 `fork s read l ...`。父 body 随后返回；这些任务若尚未开始，仍是关闭前已经准入的具体读工作。它们内部可以多遍读取并返回复制值。新申请第三个读工作若输给关闭竞争，则必须零访问拒绝。类型禁止 `return a`、在普通 fork 中捕获 a、通过 Shared 返回 live view；语义仍负责登记/关闭竞争与真正完成。

具体拒绝点需同时有合法控制：`request_cancel r` 类型错，但显式传入 Control 合法；复制 Write 类型错，但复制 Lease 并注册两个读任务合法；返回 Lease 类型错，但返回完成的 Shared 合法；关闭后新读动态拒绝，但关闭前登记的多遍读取继续合法。不能只搜集失败例来掩盖不必要的限制。

## 8. 候选证明，不把经验结果当定理

下列为拟写的归纳命题和它们真正需要的假设，尚未完成证明。

1. **权限/区域保持。** 在可信原语签名和新鲜身份规则下，良类型配置每步仍良类型；普通消费者不能获得 Control，live 能力不能经值/closure/Shared 逃逸。仿射写权限不重复。需要替换、弱化/收缩条件、并发上下文分离及配置类型引理，不能只展示几个 Swift 编译失败。
2. **义务守恒。** 对任意有限执行前缀，单调的 `Issued` 集合恰分为仍活跃、安全终结和 unresolved 三个不交部分；每个 safe 终结有对应真实证据。按转移归纳，尤其逐条检查 drop、wait detach、取消、故障合并和 abort-before-start 都没有删除身份。这沿用旧独立历史账本发现的要求，不能用一个局部终态断言代替历史。
3. **无跨代访问/提前复用。** 从登记原子性、写/读阶段、epoch 检查、访问根保留和 RELEASE 前提归纳出：新代 writer 开始前，旧代相关访问已安全结束。它是类型规则与动态语义共同的定理，前提包含私有封装和可信访问完成回报，不是仅靠 `~Copyable` 的定理。
4. **关闭与丢句柄不遗失已注册工作。** close 之后没有新的同 scope 登记；已经成功提交的完整身份集仍属于 drain，哪怕所有用户结果/控制句柄均被丢弃。已登记读程序内部继续 load 不属于关闭后新登记。
5. **等待取消的非干扰与唯一终态。** 对 w 的 detach 不改变 producer 的取消位或义务账本；最多一项交付/取消胜出，历史复制结果跨 epoch 有效。只证明语义，没有“取消一定在 x 毫秒内返回”的结论。
6. **故障保留。** 安全 drain 不丢任何已产生原 fault，传播不合并不同原身份；unresolved 不能被其他 owner 成功或二次诊断错误覆盖。需定义源程序 `throw/rethrow`、后端失败与 scope 聚合三者的模拟关系。

这是无限状态、以任意有限程序/执行前缀为量化对象的目标。有限 explorer 可用于寻找这些规则的反例，但穷举更多状态仍不替代保持性与账本不变量的归纳证明。Swift 实现还需要从实际 Task/锁/callback/指针封装到这些转移的精化证据；硬件访问完成的可信边界不由语言的自报字段证明。

## 9. 进展、响应成本与 PriML 动机的连接

安全语义可让 owner 永远保留资源。即使输入循环有界，用户等待、调度、设备或外部服务仍可能不返回。首版不宣称一般死锁自由；对响应性单独限制实际依赖图无环、原语终止、必要任务获服务、没有 unknown。还要检查这些条件是否能由更窄的语法/类型效应推出，不能先把“所有良类型程序无环”写成结论。

抽取的图至少区分：设备访问结束 → 主机观察 → 必要 CPU 读取 → 实际 task join → owner 归还 → B 准入/服务。复制结果 publish 可早于 join；一个 waiter detach 是另一条短路径，不删除前述路径。只有在逐节点已有服务成本上界 C(v) 和就绪后服务延迟上界 S(v) 时，才有：

```text
E(v) ≤ max(E(u) : u ∈ pred(v)) + S(v) + C(v)
```

可取消等待减少的是该 waiter 的语义前驱；何时安装 handler、获得锁、resume 及得到 CPU 服务仍要各自有界才能给等待返回上界。owner 转交仍受完整义务路径限制。保守 child pin 还可能增加依赖；不能称该协议自动最优。

效果中的 Need 身份为将来 priority/需求规则保留信息，但本提案不含未经证明的 priority inversion 定理。运行时 continuation 可能隐藏原 producer，传递身份也不等于调度器遵守需求；若要 PriML 风格结论，应另给 priority 索引、允许等待规则、需求传播语义及对应调度假设。公平性只能在额外终止条件下支持最终进展，不给统一墙钟界；CPU worker 与 GPU lane 不能合并成同质处理器数。样本最大值、p95、C1 gate 顺序都不是 S/C 的证明。

## 10. 建议的下一步验收顺序与消融

先审定上述权限可复制性、区域逃逸政策、单 producer 写后读及一个 child 借一个 owner 的范围，再写完整 typing/小步规则。之后才分别做语法/检查器、参考执行语义、归纳证明和 Swift 适配；不先把现有库的实现逐行改名成类型系统。

必须保留至少以下分离对照：仅手写协议；动态 owner/义务账本但无静态 live 逃逸约束；再加权限/区域/效果规则；最后另加需求传播或服务政策。删除静态规则可导致误用获准，却未必击穿仍存在的动态守卫；删除 runtime 的义务保留则可能在合法 drop 后出错。不能把任何负例都算成所有层同时必要。

初个反例集应覆盖：结果消费者取消 producer、共享合法控制者、live closure 逃逸、已注册读与关闭竞争、同 owner 重入、全部 waiter 离开、失败兄弟、unknown child、ready 未满足的 cleanup、后代等待祖先返回形成循环，以及删除实际 join/完成观察守卫。正常控制要覆盖重复结果等待、多个读消费者、关闭后继续旧读、原故障安全归还与下一 epoch 的真实工作。C1八个实际case的独立事件/来源验收已经完成；从这些有限轨迹及具体实现到此语言的精化证明仍待建立。

本提案希望统一的是权限、登记、依赖、完成与故障的可组合语义。设备执行位置、kernel 融合、布局或调度器可以保持不同实现；它们是否保留这些语义、造成什么额外成本，需要各自的契约和证据。
