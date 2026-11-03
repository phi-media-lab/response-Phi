# P8 核心语言：类型判断、状态与小步规格

2026-10-30，修订草稿 v0.4-r1：在 R6 源执行退场候选上，补启动 Unknown 的实际状态、load 完成后的单次消费及 INIT-MAIN。**待 root 复核，未验收；尚无运行时解释器、机械化证明或健全性定理。** 静态 checker 的后续实现单独记录，不能用它声称本动态稿已实现。[v0.1](../experiments/core-semantics-review/v0.1/P8_CORE_SEMANTICS.md)、[v0.2](../experiments/core-semantics-review/v0.2/P8_CORE_SEMANTICS.md)和 [v0.3 候选](../experiments/core-semantics-review/v0.3-draft/P8_CORE_SEMANTICS.md)原档不变；原反例见 [FINDINGS-v0.1](../experiments/core-semantics-review/FINDINGS-v0.1.md)、[R6](../experiments/core-semantics-review/FINDINGS-v0.2.md)及 [v0.3 独立复核](../experiments/core-semantics-review/INDEPENDENT-R6-v0.3.md)。作者的 [v0.3 说明](../experiments/core-semantics-review/v0.3-draft/REVISION-R6.md)和 [v0.4 说明](../experiments/core-semantics-review/v0.4-draft/REVISION-CLOSURE.md)不作为独立审阅或保持性证明。

本次 r1 仅修订[独立复审 D1](../experiments/core-semantics-review/INDEPENDENT-CLOSURE-v0.4.md)的重复启动 Unknown 诊断累积；[修订说明](../experiments/core-semantics-review/v0.4-r1-draft/REVISION-D1.md)仍待定向复核。原 v0.4 冻结稿保持不变。

依据为 [语言子集提案](P8_LANGUAGE_SUBSET_PROPOSAL.md)、[研究路线](../ROADMAP.md)、[实验契约](EXPERIMENT_CONTRACT.md)与已独立通过的 [C1 实验](../results/batch5/cancellable-wait-REPORT.md)。C1 的 8 个实际 trace、97 项检查器测试是规格选择的依据，不是本语言的类型健全性证明。旧有限模型的 836 状态也不充当本规格的无限状态证明。

## 0. 对提案的明确精化

| 原提案中的未定处 | 本稿决定及原因 |
| --- | --- |
| `Shared` 的结果 publication 与 Task completion | `await` 保留原 Task 等待，需真实 task terminal；`await?` 在 outcome publication 上进行取消／交付仲裁。二者均不代替 owner 的 join 和访问证据。与 C1 两条 API 的差别一致。 |
| `fork s read l` 的 `s` 是谁 | `s` 是 child 的登记／origin scope；`l` 指向 resource scope，可不同。一个原子事务同时登记目标 scope 的 Task 与源 owner 的 access/pin。 |
| scope close 后区域是否失效 | body exit 只关闭新准入。scope live region 到最终 join 才结束；已登记 read job 有自己的 live region，到其执行体实际结束才撤销 Read。原 resource pin 仍保留到实际 Task join。 |
| `Owner` 可逃逸而 `new_owner` 引入新类型名 | owner 的 generative brand 与 live region 分离。通过存在打包隐藏 brand；owner handle 是持久申请入口，不是 live storage view。 |
| `Write` 在 Δ，却未给普通函数／捕获规则 | 使用有复制等级的闭包和输入／剩余仿射上下文。普通 spawn 不转移 live storage 能力；producer 的唯一 Write 由运行时新建并移交受信 K。 |
| `throw`、重抛与聚合 | 原 fault ID 进入单调全局日志；`rethrow` 和 scope union 保留 ID。局部 catch 可以处理自己的异常，但不能删除已登记 child 的失败 outcome 或访问义务。 |
| close 后 producer 还能发行设备命令吗 | scope 关 source-level job/lease/access 准入；已登记访问里的 backend command 可继续。backend 的 issuance seal 是另一字段。close 时固定根集合，不冻结其未来 command 子集合。 |
| 注册的部分失败 | 内部事务可以先持有真实 gated Task；commit 前不发行可用的 source job/access/Read/Write。若撤销，实际存在的 Task 必须 abort并join；明确未创建用noncreation证据，不能伪造join。commit 后不删除账本。 |
| 稳定结果中的错误／未知 | 错误值可复制；未知完成不授予 release。task 可以携带诊断终结，但对应未知访问根和 owner 仍保留。scope 不以普通结果返回。 |
| v0.1 的空错误集合 | 新增 opaque `Failure` 非空 fault 集类型作为 catch/rethrow/err payload；空 FaultSet 只作 cause／集合数据。scope 不再可能把 err(∅)当作没有失败。 |
| v0.1 的隐式执行者和依赖闭包 | typing加入Θ，明确当前job/Task/invocation/region；动态Req用单独的requires图，不把issuer parent、捕获Scope值和实际同步子scope混同。 |
| v0.2 源 region 结束后仍有旧环境／Θ | 单独记录 region 生命周期与 sourceRetired；源结果到达终结哨兵后，按 RETIRE 原子移出 Stable outcome、移除本 job 的源环境／局部栈并切至 runtime Θ。reader／cleanup 的源 region 此时结束，job region 直到 actual Task terminal 才结束；未知物理义务不随两者清除。 |
| v0.3 启动 Unknown、load 消费与 main 初态 | C 表示实际状态，启动观察 Unknown 不猜测／丢弃实际源控制；命令 Safe 与 load reply 单次消费分开；main 由显式 host 根登记，隐式 scope 位于 main region 内，不自 join。 |

没有新增布局、fusion、工具效果、设备 Future、优先级或调度接口。它们以后必须通过各自 backend／扩展的契约精化本核心，不能由这个统一语法推定已经组合。

## 1. 名称、签名与记号

有无限且互不相交的名称集合：owner `ρ`、epoch brand `γ`、scope `s`、job `p`、实际 Task `τ`、invocation `ι`、access `α`、lease `ℓ`、pin `η`、backend command `δ`、wait `w`、fault `f`、live region `r`、内部登记事务 `ζ`。运行中只出现有限前缀；`fresh` 必须避开全部已发行名称，包括已终结历史。epoch 还带 owner 内递增 ordinal `g`；`γ` 不等同于任意可构造的整数 `g`。

`Ψ`是有限签名世界，记录名称sort和持久关系：`owner(ρ,n)`、`epoch(γ,ρ,g)`、`scope(s,r,kind,parent)`、`job(p,τ,b,T)`、`jobRegion(p,rJ)`、`bodyRegion(p,role,rB)`、`result(p,T)`、`producerOf(p,s,ρ,γ)`、`access(α,p,ρ,γ,rα)`。job的登记边界b通常是源scope s，唯一main特例为不可伪造的运行时常量host；host不是Scope值，源REGISTER不能以它为目标。kind为plain或owned(ρ,γ)。jobRegion记录完整Task的执行region，bodyRegion记录ordinary／reader／cleanup源执行所在region；它们不宣称相应region当前live。名义索引不能由字符串／整数cast构造，也不能制造owner/control/shared/access常量。

另有非权限性的自然数size索引`n ::= numeral | n̂`。n̂是绑定到运行时自然数的静态witness，不是新资源brand；Ψ可保存`n̂>0`及创建操作确立的size等式，普通Nat参数本身不自动成为类型级常量。NEW的正常分支引入这个witness，失败分支不假装它存在。ΣK可以是对size索引一致的签名schema `∀n>0.(A(n),n,post(n))`，按已知witness实例化；固定大小K只有在Ψ能证明size相等时适用。本稿不含任意依赖算术求解，只有字面相等、witness别名和显式存在接口提供的持久等式。

存在接口使用telescope `Ξ=(ν1:sort1,…,νk:sortk;Φ)`，Φ仅含上述持久结构关系、result类型与size约束，不能含scope open、access ended或safe completion等动态事实。`∃ν:sort.A`只是无额外关系需要时的简写；否则必须写`∃Ξ.A`。pack需给全部witness并在当前Ψ证明Φ，unpack以fresh静态skolem与Φ扩展Ψ。最小例子`∃ρ:OwnerName; owner(ρ,128).Owner(ρ,128)`使unpack后有合法run的依据；只给OwnerName sort不够。Shared可导出最小持久接口`∃p:JobName; result(p,T).Shared(p,T)`，不必导出其live Scope入口。导出关系不会生成关系所描述的capability值。

`r₁ ⪯ r₂` 表示 r₁ 的执行寿命由 r₂ 的边界覆盖；它不表示 r₂ 的 body 仍运行或准入仍 open。scope region到scope最终joined才结束；登记job的region到该实际Task terminal才结束，所属scope仍须随后观察join。job region包含reader／cleanup源region及实际同步进入的嵌套scope region。跨owner的resource pin不把lexical regions等同，它另外保留资源，可能活得比job region更久。执行region结束不是物理访问完成、Task join或permit return的证据。

`Σ` 是静态 backend／纯原语签名，至少包含：

```text
Σop(op) = (A1,…,An) → B             // 无权限生成、无隐式异步效果的总纯函数
ΣK(K)   = (A, n, post) ↦ Kernel     // A Stable；独占写一个 n 元素 allocation
Σload   = one indexed readonly command
```

`post` 规定成功时初始化的数组和值关系；完成安全性与数学正确性是两项不同契约。K 可以有输入决定的多个实际 command，不能假装 descriptor 是一次完成证据。Σ 不允许读写未声明的 owner，不返回 pointer/live capability，不创建未登记 Task，不调用任意用户 FFI callback。拒绝、部分安全失败、未知完成的外部事件见第 8 节。

后文 `E ⊎ E′` 为必须互斥的有限映射并；`F ∪ F′` 为 fault ID 集合并；`Γ|X` 为恰好自由捕获变量 X 的上下文。`fv` 包括函数体中使用的 scope 名称以及潜藏在闭包值中的能力，不能仅看最外层变量类型名称。

## 2. 类型和闭合的源语法

### 2.1 类型、复制等级与 live 支持

```text
A,B ::= Unit | Bool | Nat | Word | Vector(n,Word) | ErrorPayload | FaultSet | Failure
    | A × B | A + B | ∃Ξ. A
      | Fn[q, χ, ε](A → B)
      | Owner(ρ,n) | Scope(s,r,kind)
      | Shared(p,A) | Control(p)
      | Lease(sX,ρ,γ,ℓ,rX) | Read(α,ρ,γ,rα) | Write(ρ,γ)
q   ::= U | A                         // unrestricted / affine
```

`χ` 是闭包实际捕获的能力 footprint，保留种类与身份，不只是一个区域布尔位。它可含 `scope(s,r)`、`lease(…,r)`、`read(…,r)`、`write(ρ,γ)`。积、和、存在类型的 footprint 为各部分并；存在绑定只隐藏名义名称，不擦除 live 支持。函数 footprint 为 χ 加其类型中未绑定 live region 的支持。`Owner`、`Control`、`Shared` 的名称是持久 brand，单凭名称仍引用旧 epoch 不算 live 支持。

复制谓词 `Copy` 递归定义：基本类型、Owner、Scope、Shared、Control、Lease、Read 为 Copy；积／和／存在在所有分量 Copy 时 Copy；`Fn[U,…]` 为 Copy；Write 和 `Fn[A,…]` 非 Copy。`Shared(p,A)` 合法的必要条件是 `Stable(A)`。

`Stable(A)` 当且仅当：`Copy(A)`，其所有值、闭包捕获与未绑定类型／latent scope effect 中无 live storage 或 scope region。对普通数据、FaultSet、Failure、ErrorPayload、Owner、Control 取真；对 Shared 递归检查 payload；对 Scope/Lease/Read/Write 取假；组合递归；函数需要 Copy、χ 无 live 支持，且签名不引用未绑定 live region。错误 payload 的 cause 只含 opaque fault IDs，不含 live capability。存在量化不能擦掉 Lease/Read/Scope 类型构造本身的 live footprint，即使其 r 被绑定；因此 `∃r.Read(…,r)` 仍非 Stable。

本稿没有 rank-2 region polymorphism、隐式泛型／subtyping／effect masking。存在类型、显式注解和 α-renaming 足以表达持久 handle 的逃逸。不能声称这个保守前端接受所有动态 Swift lease 的合法程序。

### 2.2 值、纯表达式与命令

```text
v ::= x | () | true | false | numeral | word | [word,…,word]
    | (v,v) | inl[A,B] v | inr[A,B] v
    | λ[q; captures X; χ; ε](x:A). c
    | pack[Ξ:=witnesses] v as ∃Ξ.A

e ::= v | fst v | snd v | op(v1,…,vn)

c ::= return v
    | let x = e in c
    | let x ← c in c
    | let x ← call v v in c
    | if v then c else c
    | case v of inl x ⇒ c | inr y ⇒ c
    | unpack (Ξ,x) = v in c
    | for i < v do c ; c
    | throw v | rethrow v | try c catch x ⇒ c
    | new_owner v as (ρ,o) in c
    | scope s { c } | run v as s { c }
    | produce s K(v) cleanup (a ⇒ cR) as (p,h,k) in c
    | fork s { cJ } as (p,h,k) in c
    | lease s v as (ℓ,l) in c
    | fork s read v as a { cJ } as (p,h,k) in c
    | let x ← load v v in c
    | let x ← await v in c
    | let x ← await? v in c
    | request_cancel v ; c | drop v ; c
```

`p,ρ,ℓ` binder引入新抽象名；需要携出必须以带关系约束的存在接口打包。NEW还隐式绑定一个size witness n̂；完整注解形式可写`new_owner v as (n̂,ρ,o)`，没有把任意Nat参数当成已知类型常量。表面`let(h,k)=produce…`是generative binder语法糖。scope/run的s绑定Scope入口，没有关闭权值。for的bound进入时求值一次，体返回Unit。没有递归、通用mutable cell、raw pointer、任意continuation、未登记spawn或源级直接K调用。

`ErrorPayload` 可由 Σ 的纯 `error(code,data,cause)` 构造：data 必须 Stable，cause 为已有 FaultSet 或空。`Failure` 是具有至少一个既有 fault ID 的 opaque 非空集合；catch 绑定 Failure，`rethrow` 只接受 Failure。FaultSet 可以为空；纯 `forgetNonempty:Failure→FaultSet` 和集合 union 不创造 ID，不能把空 FaultSet cast 成 Failure。FaultSet／Failure 的外部参数必须满足配置typing，不能从整数制造 fault ID。`throw` 创建新原错误，`rethrow` 不创建。

## 3. 类型判断与每个源构造的规则

### 3.1 上下文与基础判断

```text
Ψ ; Θ ; Γ ; Δ ⊢ v : A ▷ Δ′
Ψ ; Θ ; Γ ; Δ ⊢ e : A ▷ Δ′
Ψ ; Θ ; Γ ; Δ ⊢ c : A ! ε ▷ Δ′
Θ = (job=p, task=τ, invocation=ι, region=r, scopeStack=σ)
```

Γ 只含 Copy 类型，允许弱化／收缩；Δ 只含非 Copy 类型且变量唯一，禁止收缩。Δ′ 是输入中尚未被移动的变量，不能含新生成的内部变量。命令正常返回可在返回值中转移非 Copy 权限；异常路径的局部值可被丢弃，但 runtime obligation 不因此删除。每条规则都可以 frame 一份不相交且未被自由使用的 Δ；不是暗中复制它。

`bind(x,A)` 在 Copy(A) 时加入 Γ，否则加入 Δ。Θ 描述当前执行者，普通局部调用保持job/Task而改变局部调用frame；scope body切换invocation/region并压入σ；spawn的child使用fresh job/Task/invocation/region而不继承父执行者身份。类型形成检查 Ψ 中索引种类、scope-owner关系、n 的合法性和 Shared payload Stable。每个live footprint要求当前region的执行寿命被相应region覆盖，或是本job的显式Read region；这不把动态access authority是否已revoked编码成静态类型。

效果为有限集合：

```text
atom ::= Alloc | Enter(s) | Register(s) | Issue(ρ) | Read(ρ)
       | Need(p,publication) | Need(p,terminal) | WaitDetach(p)
       | RequestCancel(p) | Raise | Drain(s)
ε ::= finite-set(atom) | ε ∪ ε | ν name:sort. ε
```

有限效果是上界，允许加入额外项。`ν name:sort.ε` 是生成名称的effect binder，α等价，bound名称不得与外部Ψ别名混合；嵌套binder按生成关系排序。每个生成构造在自己的continuation内部扩展Ψ，退出时把其内部新名从effect中用ν绑定；外部结果类型与剩余上下文不得自由含这些新名，必须由源程序存在打包，不能仅靠effect binder隐藏类型逃逸。`pack/unpack`的value witness与effect binder是各自显式的抽象范围，不假定调用者知道它们的别名关系。Need只记录语义依赖；没有优先级传播定理。

ε在本稿统一表示“此命令及其显式注册／同步执行子工作”的may-effect上界，不只是当前Task直接执行的动作。因此FORK/FORK-READ/PRODUCE结论必须并入child/cleanup的ε以及父continuation ε，再做新名ν闭合；J保存被检查的child latent ε作为配置typing证据。普通await某个已有Shared只加Need/Raise，不把目标的全部内部effects或ownership并进caller；该目标由其原登记配置单独检查。这是effects范围选择，不是服务成本求和公式。

### 3.2 值与纯表达式：穷尽规则

| 规则 | 前提 | 结论／上下文变化 |
| --- | --- | --- |
| V-U | Γ(x)=A | `x:A ▷ Δ`；不从其他变量推导额外权限。 |
| V-A | Δ(x)=A | `x:A ▷ Δ\{x}`；唯一一次移动。 |
| V-CONST | literal 与其基本类型匹配 | 不改变 Δ。vector 长度为注解 n，元素为 Word。 |
| V-PAIR | 先检查 v1 得 Δ1，再检查 v2 得 Δ2 | `(v1,v2):A×B ▷ Δ2`。 |
| V-INJ | 检查 v:A 或 v:B | 相应和类型，保留剩余上下文。 |
| V-PACK | witness各自符合Ξ的sort；Ψ证明Φ[witnesses]；v:A[witnesses] | `pack…:∃Ξ.A`；隐藏名字但保留接口关系与live构造，不能装入未经证明的owner/epoch关系。 |
| V-LAM-U | 捕获恰为 X；ΓX=Γ\|X；没有 Δ 捕获；χ=Foot(ΓX)；以 x:A 检查 body:B!ε；body 局部 affine context 被消耗／丢弃 | 产生 `Fn[U,χ,ε](A→B)`，外部 Δ 不变。函数可以接收 affine 参数，但没有隐式 affine capture。 |
| V-LAM-A | 将 Δ 分为被捕获 Δc 与 frame Δf；χ=Foot(ΓX,Δc)；以 Δc 与 x:A 检查 body，所有局部 affine 残余显式弱化丢弃 | 产生 `Fn[A,χ,ε](A→B) ▷ Δf`。创建 closure 时移动 Δc，不等待调用才复制。 |
| E-FST/SND | v:A×B 的 affine 使用检查通过 | 返回对应分量；未返回分量执行 affine drop，不归还底层资源。 |
| E-OP | Σop 签名匹配，参数按从左到右顺序检查；每个传给纯 op 的类型 Copy、op 不隐藏 capability transfer | 返回 B，顺序传递剩余 Δ。 |

λ 的体不得通过未声明的自由变量／scope 访问权限。`χ` 由捕获环境计算，用户注解只能与计算值相等，不能自行填写为空。affine closure 被 drop 时丢弃其捕获值，但不会删除 ledger；不存在凭 closure 析构归还 allocation 的规则。

### 3.3 顺序、分支、函数、循环和异常：穷尽规则

`RETURN` 按值规则消费 v，产生其类型，效果为空。`LET-E` 先检查 e，再以 bind(x,A) 检查后续 c；局部 x 的 affine 残余在离开 binder 时丢弃。`LET-C` 同理连接两个命令，并合并效果。`CALL` 顺序检查函数和参数，要求 Fn 的参数类型相同，callee 的 χ 所需 region 仍 live；结果为 B、效果加入函数 latent ε。调用 affine closure 消耗它，调用 unrestricted closure 不消费其 Γ 引用。

`IF`要求Bool，两分支同输入/结果类型/输出Δ′，效果取并。未用affine变量可显式drop以汇合，不能复制。`CASE`消耗和类型值并bind分量，外部剩余Δ一致。`UNPACK`给Ξ的全部名字fresh静态skolem并加入Φ，再检查body；结果和外部Δ′不可自由含skolem，除非重新存在打包。函数/Shared/Owner等类型形成据Φ进行，不凭sort猜测关系；Region存在绑定仍不擦除live footprint。

`FOR` 检查 bound:Nat；体在 Γ,i:Nat 与空 affine capture 下返回 Unit。外部 Δ 完整 frame，不能把同一 affine 值用于不定次数 iteration。每轮可在体内生成并关闭新的 owner/scope/affine 局部值。体与后续的效果取并；循环期间若抛错，后续 iteration 不执行，原有 runtime obligations 仍由外层 close/drain 负责。

`THROW` 要求 Stable ErrorPayload，效果 Raise，结果类型可按上下文任意标注；该路径不会正常返回。`RETHROW` 要求 Failure（非空），效果 Raise，原 fault 不重命名。`TRY` 对 c 和 handler 检查相同结果类型；handler 的 x:Failure 放入 Γ。为给出可判定的保守 affine 规则，try 与 handler 都不得捕获外部 Δ；外部 Δ frame 到最终 continuation，局部产生的 affine 值可在各分支结束时丢弃。handler 可以执行资源操作，但必须按普通规则登记；异常不隐式取消兄弟。

这条 try 限制是表达能力代价，不是“所有异常安全语言都必须如此”。未来若加入逐退出路径的 affine 效果，需要单独定义成功／异常剩余环境与证明，不能直接删除限制。

### 3.4 Owner、scope、produce 与普通 fork

`NEW`：v:Nat；正常分支静态引入fresh size witness n̂、事实n̂>0与freshρ及owner(ρ,n̂)，在Γ bind o:Owner(ρ,n̂)检查body。这是对runtime正数检查后的正常分支检查，不要求预先证明任意Nat参数非零。若v为字面N，还加入n̂=N；动态参数不添加猜测等式。结果／外部剩余环境不得自由含ρ/n̂，需以含owner/size关系的存在接口打包。effects至少{Alloc,Raise}。runtime size=0或分配失败走错误分支，不创建实际owner；handle Copy/Stable不提供pointer。

`SCOPE`：引入 fresh s、rs、ιs，kind=plain；body 的 Θs=(Θ.job,Θ.task,ιs,rs,s::Θ.scopeStack)，且rs⪯Θ.region。Γ 增加 Scope(s,rs,plain)，Ψ增加scope/invocation与region关系。c正常结果必须Stable，外部affine context不被body捕获（frame），内部可创建局部affine值。结果类型与外部剩余环境不得自由含s/rs/ιs；局部身份若要携带必须存在打包且payload仍Stable。effects加Enter(s)、Drain(s)后用ν绑定内部新名。异常也经过相同drain。

`RUN`：检查 owner:Owner(ρ,n)，引入 fresh γ、s、rs，kind=owned(ρ,γ)，随后同 SCOPE。一次 run body 可以零次 produce；可以写出第二次 produce 的程序，但运行时 second-producer rejection 在准入前发生。第一版不引入控制流敏感“恰好一次 produce”静态 typestate；独占写安全依靠唯一 slot 和 Write authority。

`PRODUCE` 的完整前提：

1. 当前可用 Scope(s,rs,owned(ρ,γ))；参数 v:A Stable 且 ΣK(K)=(A,n,post)，owner 大小匹配。
2. 预登记 cleanup 的自由捕获均为 Stable，允许生命周期覆盖 producer job 的 Scope 入口；不捕获已有 Lease/Read/Write 或 affine closure。
3. 引入fresh p、τ、ιP、rP、αW、αC、rC，rC⪯rP⪯rs；Ψ记jobRegion(p,rP)、bodyRegion(p,cleanup,rC)，αW的执行region为rP、αC为rC，两个access的授权job都是p。cleanup以ΘC=(p,τ,ιP,rC,[producerScope s])，在`a:Read(αC,ρ,γ,rC)`下返回T，Stable(T)。正常／异常出口都交给同一job的JobFinish哨兵，不能直接作为Task terminal。它可以同步调用捕获a的局部函数，但不能把a隐藏进Stable结果或普通fork。
4. 生产者的内部 kernel 配置独占 `ΔK={w:Write(ρ,γ)}`；这份新 Write 不留在父 continuation，也不交给 cleanup。
5. 父continuation保持原Θ，在h:Shared(p,T)、k:Control(p)下检查。外部结果／剩余环境不得自由含新p/τ/ιP/rP/αW/αC/rC，所需持久p可存在打包；效果以ν绑定这些内部新名。k可复制但必须显式交付。

effects 为参数／cleanup／continuation 的并，加 Register(s)、Issue(ρ)、Read(ρ)。静态规则不声称 s 当前 open、owner 已 idle 或成功初始化；这些是运行时 guard。即使 caller 丢弃 h/k，producer 的完整登记记录不变。

`FORK`：目标Scope(s,rs,kind)必须可用。cJ捕获集合ΓJ不能有Lease/Read/Write（包括闭包内部）或affine capture。若捕获Scope(s′,rs′,…)，必须有rs⪯rs′，使登记scope的join覆盖该入口的lifetime。引入fresh p、τ、ιJ、rJ，rJ⪯rs，记jobRegion(p,rJ)、bodyRegion(p,ordinary,rJ)；以ΘJ=(p,τ,ιJ,rJ,[registrationScope s])检查cJ:T且Stable(T)。ordinary不另造一个短源region：源代码退休后rJ仍覆盖runtime退场，actual terminal才结束。父continuation保持原Θ并获h/k，父Δ frame保留。新名的结果逃逸/ν效果闭合按PRODUCE第5项。child可在同一尚开放scope登记后代，关闭竞争动态拒绝。

这明确给出了并发上下文分配：Γ 只复制可获授权的捕获，scope 捕获须 outlive，live storage 捕获必须走显式 read 注册，Δ 不复制。名字在 Ψ 可见不自动构成 capability；child 只有实际环境里收到的 Scope/Control/Owner/Shared 才能调用相应操作。

### 3.5 Lease、跨 origin 的读注册、load 与等待

`LEASE`：s:Scope(sX,rX,owned(ρ,γ)) 且 h:Shared(p,T)，Ψ 证明 p 是该 epoch 的 producer，而非任意 child。同一 producer 身份关系由 produce binder 和 runtime tag 保留，不由 T 相同推出。引入 fresh ℓ，l:Lease(sX,ρ,γ,ℓ,rX)，检查 continuation。效果加入 Need(p,publication)、Register(sX)、Raise。动态等待成功 publication 并再次检查 source gate；结果失败或关闭都不生成 lease。

`FORK-READ`：

```text
target : Scope(sY,rY,kindY)        l : Lease(sX,ρX,γX,ℓ,rX)
fresh p,τ,ιJ,rJ,α,rα             rα ⪯ rJ ⪯ rY
ΓJ 的其他捕获满足 FORK 的 Stable／Scope-outlive 限制
ΘJ=(p,τ,ιJ,rα,[registrationScope sY])
Ψ+job/access/regions+jobRegion(p,rJ)+bodyRegion(p,reader,rα);
ΘJ; ΓJ,a:Read(α,ρX,γX,rα); ∅ ⊢ cJ:T ! ε ▷ ∅
Stable(T)
────────────────────────────────────────────────────────────────────────
fork sY read l as a {cJ} as (p,h,k) in c
```

结论给保持原Θ的continuation h:Shared(p,T)、k:Control(p)，父Δ frame；effects加Register(sY)、Register(sX)、Read(ρX)，新名按PRODUCE第5项闭合。l是登记时source authority，不被隐式复制到child。Read具有新rα；只有READER-EXIT及RETIRE的完整前提满足，才撤销源访问、清除源环境并结束rα。rJ保留到actual terminal；pin还需Access Safe及source finalizer的实际join才能退休，unknown pin不随rJ结束。父scope close不直接结束rα。target/source scope可不同，静态不unify。

`LOAD`：a:Read(α,ρ,γ,rα)，index:Nat；Ψ.access(α).job=Θ.job，当前Θ.region的执行仍在rα内，返回Word；effects Read(ρ)、Raise。不能用其他job的Read，即使owner/epoch相同。类型保证tag授权者和lifetime，动态authority可能因unknown已revoked；这种tag仍可存在但LOAD guard必须在真实提交前拒绝。index越界同样在访问前抛错。

`AWAIT`：h:Shared(p,T)、Stable(T)，返回 T，effects Need(p,terminal)、Raise。`AWAIT?` 同一前提，返回 `T+Unit`（Delivered/WaitCancelled），effects Need(p,publication)、WaitDetach(p)、Raise。producer failure 通过 Raise 传播；本地 WaitCancelled 是右值，不伪造成 producer fault。

`CANCEL`：只能接收 Control(p)，effects RequestCancel(p)，后续环境不变。Shared 不可替代 Control；Control 复制／重复请求合法。`DROP` 消费值表达式的 affine 使用并丢弃值，后续命令不获新权限、不改变义务；Copy 值的其他引用保持可用。

**动态失败的最小效果不能省略。** NEW至少加`{Alloc,Raise}`；SCOPE加`{Enter(s),Drain(s),Raise}`（含所属子工作失败聚合）；RUN加同样集合与Raise（含busy）；PRODUCE/FORK/FORK-READ均强制加Raise（关闭、重复producer、部分启动故障）；LEASE/LOAD/AWAIT/AWAIT?强制加Raise。UNPACK/CANCEL/DROP/纯值操作在良类型配置和其纯契约下不新抛错。effect widening只能加项，不能让这些构造少报Raise。作用域与job的latent effects按前文合并／ν闭合，包含子工作可能失败而非只看父body纯度。

NEW、RUN、SCOPE、PRODUCE、FORK、FORK-READ、LEASE的每一个新名都遵循统一出口规则：在body/continuation推导中加入Ψ；构造的外部结果A和剩余Δ不得自由含该新名；需要携出的名由`pack`绑定且类型仍须满足调用边界的Stable要求；effect使用ν闭合。RUN引入的γ也受此规则约束，LEASE的ℓ不能例外。UNPACK以新的静态skolem检查body，离开时同样禁止自由skolem逃逸。

至此第 2 节每个值／表达式／命令构造均有类型规则。没有默认允许的未列 constructor；实现检查器遇到未知节点必须拒绝。

## 4. 动态配置与配置 typing

运行配置：

```text
C = ⟨Ψdyn, Λ, H, O, S, J, A, L, P, D, W, Tx, Ω, F, R⟩
```

H是封装allocation内容。O(ρ)=(allocation,n,lastOrdinal,activeEpoch?,gateScope?)。活跃epoch record保存γ/g、stage=`fresh|writing|cleanup|readable|failed|quarantined`、producer?；scope admission独立于stage。S(s)=(region,parentInvocation,bodyTask,kind,admission=open|closed,bodyOutcome?,localJobRoots,localRuntimeRoots,synchronousChildren,observedJoins,state=body|draining|joined|unresolved)。`synchronousChildren`仅记录该body实际进入且等待退出的子scope，不包含捕获的Scope入口值。

Λ(r)=(kind,parent,state)，kind为host／scope／job／accessBody，state为dormant／live／ended。唯一rHost为运行时永久live根，parent=none，不向源程序发行capability。Ψ的名称／关系是永久历史，Λ是执行权限的动态生命周期：scope进入时live、最终joined时ended；job在commit时live（main由INIT-MAIN建立）、actual terminal时ended；reader rα在commit时dormant、实际START-READER时live；cleanup rC在commit时dormant、K-SUCCESS时live。RETIRE结束已完成的rα/rC，never-started路径凭永久禁启证据将dormant源region置ended。ordinary与kernel使用job region，不在源退休时结束它。ended无重新激活规则。

J(p)=(τ,registrationScope,originInvocation,region,kind,env,Θ?,c,stack,startEnvelope?,startGate,startupObservation,latentEffect,cancelBit,appStarted,sourceRetired,preparedOutcome?,publishedOutcome?,terminal,joinObservers,synchronousChildren)。region固定为rJ（producer记rP），Θ.region是当前控制点所在region，不得混作同一字段。NameEnv是每份源env内含的witness映射（见第5节），不是J的额外字段；env为空时其NameEnv也不存在。startEnvelope在START/K-SUCCESS前唯一保存待移交的kernel参数、Write及cleanup代码／捕获；一旦移交便清除该份能力，不把它当成源环境备份。sourceRetired初始false，只由RETIRE置true，不反向。

startGate是实际运行时状态`closed|released|abortOnly`，仅closed可原子走向released或abortOnly，无回边。startupObservation的status为`unreported|ackStarted|ackNoStart|unknown(Failure)`，每种status还附只追加的观察events；记录调用者获得的启动观察，不是实际gate或PC的替代。C始终描述实际执行状态，观察Unknown可对应不同的实际C，却不把两份能力复制进一个配置。trusted runtime必须始终唯一持有实际控制、环境和登记账本，并在资源程序可能执行前原子完成START的能力／region移交；观察者可以不知道这一点是否已发生。丢失了实际授权／控制记录的runtime不满足此精化前提，不能通过臆造closed或清空env宣称实现本核心。

`appStarted`指资源程序越过start gate，不等于Task对象创建。kind为ordinary、producer(ρ,γ,K,cleanupCodeId)、reader(ρX,γX,ℓ,α,η)；cleanupCodeId只是不可执行的历史代码身份，实际待执行closure只在startEnvelope。outcome=`ok(v)|err(Failure)`，err非空且payload Stable。preparedOutcome只归内部publishReady控制持有，不对Shared/lease可见；publishedOutcome仅PUBLISH写入一次。terminal与join仍是独立字段。retired job仅有runtime控制及Stable outcome／持久身份元数据，不能保存可调用的源cleanup closure或再进入源代码。

A(α)=(resource=(ρ,γ),origin=(ι,p,τ),region,state,authority=enabled|revoked,reservedRole,commands,issuanceSealed,unknownFailure?)。state为reserved、started、ended、abortedBeforeStart、unknown。L(ℓ)记录source scope/owner/epoch与admission状态；不直接保留存储。P(η)记录reader p的source lease/owner/epoch、access α、sourceJoinObserved与未释放／安全退休／unknown。D(δ)记录真实command descriptor、parent α、submitted、deviceResult?、hostObservation?，并对load另存`reply=none|ok(Word)|err(Failure)|unknown(Failure)`及replyTaken=false|true；非load的reply不用于源返回。reply一旦非none不可重写，replyTaken只由消费该load唯一kont的转移置true。callback必须绑定真实descriptor身份与证据，不能靠可伪造的字符串生成完成权。

W(w)=(caller pC,target p,mode=terminal|publication,handlerInstalled,continuationInstalled,registration,choice,resumeIssued,returned,kont?)。registration为created/attached/skipped/cached；choice为⊥、delivered(outcome)、cancelled。kont唯一持有caller的后继Bind frame、env与原stack；caller阻塞时这些affine槽不在J与W间复制。R保存wait/Task/owner运行时强根，不由源Shared/Control引用计数决定。

Tx(ζ)=(issuerInvocation,anchor,kind,target/source tags,capturedEnv,provisionalTask?,resourceGate,phase,completionEvidence,kont?,diagnosticFailure?)。phase为preparing/prepared/committed/rollingBack/safe/unknown。provisionalTask若存在必须指实际gated Task，不能仅是分配的τ名字。kont是parent后继的唯一持有处，恢复parent后必须置none；它不同于可能继续保留的未决Task／capture根。未commit前没有给该Task Read/Write或资源入口许可；Tx仍是实际runtime义务并持有全部准备根。

Ω为单调issued ID集及其账本状态。每个条目有kind、唯一issuer（谁发行它）、明确requires边和三选一class：Active、Safe、Unresolved。issuer用于追踪来源，不能当成全部依赖图。Safe按kind定义，不能用“Task Safe”替代“Access Safe”：

| kind | Safe 所需的证据 |
| --- | --- |
| Job(p) | 实际 task terminal 已被所属 scope／专用 finalizer join 观察；outcome 原样保存。 |
| Access(α) | 所有已发行命令实际安全完成并观察、issuance sealed、授权程序已撤销该 access；或有实际 never-started 证明。 |
| Command(δ) | 确实未提交且有拒绝证明，或已提交工作被真实完成观察覆盖；descriptor 创建不够。 |
| Pin(η) | 对应 Access Safe 且关联实际 Task 已 join；仅 read 结束或结果发布不够。 |
| Registration(ζ) | 成功commit且全部Task/能力/根已移交已登记Job，或实际Task回滚abort并join，或确切noncreation证据。unknown工厂结果不能记Safe。 |

Fault map F 单调保存 `f ↦ (originInvocation,obligation?,localOrdinal,payload,causes,reportedVia)`。FaultSet 聚合不创建新 f；新 wrapper 的 throw 才创建，并保留 cause 边。根 R 至少覆盖全部 Active/Unresolved obligation 的 task/access/storage/backend references，以及未消费结果／观察所需不可变 outcome。R.host=(main,τMain,joinObserved=false|true,delivered=false|true)持有main真实Task；它不是源scope或Ω的新kind，只有INIT-MAIN/HOST-JOIN/HOST-RETURN维护。

配置 typing `Σ ⊢ C wf` 为以下可检查约束的合取，不含死锁自由或调度公平性：

1. Ψdyn 名称唯一、关系 well-sorted；O/S/J/A/L/P/D 的 tag 与 Ψdyn 相符。Λ的parent与Ψ的region包含关系相符，live子region的祖先皆live；ended无恢复边。每个Task只有一个当前控制点，scope close authority只在其frame中。
2. sourceRetired=false的活动源job以Θ及Λ(Θ.region)=live检查env/control/stack；阻塞在W/Tx/loadWait时只在唯一kont处检查保存的源环境。尚未START的ready job满足实际startGate=closed、appStarted=false、唯一startEnvelope，Θ=none、尚无活动源env／stack；kernel按ΣK检查其唯一Write配置。实际START后startGate=released、appStarted=true，能力已唯一移入相应源／kernel环境。实际abortOnly且未退休时由封套检查证明永久禁启，不能执行源code。startupObservation=unknown可以叠加于这些实际形态，既不要求实际gate closed，也不替代每种形态的typing；相关状态及后继见第7.1节。Copy/affine capture与登记规则一致；每个affine capability在env、stack、kont、startEnvelope和backend配置中至多一份。sourceRetired=true且未terminal的job满足第8.2节RuntimeExit typing；terminal job没有可调度Θ。静态skolem由runtime witness环境解释，不能重命名实际实体brand。
3. Read值仅在其授权job尚未退休的源环境／同步局部栈／该job唯一kont／局部closure中，且其源region live；不在Stable outcome或runtime退场环境。authority可在region仍live时revoked，catch可持有opaque Read值但LOAD必须另检查enabled+started+未sealed。RETIRE前必须移除所有这样的可执行值及closure；A/Ψ/诊断中对已ended region的身份引用是元数据，不是Read capability。Scope/Lease值只由其live region覆盖的执行上下文持有，RETIRE不携带这些值进入runtime环境。
4. Write authority 恰属该 epoch 的单 producer backend；新 writer 与任何旧代未 Safe 的 access/pin 不共存。producer 未成功完成写阶段时没有可使用的 cleanup Read。
5. 每个源REGISTER登记的job都在目标S.localJobRoots及Ω；reader同时在source owner的pin/root集。唯一main的registrationScope=host，由Ω.Job(main)及R.host负责，不属于任何S.localJobRoots。resource程序只能在完整commit后START；main由INIT-MAIN明确初始化。准备中的实际gated Task归Tx和Registration义务持有，不能凭它存在就声称已经给source发行job/access。
6. 所有已提交 D 都有唯一 parent α，且对应 Ω Command；seal 之后不会再给该 α 新 command。S.closed 只禁止新 root job/lease/access，不禁止已有未 sealed α 的合法 command。
7. W.choice至多一个，handlerInstalled/continuationInstalled/resumeIssued/returned单调；返回必须持有唯一kont并已安装continuation和唯一resume。terminal-mode交付必须以target真实terminal为前提；publication-mode先装handler再执行continuation安装operation。
8. Ω 的三类互斥完备，历史 ID 从不删除；Safe 各自附有表中证据；R 覆盖所有未清偿根；Unresolved 不具有 release 证据。
9. 每个published outcome满足Shared payload类型；err持有非空Failure、所有IDs在F，传播不改origin。局部catch可处理普通业务异常，但不能抹掉登记child的最终err或unknown obligation的诊断Failure。
10. 每个返回到scope外的值Stable；Λ.ended region没有正在执行或可恢复的源上下文／capability。scope若因unknown尚不能joined，其region及ScopeExit保持live；reader／cleanup源执行却可在RETIRE后结束其源region而保留未知A/D/P/R。job region只由actual terminal结束，结束region不改变Ω的Unresolved或构造Safe证据。
11. sourceRetired的job满足：源env／局部stack／startEnvelope为空，无任何指向其源后继的W/Tx/loadWait kont，无未joined同步子scope；Θ为RuntimeExit(p)，或terminal后为none。Job.registrationScope、synchronousChildren、issuer／requires边和故障历史仍保留；不能通过清空Θ.scopeStack删除它们。所有源规则、START及ISSUE均拒绝把已退休job作为新的源执行者。
12. load reply的Word／Failure是Stable且绑定同一个δ；ok/err reply要求Ω.Command Safe，unknown reply要求Unresolved；其是否已经消费与完成class分开。未消费时至多一个匹配caller的loadWait.kont，消费后该kont不存在且replyTaken=true。Safe不会自动恢复caller，重复完成事件不能再次消费kont。

### 4.1 INIT-MAIN 与 host 终结

INIT-MAIN是唯一启动封闭程序的运行时规则。给定在下面Θ0下已检查的main命令c:T、Stable(T)，创建实际τM与fresh m/ιM/ι0/rM/s0/r0，建立：

```text
Λ(rHost)=(host,none,live)  Λ(rM)=(job,rHost,live)  Λ(r0)=(scope,rM,live)
Ψ.job(m,τM,host,T), jobRegion(m,rM), bodyRegion(m,ordinary,rM), result(m,T)
Ψ.scope(s0,r0,plain,ιM)；r0⪯rM⪯rHost；记录ι0是该scope body、ιM是main job invocation
Ψ中的sort：m:JobName、τM:TaskName、s0:ScopeName、ιM/ι0:InvocationName
             rHost/rM/r0:RegionName；host是专用登记边界常量
Ψ中的invocation归属：ιM与ι0都属于(m,τM)，角色分别为job与scopeBody(s0)
Ψ中的region父关系：parent(rHost)=none、parent(rM)=rHost、parent(r0)=rM
ΘM=(m,τM,ιM,rM,[])        Θ0=(m,τM,ι0,r0,[s0])
J(m).registrationScope=host；originInvocation=ιM；region=rM；kind=ordinary
J(m).c=c；env=∅（封闭程序，无外部capability／affine输入）；Θ=Θ0
J(m).stack=[ScopeExit(s0,∅,∅,ΘM), JobFinish(m,ordinary,rM,rM,RuntimeExit(m))]
J(m).synchronousChildren={s0}；startGate=released；startupObservation=ackStarted
J(m).appStarted=true；sourceRetired=false；terminal=false；cancelBit=false
J(m).startEnvelope=none；preparedOutcome=publishedOutcome=none；joinObservers=∅
J(m).latentEffect=main推导的ε加隐式Enter(s0)/Drain(s0)/Raise
S(s0)=(r0,ιM,τM,plain,open,none,∅,∅,∅,∅,body)
Ω.Job(m)=Active，issuer=host；Job(m)→Scope(s0)
R.host=(m,τM,false,false)，并持实际Task根
```

封闭初态其余O/A/L/P/D/W/Tx/Ω条目和F为空，H无allocation。隐式s0不作为可见源Scope变量发行；当前invocation使main直接登记的运行时义务归s0。main不在s0.localJobRoots，r0是rM的子region，故无自join或region父子环。main正常／异常都必须先经该ScopeExit实际drain并joined，唯一恢复空外层env/ΘM后到JobFinish；若s0 unknown则不能越过它退休。随后沿ORDINARY-EXIT/PUBLISH/actual TASK-TERMINAL结束rM。

`HOST-JOIN`只在R.host持有的实际main Task已terminal并被host真实join观察时，记录R.host.joinObserved与J.joinObservers渠道；Ω.Job(m)为Active才置Safe，Unresolved只增加诊断。它不向s0加入main，也不代替任何scope自己的join。`HOST-RETURN`要求host已join、Job(m)及其requires闭包中的Ω节点全Safe／Scope节点全joined、尚未delivered，才交付同一publishedOutcome并置delivered=true；rHost仍live，历史根可由以后另立GC规则处理。Unresolved可报告诊断，但不走普通HOST-RETURN。开放程序不是往此空初态塞入tag：其输入Owner/Shared/Control及既有Task/root/region必须由扩展的良类型配置提供；这里不宣称构造了任意开放初态。

## 5. 普通小步：栈、返回、异常与捕获

局部控制使用显式CEK `(c,env,Θ,stack)`。frame完整集合如下，`EΓ`可复制，`EΔ`必须唯一持有：

```text
Bind(x,next,EΓcaller,EΔremain,Θcaller)
GenBind(nameBinders,valueBinders,next,EΓcaller,EΔremain,Θcaller,Ξinterface)
CallReturn(x,next,EΓcaller,EΔremain,Θcaller)
Try(x,handler,EΓcaller,EΔframe,Θcaller)
Restore(EΓcaller,EΔframe,Θcaller)
Loop(i,N,body,next,EΓcaller,EΔframe,Θcaller)
ScopeExit(s,EΓouter,EΔframe,Θouter)
JobFinish(p,role,rSource,rJob,Θruntime)
```

runtime控制为`ready(p)`、`abortGate(p,F)`、`abortReady(p,F)`、`val(v)`、`raise(Failure)`、`wait(w)`、`leaseWait(s,p,kont)`、`loadWait(δ,kont)`、`registrationWait(ζ)`、`drain(s)`、`kernel(p,κK)`、`publishReady(p,outcome)`、`epilogue(p)`、`terminated(p)`。Unknown记录在对应Ω/S/访问或观察字段，不以无环境的stuckUnknown标签取代实际源控制。abortGate/abortReady都保留唯一未执行封套、Θ=none、空源env/stack，实际gate=abortOnly；区别是实际Task是否已经进入abort-only控制。registrationWait仅索引Tx，不再内含第二份kont；lease/loadWait在其控制记录中唯一持有kont。余栈是continuation本身，ScopeExit不另复制一份outerStack。kont唯一持有待恢复frame/env/stack；移入W/Tx/loadWait后原Task不留第二份affine槽。JobFinish是typed源终结哨兵，只存身份／role／region与runtime Θ元数据，不保存源env、Read或cleanup closure；它不是“无条件丢弃余栈”的命令。

每份env还含独立的`NameEnv`：将该代码实例的静态binder/skolem映射到既有runtime brand或size witness。只有生成规则创建新的实际brand，UNPACK只扩展映射。GEN-BIND用于produce/fork/read的`(p,h,k)`和lease的`(ℓ,l)`后继：成功时原子安装对应NameEnv witnesses及value binders，然后执行next；失败时不绑定这些值而恢复原env/Θ进入raise。等待后继不能被实现者默认猜作单一普通let。

**CEK环境分配依类型推导的残余Δ执行。** 对`let x←c1 in c2`，若c1的输入Δ、正常残余Δ1，Bind只持Δ1，active c1只持Δ\Δ1；Γ可复制。解释器使用已检查的usage annotation，不能信任未验证用户注解或把整个env同时复制进frame和active code。对source call，函数值和实参的移动先完成，CallReturn只持余下Δ，callee独占closure捕获的affine值与affine实参。scope/try/loop按各自类型限制把外部Δ放frame、活动body只收Γ。每次正常恢复移动frame的Δ回caller一次；throw丢弃不再执行的frame值，不能删除任何runtime obligation。

配置typing对stack逐frame检查，不用“有一个推导”隐藏frame规则：Bind/CallReturn必须能在所保存Γ/Δ加x:A下检查next，并让余栈接收next结果；GenBind额外检查Ξ关系及全部新binders，失败分支不引入它们；Try的body/handler类型相同且外部Δ只在frame；Restore仅转交其唯一保存环境；Loop只接收Unit，body无外部affine capture，next在保存Δ下检查；ScopeExit只接收原scope body的Stable类型／Failure，保存Θouter且必须先drain。JobFinish的role/rSource/rJob必须匹配Ψ.bodyRegion/jobRegion与实际job，Θruntime等于第8.2节RuntimeExit(p)；它只接收Ψ.result(p,T)的Stable T或Failure，且必须是该job源栈的唯一最外层哨兵。内层ScopeExit、Bind、Try等必须按自身规则完成／unwind，不能由JobFinish跳过。wait/load/registration的kont分别满足其原源构造的frame条件，且caller job/Task身份不改变。所有`val`中的affine值也计入唯一所有权；preparedOutcome强制Stable，不允许借其字段藏affine值。

纯表达式按第 3 节值移动规则在单一步中求值；Σop 总函数、vector 索引等部分操作不放入 Σop，而通过有错误分支的 load 实现。λ 创建实际 closure `(code,capturedEnv,q,χ)`，捕获列表与 typing 一致。pair/injection/pack 保留值身份；affine move 让原槽变成 unavailable；drop 从该 env 删除值但不修改 Ω。

| 转移 | 精确作用 |
| --- | --- |
| LET-E | 求 e 得 v，按 bind 放入新 env，继续 c；使用后不能再读取 moved affine 槽。 |
| LET-C | 按残余Δ切分，压Bind(x,c2,EΓ,EΔremain,Θcaller)，在消费部分环境执行c1。 |
| GEN-BIND | 按对应REGISTER/LEASE成功返回的持久关系witness与handle安装NameEnv/value env；恢复保存Θ并继续next。runtime关系必须满足Ξ，不能以同sort替代关系证明。 |
| CALL | source call本身已有let binder，只压一个CallReturn(x,next,EΓcaller,EΔremain,Θcaller)，不含糊双压Bind；callee env仅闭包捕获+参数，callee Θ保持实际job/Task，局部同步调用不伪造新job。 |
| RETURN | `return v→val(v)`将v从active env移出；遇Bind/CallReturn时移回frame环境、恢复Θcaller、bind x=v并执行next；遇Restore时恢复环境/Θ并继续向余栈交val；到达唯一JobFinish哨兵后按role进入相应EXIT及RETIRE，不能直接发布或terminal。 |
| IF/CASE | 按实际 Bool／tag 选择一支；未选择支不执行、不发行义务；CASE bind 其 payload。 |
| UNPACK | 静态fresh skolem映射到package既有witness；runtime tag保持原brand。α-renaming只作用于静态绑定名，不给实际owner/job/epoch重新命名或创建别名实体。 |
| FOR-ZERO | N=0，直接执行后续。 |
| FOR-START/NEXT | N>0时Loop独持外部Δ和callerΓ/Θ；每轮body只获Γ+i，正常Unit返回后若i+1<N则从保存Γ/Θ开始下一轮；否则pop Loop、恢复其Γ/Δ/Θ执行next。N不重读，上一轮局部env不能泄漏到下一轮。 |
| THROW | 创建 fresh f，origin=当前 invocation，obligation=当前 job/access上下文（若有），payload/cause 原样存 F；控制变 raise({f})。 |
| RETHROW | 对已有非空Failure添加reportedVia当前渠道，控制变raise(Failure)，不分配f。没有良类型的rethrow空FaultSet路径。 |
| TRY | 压Try并把外部Δ只存frame，body收Γ。正常val恢复frame环境/Θ再向余栈传播；raise到达Try时把它换成Restore，handler只收保存的Γ加x:Failure，外部Δ仍在Restore中。handler正常返回时恢复；handler再抛则unwind Restore并丢弃其affine值。 |
| UNWIND | raise穿过Bind/GenBind/CallReturn/Restore/Loop，丢弃其不再执行的affine局部值并按frame恢复外层Θ后继续；Try按其handler规则；ScopeExit必须close/drain且保留frame，不能跳过；到唯一JobFinish哨兵后进入失败EXIT及RETIRE。未返回的ScopeExit或外置kont不被RETIRE清空来制造此条件。 |
| DROP | 丢弃已求值 v，继续 c；对 O/S/A/P/D/Ω/J.cancelBit 无变化。 |

affine drop 是不再使用该权限，不是归还资源。此规则也适用于异常 unwind。当前没有用户源规则直接获得 Write；它只出现于受信 kernel 的内部配置，后者必须自行保证 seal/drain，不依赖用户析构。

一个 task 在一个小步中只执行一个局部转移，scheduler 可选择任一 enabled task 或 backend 事件。没有默认轮转、公平或最大调度延迟；无 enabled 局部步与没有全局进展证明是不同情况。

## 6. Owner 与 scope 的所有进入／关闭／退出转移

`NEW-OWNER`：先求实际Nat N；N>0且分配成功时建立n̂↦N的NameEnv witness、freshρ及封装allocation H，O.lastOrdinal=0、activeEpoch=none，安装owner(ρ,n̂)与正size事实继续body。N=0在allocation前产生参数fault，分配失败同样Raise；不安装成功witness/Owner。字面size的等式和runtimeN必须一致。

`ENTER-SCOPE`：分配s/rs/ιs，Λ(rs)=live且parent为原Θ.region；S.open、localJobRoots/localRuntimeRoots/synchronousChildren皆空、state=body，压ScopeExit保存EΓouter、全部外部EΔframe和Θouter；body仅获Γ，仍由原实际Task执行，Θ切为该scope body。把s加入实际调用者invocation的synchronousChildren：若调用者为scope body，记入那个S；若为job直接代码，记入J。仅实际ENTER添加此关系，捕获Scope入口不添加。drain期间ScopeExit及其env仍在，unknown不能pop。

`ACQUIRE-RUN`：仅O.activeEpoch=none时原子增加ordinal、创建γ/s/rs/ιs与active fresh epoch，并执行与ENTER-SCOPE相同的环境/Θ切换、ScopeExit持有和同步子scope边登记。busy产生当前invocation的AdmissionBusy fault，**不创建epoch/scope/job/access，不排队等待自己的owner**。实现需精化原子提交，不把未定义的锁等待当成本模型。

`BODY-EXIT`：body 以 val(v) 或 raise(F) 到达 ScopeExit 时，保存 bodyOutcome，原子将 S.admission=closed、所有由 s 发行的 lease admission 关闭，S.state=draining，控制为 drain(s)。若为 owned scope，禁止该 epoch 新 producer/lease/reader root；**不改变已登记 producer/reader 的 cancelBit，不 seal 其 backend issuance，不撤销其 Read**。给 caller 设置取消位本身没有 BODY-EXIT 转移。

`JOIN-OBSERVE`：S正在drain且其localJobRoot p的实际J.terminal=true，记录S.observedJoins中的该观察渠道。Ω.Job若为Active则置Safe；已Safe则只增加渠道，不重发终结。source owner的foreign-pin finalizer可以独立实际join同一Task并记录P.sourceJoinObserved。Ω.Job已Safe不让另一负责scope跳过它自己的join观察；Unresolved Job不经此规则改Safe。

`PIN-RETIRE`：Pin当前Active，Ω.Access(α)=Safe且P.sourceJoinObserved=true、实际Task terminal，才把η置Safe。若α Unresolved则pin置Unresolved；后续task join只增加诊断，不恢复它。read早已完成但child仍等待嵌套scope／其他清理时pin保留。

`Req(s)`通过以下明确requires边求可达闭包；scope是图节点但不是可伪造的源Obligation值：

```text
Scope(s) → Job(p)                      for p∈S.localJobRoots
Scope(s) → Registration(ζ)             for ζ∈S.localRuntimeRoots
Scope(s) → Scope(t)                    for t∈S.synchronousChildren
owned Scope(s) → Access(α), Pin(η)      for its epoch's resource roots
Job(p) → Access(α), Registration(ζ)     issued by that actual job
Job(p) → Scope(t)                      for t∈J.synchronousChildren
Access(α) → Command(δ)                 for δ∈A.commands
Pin(η) → Access(α), Job(p)              the particular pinned reader
```

issuer只记来源，不自动生成反向边；Need(p)等待外部Shared也不等于把p登记到本scope。已经joined的同步子scope保留历史边但已满足，不能把它捕获的外部Scope入口递归纳入。后续实际ENTER、ISSUE等追加的是这些动态子边，因此已登记工作在父close后发生的command/嵌套工作仍可达。

Req取**正长度路径**的可达节点，不因零长度路径把s自己算作子scope；若实际requires环又到达s，则s确实作为未joined依赖使READY不成立，不能删掉该回边假装无环。`SCOPE-READY`仅当当前draining而非unresolved、本scope的localJobRoots全有自己的observedJoins、Req中全部Ω节点Safe且全部Scope节点joined、owned epoch每个foreign pin有sourceJoinObserved、无未知根。global Ω.Job Safe不足以替代本scope观察。

`SCOPE-FINALIZE`的**安全依赖集合与故障传播集合不同**。定义：

```text
DirectJobs(s) = S(s).localJobRoots
BorrowJobs(s) = { P(η).readerJob | η由s的owned epoch直接发行 }
FailResponsibilities(s) = DirectJobs(s) ∪ BorrowJobs(s)
F* = 未处理的bodyOutcome.err
     ∪ { J(p).publishedOutcome.err | p∈FailResponsibilities(s) }
```

BorrowJobs包括该epoch已安全退休pin的历史，不因pin退出活跃集而丢掉责任。source X因此收集Y登记的被pin reader**最终**err，原ID保留而reportedVia(scope,local-job或foreign-pin)分开。它不把Req的所有可达Job错误一并复活。安全回滚的registration failure只有在body最终未处理或registered job最终err里才传播；日志F的存在不等于scope必失败。

同步嵌套ScopeExit返回的err通过普通try/catch处理；若caller body已catch并返回ok，不再从已joined内层scope的历史children重新汇入。例如`try scope si {fork si {throw e}; return ()} catch f ⇒ return ()`中，si安全结束并抛f，outer body处理后可成功。若inner工作在一个被pin reader内部，其错误也只由reader的最终outcome表达；reader处理后ok不会把历史错误投影回X。

这是明确的保守whole-child failure政策：被pin的child若在完成X读取后因无关的Y工作失败，X也报告它的失败；不宣称是最小故障隔离。要只传播特定access fault需另立outcome/责任划分规则，不在本稿暗中改变。

plain scope将state=joined并令Λ(rs)=ended；owned scope先满足RELEASE，再同样joined并结束rs。F*=∅时必须且必然bodyOutcome=ok(v)，返回其Stable v；否则以非空Failure(F*)恢复外层raise。同一步丢弃已退出scope body的局部env／NameEnv，唯一移回ScopeExit保存的外部env/Θ并pop该frame，再继续余栈；外层region仍live。已持久化的S/Ω/故障身份不删除，不创建新aggregate fault。F历史不是跨已处理边界再次传播错误的理由。

`RELEASE`：只在 owned scope 已 closed、Req(s) 全 Safe、当前 O.epoch=γ、全部该 epoch issuance 已 seal、没有仍可执行的 Read/Write、全部 pins Safe 时，令 O.activeEpoch=none；保留已发行 history、Shared outcome 和 FaultMap。下一次 ACQUIRE 使用新 γ/g。空 run（未 produce、无访问）可按同一条件释放。

`DRAIN-UNKNOWN`：Req(s)含Unresolved时，S.state=unresolved，owned epoch quarantined，保持region/allocation/Task/backend/access或transaction roots。可以输出诊断，没有普通val/raise返回外层ScopeExit或RELEASE。**Unresolved是粘性class**：所有Safe规则仅接受Active的指定源状态，迟到真实完成只追加diagnostic evidence，不把Unresolved改回Active/Safe。独立安全owner仍可joined，依赖未知root的owner不能借此释放。未来恢复需另立显式规则与证明。

`WAIT-DRAIN`：还有 Active job/access/pin 且无上述 enabled join/completion 时，仅保留 drain 控制，不执行伪完成。一个 body 失败不得跳过仍活跃的兄弟。

## 7. 登记、关闭竞争与 commit 后启动

所有REGISTER有准备阶段和一个原子commit线性化点。guard/read、source身份发行及完整job/access/pin账本移交在commit同一步发生；真实Task的准备和撤销可以跨多个步，必须由内部Tx义务持有。source操作和close竞争的两个正常结果是：commit先行，close包含完整根；或close先行，注册不发行source job/access且准备Task实际零资源访问地收束。未知准备结果是第三种诊断状态，不能假装属于已经安全回滚的第二种结果。

`REG-PREPARE`：经初步身份检查，fresh ζ，Ω.Registration Active；记录当前issuer以及责任anchor。若当前执行的是scope body，加入该S.localRuntimeRoots；若是registered job直接代码，添加Job(p)→Registration(ζ)。隐式main根也维护此集合。Tx只保存验证过的capturedEnv与目标／源tags，不给候选Task任何Read/Write或资源入口许可；父操作进入registrationWait，后继kont由Tx唯一持有。

`TASK-PREPARED`：可信runtime返回一个**实际存在**、只能在受控gate上等待的Task τ，放入Tx.provisionalTask；其资源程序尚未开始。它持有Copy捕获和未激活的启动描述，不能凭描述符自行访问H；Read/Write在commit时才移交。分配一个τ名称不满足本规则。runtime同时支持以下两种失败回报：

- 明确的`notCreated`证明：没有实际Task可运行，也没有悬挂callback，Tx可安全回滚；此路径不产生join事件。
- `creationUnknown(token)`：无法确认是否存在Task，保留token、永久不放行的gate和capture根，Ω.Registration→Unresolved，Tx.phase=unknown、保存非空diagnosticFailure；不提交source job/access。此步本身不复制或删除parent kont；caller仍在registrationWait。可由下述REG-UNKNOWN-RETURN把诊断交回caller，但anchor最终仍不能完成。source owner尚无pin、候选Task尚无访问权，因此不能伪造“X有未知访问”；未知的是内部Task义务。

`REG-UNKNOWN-RETURN`：仅对creationUnknown的诊断返回路径、Tx.kont尚在且caller确实阻塞于该registrationWait时，唯一移动其GenBind/env/Θ/原余栈回caller并令Tx.kont=none，恢复为raise(Tx.diagnosticFailure)。GenBind失败不安装p/h/k；后续按UNWIND/Try处理，原ScopeExit完整保留。Tx继续持有不可执行用户代码的未决Task/gate/capture根及Unresolved身份；封套永久禁启，源Scope入口被撤销为不可调用的身份／保活记录，不是可供后续求值的源env。此处不能删除实际Task仍可能引用的内存。返回诊断不表示安全回滚。若kont还没有移回，caller没有活动后继，不能到JobFinish或RETIRE。PRECOMMIT-ROLLBACK本身的完成未知仍按原规则保留阻塞，不借此新规则伪造完成。

`PRECOMMIT-ROLLBACK`：commit guard失败或准备中确定错误时，永久禁止resource entry。若实际Task存在，给它发出明确的abort-only gate结果，使其执行不含用户code／资源访问的退场；只有**实际terminal和join观察**到达后Tx才Safe并向caller抛准入错误。若已有notCreated证明，直接按该证明Safe。安全返回唯一恢复父GenBind/env/Θ/余栈、Tx.kont=none，清除已不被实际Task使用的准备源封套，失败不安装新name/handle。若abort/terminal是否完成未知，Tx Unresolved，持有根，不返回“已完成回滚”诊断。历史ζ/τ和证据不删除；“零访问拒绝”不等于从未创建内部Task。

`REG-COMMIT`：仅Tx.prepared且实际Task仍被resource gate封住，重新原子检查所有target/source gates与epoch，然后按下面相应登记规则创建完整source记录。Task根、获准环境和能力移交J/Ω；J的env/stack为空、Θ=none、控制ready(p)、startGate=closed、startupObservation=unreported，startEnvelope唯一持有待START的code/capture及新能力。清Tx中的已移交封套／源capture，历史仅留身份与ownershipTransferred证据，Tx记Safe；h/k经父GenBind恢复后继并清Tx.kont。此后不得PRECOMMIT-ROLLBACK；START是另一步，所以close可以在commit与START之间发生且必须保留此job。若实现不能证明这些Task-factory/gate契约，不能宣称精化了此核心。

### 7.1 普通任务与 producer

`REG-JOB`是REG-COMMIT普通分支：目标S.open，捕获／品牌tags匹配；fresh p/rJ/ιJ，Λ(rJ)=live、parent=rs，绑定Tx实际τ，加入J(ready,appStarted=false,sourceRetired=false,preparedOutcome=none,publishedOutcome=none,terminal=false,cancel=false)、S.localJobRoots、Ω.Job Active与R Task根，并保存child latent effect和job/body region关系。消费父GenBind kont交付h/k并清Tx.kont；资源程序不能在commit前开始。

`REG-PRODUCER`额外要求owned scope当前γ、stage=fresh、producer=none、参数/签名正确。commit同时发行p/rP及live job region、唯一Write、reserved写access αW、dormant cleanup αC/rC及Ω.Job/Access和根；Λ(rC)=dormant、parent=rP，stage=writing、slot=p。Write及未执行cleanup封套暂由J.startEnvelope唯一持有，未复制给父或cleanup；两个access尚未started，sourceRetired=false。第二个produce、旧代s、关闭s使commit失败，按PRECOMMIT-ROLLBACK收束prepared Task，零资源访问。

`START-ORDINARY`：完整登记、sourceRetired=false、实际startGate=closed的ready job，把源捕获从startEnvelope移入env，以ΘJ执行code，源栈为`[JobFinish(p,ordinary,rJ,rJ,RuntimeExit(p))]`；同一线性化步令startGate=released、appStarted=true，源程序不能先越gate再补环境／账本。`START-PRODUCER`在同样guard下把αW置started/enabled，将唯一Write及kernel参数移入受ΣK检查的kernel配置，当前region=rP；封套中仅保留尚未运行的cleanup代码／捕获，αC/rC仍dormant。`START-READER`按下一节激活α并交付Read。父scope已closed不禁止合法START；sourceRetired、terminal或实际gate=abortOnly则禁止重新START。

**commit后确定未启动的失败**分三个控制步骤：`START-DISABLE`仅可对实际closed gate原子成功，令gate=abortOnly、c=abortGate(p,F)，保存原startup Failure与全部封套，appStarted仍false；它与START竞争，已released不能假装禁启成功。`ABORT-ENTER`须实际τ接收该abort-only结果并进入不含源code的退场，才c=abortReady(p,F)。此后以真实never-started／永久禁启证据对仍Active的reserved α执行ACCESS-ABORT，再走RETIRE never-started入口；已Unresolved的条目只添证据不改class。清除封套中的源closure/Write权限、dormant短region结束、切RuntimeExit(p)；producer无未知时stage=failed，有未知仍quarantined，reader不改其来源producer的成功结果。仍须PUBLISH、actual terminal及各渠道join。Task根本未创建只能走准备阶段notCreated，不能套用此路径。

**启动观察Unknown不是新的源控制点。** `STARTUP-UNKNOWN(p,κ,F?)`仅处理已commit且尚未sourceRetired的job。κ是可信runtime给同一实际启动观察的稳定身份，绑定p；重复交付沿用κ，它不是源程序可构造的权限。startupObservation.events保存κ、解析后的非空Failure及观察渠道：已有κ必须复用原Failure，不再创建原fault；同一κ携带相互矛盾的payload不满足本事件契约，不得覆盖旧记录。新κ若带原Failure则保留原ID；若没有原fault，才创建一次StartupObservationUnknown（origin=该job invocation，obligation=Job(p)）并绑定κ。令本次解析后的Failure为F：此前非unknown时保存unknown(F)，已有unknown(Fold)时保存unknown(Fold∪F)，按原fault ID去重，旧诊断不删除也不重编号；两种情形均追加观察渠道。Ω.Job若Active则置Unresolved，R继续保活。若producer则其epoch quarantined。它不改实际startGate、appStarted、源PC、env、stack、kont、封套或Λ，不仅凭启动观察未知推导某个command访问未知，也不撤销实际程序而漏掉其后续登记。具体配置与下一步为：

| 实际形态（startupObservation=unknown） | 权限／region所在与允许的后继 |
| --- | --- |
| closed、appStarted=false、ready | 唯一封套仍在J；rJob live，reader rα/cleanup rC dormant；没有活动Read或kernel执行。START仍可真实发生并唯一移交，因为观察Unknown不等于取消。也可真实START-DISABLE竞争成功；不能用unknown flag代替禁启。 |
| released、appStarted=true | 唯一源env/局部栈/外置kont或kernel配置仍按原规则typing；相应已激活region live，所有ISSUE及新子义务照常登记。若源代码已进入同步scope，ScopeExit不能丢；若阻塞，实际kont仍被唯一持有。Unknown不伪造停止，程序须真实到EXIT或满足kernel退出证据。 |
| abortOnly、appStarted=false | 封套仍在abortGate/abortReady，源code永久不可进入；只有实际ABORT-ENTER才可进never-started RETIRE。rJob及未退休短region保持，不能因观察诊断先清除封套。 |

`STARTUP-OBSERVE`需身份正确的实际证据：确认released则记ackStarted，确认abortOnly且永久never-started则记ackNoStart；若此前status=unknown，只把观察加events，保留同一累积unknown(F)及Ω.Job Unresolved，不替换或删减Failure。它不是重新分配或搬回权限的步骤。后续RETIRE须把该job启动unknown诊断并入prepared err；实际terminal/join也不能把Unresolved Job恢复Safe。若Unknown报告在sourceRetired之后才到达，`STARTUP-LATE-DIAGNOSTIC`只记录迟到观察事件：既有RETIRE已经有真实退出证据，迟到的“调用者不知道是否启动”不重写已冻结outcome、不重启region/源控制，也不伪造新的设备故障。实际访问完成Unknown仍由独立的OBSERVE-UNKNOWN契约处理。

### 7.2 Lease 和跨 scope 的登记式读取

`LEASE-WAIT`：源 sX/producer p 的静态关系经 runtime tag 再确认后，控制进入 leaseWait(sX,p)。它等待 p 的成功 **publication**，不是 terminal；需要 owner stage=readable。若 p 发布 err，则原样 rethrow；如果源 scope 先 close 或 γ 失配，则产生 AdmissionClosed/Stale fault，不生成 ℓ。guard 在实际发行步重新检查，避免 await 与登记之间的 TOCTOU。

`LEASE-ISSUE`：source gate open、epoch γ当前、p正是producer、publishedOutcome=ok且stage=readable时，freshℓ写L并返回Lease。复制/drop l不增减pin，没有close l的源权限。

LEASE-WAIT保存`GenBind((ℓ),(l),next,env,Θ,Ξ)`；成功发行仅查看p.publishedOutcome=ok并消费此kont，失败则恢复kont的原环境/Θ并raise，不创建ℓ。REG-COMMIT同样消费其GenBind恢复父continuation。preparedOutcome不满足LEASE-ISSUE前提。

`REG-READ` 同时要求：

```text
S(sY).open ∧ S(sX).open ∧ L(ℓ).open
L(ℓ).resource=(ρX,γX) ∧ O(ρX).current=γX ∧ stage=readable
target/source/captured type tags 与静态实例相同
```

一个commit绑定已prepared的实际τ，fresh p/ιJ/rJ/rα/α/η，Λ(rJ)=live、parent=rY，Λ(rα)=dormant、parent=rJ；记录job/body region关系、sourceRetired=false。加入目标S(sY).localJobRoots的job、J的reader body/Task根、A(α)的resource X与origin(ιJ,p,τ)、source owner的P(η)、Ω.Job/Access/Pin Active与R storage/task/access根。返回h/k并清parent Tx.kont后，START-READER才给出`Read(α,ρX,γX,rα)`。

资源身份与origin不互相替代；存sY/ιY→ιJ渠道及`(sX,ρX,γX,ℓ,α,η)`。source或target关闭使commit失败，由Tx实际abort/join prepared Task；不得只在Y登记job而未pin X，或只pin X却让未登记Task访问。Ω.Pin的requires边同时指向α和pr整个Job，因而X的Req包含该child的终结与原失败；这不是把其registrationScope改成X。

`START-READER`：已commit、尚未退休且实际startGate=closed的ready reader，无论source scope gate为open/closed都可开始。同一步把源捕获移入env，加新Read，以ΘJ.region=rα执行body，源栈为`[JobFinish(p,reader,rα,rJ,RuntimeExit(p))]`，清相应封套；Λ(rα):dormant→live、A(α).reserved→started/enabled、startGate=released、appStarted=true，之后才允许资源程序执行。它不重新获取lease，也不因scope关闭撤销登记。每个load用同一α；新的REG-READ被拒绝，既有α内load不是新source-level access登记。

## 8. 后端 command、写后读与 job 的收尾

### 8.1 Backend transition contract

ΣK 的小步接口是一个有类型的外部 transition relation，不是源语言能够随意断言的证明对象。它只可提出以下带真实证据的事件；没有列出的事件不被接纳。

| 规则 | 前提／变化 |
| --- | --- |
| K-LOCAL | backend 仅修改自己的纯 descriptor/program-counter 状态，不修改 H/Ω；不能据此声称访问结束。 |
| ISSUE | α 属于当前尚未sourceRetired的producer/reader，源执行region live、authority enabled、started、未sealed；命令权限与resource epoch相符。fresh δ，先登记Ω.Command/α.commands/根，再真正提交；提交边界与descriptor创建分开。已登记descriptor的物理执行与迟到观察不以source region仍live为条件。 |
| ISSUE-REJECT | 有实际未提交证明；δ标记未提交且Safe。load同时冻结reply=err(原拒绝Failure)，保留待消费kont；若已登记δ，这个ID留在历史，不“当作没发生”。 |
| DEVICE-DONE | backend 获得 δ 的真实 device completion/status，写 D.deviceResult；不自动更新 host observation、不结束整个 α。CPU 同步原语可以把设备／实际调用返回的证据合并到一次具体 backend 事件，但必须声明其含义。 |
| OBSERVE-SAFE | 仅Ω.Command当前Active，主机取得身份正确、覆盖δ全部可能访问的完成证据，才写hostObservation并置Safe。若为load，此事件还须给出绑定δ的Word或非空原Failure并冻结reply=ok/err；只有安全状态却缺结果时先保留deviceResult，不能凭空造Word。该步不消费kont。已Safe的重复证据仅追加渠道；Unresolved时迟到同样证据仅追加diagnostic，均不重写reply/class。数值失败与访问安全分开。 |
| OBSERVE-UNKNOWN | δ可能已访问而完成证据未知；Active δ/α转Unresolved，A.authority=revoked、issuanceSealed=true，保持所有根。保留原backend fault；若仅“无法证明完成”而无原错误，生成明确UnresolvedEvidence诊断f，不冒充真实device error。unknownFailure为非空Failure；若为load，冻结reply=unknown(该Failure)，不消费kont。已Unresolved的重复证据只追加渠道，已Safe不被迟到的观察不确定性改写。 |
| SEAL | backend 将该 α 的后续提交永久关闭；后续 ISSUE guard 必失败。它不表示已提交命令完成。 |
| ACCESS-END | 仅Ω.Access Active且A.state=started，授权程序已结束、issuance已seal、所有δ Safe且authority已revoked，才α→ended/Safe。unknown不能使用本规则。 |
| ACCESS-ABORT | 仅Ω.Access Active且reserved，实际证明从未开始、没有可能访问且不能再启动，才seal/revoke并令α→abortedBeforeStart/Safe。没有日志或Unresolved不能当证明。 |

物理访问只允许发生在已登记且当前epoch的descriptor执行中。可信backend不能报告安全完成后继续用指针；这是外部契约假设。数学后置条件另行验证。Unresolved粘性是**当前核心尚未提供恢复规则的保守选择**，不表示现实设备永不能用迟到completion恢复；将来恢复属于完整路线，必须另给身份/闭包/终结证据、显式转移及精化证明。本稿的通用OBSERVE不能暗中实现恢复。

### 8.2 源执行退休与运行时退场

`RuntimeExit(p)=(p,J(p).τ,J(p).originInvocation,J(p).region,[])`只是运行时调度／身份上下文。空scopeStack表示已不执行源代码，不代表registrationScope或requires边消失，也不是可用于源typing的空权限证明。定义运行时退场typing：job region仍live、sourceRetired=true、env（含其NameEnv）/stack/startEnvelope均空、没有该job的源后继kont、所有实际同步子scope已joined；preparedOutcome的payload为Ψ.result指定的Stable类型或非空Failure，控制仅为publishReady/epilogue，Θ恰为RuntimeExit(p)。J/Ψ中的持久类型关系、结果自带的存在witness及合法Stable函数值自己的捕获环境仍保留；删除J的源env引用不是原地擦除结果所需的共享不可变数据。将来别的job可调用合法Stable函数并发行自己的工作，它不恢复原job的源执行或权限。

`RETIRE(p,o,entry)`是以下EXIT家族共用的一次原子转移，不是源语言命令，也不是随时可调用的取消操作。其前提逐项为：

1. `sourceRetired=false ∧ terminal=false ∧ Λ(rJob)=live`，源执行者已不能再发出新的源操作。常规entry必须是`val(v)`或`raise(F)`到达**唯一**`JobFinish(p,role,rSource,rJob,RuntimeExit(p))`；所有Bind/Try/CallReturn/Restore/Loop/ScopeExit已按各自规则处理，不能由本规则删除来满足前提。
2. 无该job留在W、Tx、loadWait、leaseWait中的未消费源kont，也没有尚Active的直接Registration；所有`J.synchronousChildren`及此job实际同步进入的递归子scope已joined。若registrationWait的kont仍在Tx，或某个ScopeExit处于drain/unresolved，本规则不enabled。清空Θ.scopeStack不是绕过这些条件的方法。
3. kernel-failure entry要求可信backend确认**发出命令的host/kernel控制程序已经退出且不能再恢复**；never-started entry要求实际Task已进入abort-only控制、源入口永久禁启及从未执行的证据。这两类没有用户源栈的入口不要求伪造JobFinish，但仍要求第2项。device completion、取消请求、没有日志或unknown token均不足以证明host控制已经退出。
4. 每个本job自己的access已seal/revoke，且或者按ACCESS-END/ABORT成为Safe，或者明确Unresolved并保留非空unknownFailure；没有以仍Active/Pending的访问跳进publication的路径。safe command failure与访问安全可以同时成立。未激活的封套只在真实never-started／永久禁启证据下销毁其源权限；不能删除可能仍被实际Task使用的内存。
5. 从当前未处理结果、**本job直接拥有的**Unresolved Access/Registration诊断及本job.startupObservation=unknown中的累积Failure得到o：诊断非空或源结果err则o=err(并集)，否则o=ok(v)。不遍历安全Req来复活已被catch处理的嵌套scope故障；未joined嵌套scope已由第2项阻断。ordinary允许尚未清偿的直接Registration，但只在其kont已唯一归还后退休；Tx的未决Task根仍在。启动Unknown不会被源catch或迟到ack清除。

此步先移出o，再移除本job全部源env（含NameEnv）、唯一JobFinish或已停止的kernel配置，以及未执行startEnvelope中的源closure/Write权限；保留实际后端descriptor、storage、Task/gate根和Ω/Ψ/A/D/P/Tx/R历史。移除源值不等于释放这些物理根。对Ψ.bodyRegion记录的每个短源region（rB≠rJob），常规reader／cleanup入口把已完成的live rB置ended；kernel-failure及never-started入口凭第3项的相应禁启证据把dormant rB置ended。因此kernel失败也明确结束尚未运行的rC。ordinary与kernel控制本身使用rJob，不在此步结束rJob。最后令sourceRetired=true、preparedOutcome=o、Θ=RuntimeExit(p)、c=publishReady(p,o)，源env/stack/envelope为空。没有将unknown obligation改Safe的副作用。

欠缺host控制停止的真实证据时，RETIRE不enabled；仍保存其实际源／kernel配置或唯一kont、相关region与保活根，而非换成丢失源状态的opaque控制。实际代码尚可前进则继续按原规则登记后续工作；实际等待或不得服务则保留对应控制。以后得到真实停止证据只能满足退休前提，不能解除已经Unresolved的账本class。反之，源控制确已结束而某个已提交device command完成未知时，可以退休rα/rC并最终结束rJob，未知的物理访问仍靠A/D/P/Tx/R保活；物理descriptor无可恢复的源Read/Write值。这是执行region与设备／事务义务生命周期的明确区别。

### 8.3 Producer 的完整路径

`K-SUCCESS`：host K控制正常结束、αW Active/started且sealed、所有写command Safe、ΣK.post初始化证据成立，才撤销并移除kernel配置中的唯一Write、ACCESS-END αW。丢弃kernel局部控制，但保留descriptor历史与根；将startEnvelope中的cleanup closure唯一移入源env、清封套，Λ(rC):dormant→live，αC reserved→started/enabled，授予同producer Task的Read，以ΘC与`[JobFinish(p,cleanup,rC,rP,RuntimeExit(p))]`执行cR，owner.stage=cleanup（此前quarantined则保持）。它是源阶段切换，**不**置sourceRetired或结束rP；不发行额外lease，后者等待成功publication。

`K-FAIL-SAFE`：host K控制已确切退出、所有可能写入已确认结束，但K失败或未完成初始化；保存原fault（若仅post未成立且没有原fault，生成明确PostconditionFailed诊断），seal/revoke/ACCESS-END αW。αC从未放行，永久禁启并以明确never-started证据ACCESS-ABORT，不得读取未初始化存储。以kernel-failure entry执行RETIRE(err(F))，同时清除已停止的kernel源配置和未执行cleanup封套、令dormant rC ended，owner.stage=failed（有启动Unknown则仍quarantined）。之后须另经PUBLISH和actual terminal；这里不补造terminal。

`K-FAIL-UNKNOWN`：写完成未知则αW/相关δ Unresolved、seal/revoke，不能由此失败分支启动αC。若可信控制确已停止且cleanup从未放行，则独立记录αC的ACCESS-ABORT证据，以kernel-failure entry执行RETIRE，err至少含αW.unknownFailure、rC dormant→ended；这不清除αW未知。欠缺控制停止证据时，保留实际kernel配置和仍未移交的cleanup封套；不能复制已移交权限或把活跃控制伪装成未启动封套。已经实际K-SUCCESS切至cleanup的配置使用它自己的cleanup/unknown规则，不倒退套用未启动路径。尚未满足RETIRE前提时不publication；可发布诊断的路径仍需PUBLISH、actual terminal。owner始终quarantined，scope不能完成。

`CLEANUP-EXIT`：cR的正常Stable v或非空Failure到达唯一JobFinish，且满足RETIRE全部控制前提。若αC仍Active且所有command Safe，则seal/revoke并ACCESS-END；若Unresolved则保留并按RETIRE合并unknownFailure。随后原子RETIRE，清除含Read的源env／局部closure、结束rC、切RuntimeExit(p)。o=ok时stage=readable，o=err且无未知时stage=failed，有未知则quarantined；尚未写publishedOutcome。程序可以有条件／循环／等待；类型约束其访问，不保证程序终止或业务数学目标，也不因cancelBit跳过cR。

如果cR的load未知，αC已Unresolved且authority revoked，而rC在退休前仍live，同job局部env／closure可暂存opaque Read。catch可继续纯计算或使用独立owner；再次LOAD该tag在提交前抛AccessRevoked。只有正常控制真的到达CLEANUP-EXIT，才能移除这些值并结束rC；preparedOutcome至少含αC.unknownFailure。若catch进入一个无法joined的同步子scope，则不能退休、publication或terminal。源执行结束不等于底层未知访问结束。

### 8.4 Reader、ordinary job、publication 与真正终结

`LOAD-ISSUE`：a的α与Θ.job、owner/epoch、region一致，A.started/authority enabled/未sealed且Ω.Access Active，index<n。登记只读δ、reply=none/replyTaken=false及唯一caller identity后提交，把后继kont移入loadWait。scope close不影响合法旧α；revoked/越界/错权限/失配在提交前抛明确fault，零新访问。

命令完成与源结果消费是两个小步。`LOAD-RETURN`要求caller仍在匹配δ的loadWait、持唯一kont，D.replyTaken=false、Ω.Command(δ)=Safe且reply=ok(word)；同一步令replyTaken=true、消费kont、恢复其caller环境/Θ和后继Bind，控制为val(word)。不再要求δ Active，不重新结束δ，也不结束α。`LOAD-FAIL-SAFE`使用同样guard但reply=err(F)，恢复为raise原Failure。`LOAD-UNKNOWN`要求Ω.Command=Unresolved且reply=unknown(F)，同样只消费一次kont并raise F，α/δ及未知根继续Unresolved、authority revoked。重复完成callback或再次LOAD-RETURN均不能再取得该kont；迟到Safe观察也不能把已消费unknown改成第二次成功返回。type tag在同job源region仍live时可持有，后续ISSUE仍受动态guard。

`READER-EXIT`：val/raise到达唯一reader JobFinish且满足RETIRE全部控制前提；seal/revoke α，若α Active且全部command Safe则ACCESS-END，若Unresolved则保留并合并unknownFailure。原子RETIRE移出Stable outcome、清除含Read的源env/closure、结束rα、切仍live的rJ上的RuntimeExit(p)。source pin此时不退休，还等整个Task真实terminal被source finalizer join；unknown pin即使join也不Safe。

`ORDINARY-EXIT`：val/raise到达唯一ordinary JobFinish且满足RETIRE全部控制前提，原子移出Stable outcome、移除源env/closure并切RuntimeExit(p)。ordinary源控制使用rJ，本步保持rJ live；它没有reader式另一个短region。若直接Unresolved Registration的kont已通过REG-UNKNOWN-RETURN唯一归还，结果并入其诊断Failure，Tx未决Task/capture根仍保留；若kont未归还则不能EXIT。普通局部throw被catch后可成功，F历史保留，safe handled嵌套scope错误不复活。

`PUBLISH`：仅在满足RuntimeExit typing、publishReady(p,o)、J.preparedOutcome=o且J.publishedOutcome=none时，一次写入publishedOutcome=o，并对publication waiters开始仲裁，再进入epilogue。所有LEASE/CACHED-WAIT只读publishedOutcome，不看preparedOutcome。此步不令Task terminal、不使Ω.Job Safe、不退休pin。epilogue只有运行时退场动作，不能调用源closure、重新安装源kont、ISSUE或发行新子义务；必要的source finalizer必须已在RETIRE前执行。

`TASK-TERMINAL`：publishedOutcome已存在且满足RuntimeExit typing，实际runtime确认该Task的全部控制栈、运行时epilogue与必要finalizer已经退出，才terminal=true、Λ(rJob)=ended、Θ=none、c=terminated(p)，env/stack/envelope继续为空；对terminal-mode waiters交付同一个publishedOutcome。模拟状态不得拿“publishReady已无源权限”代替实际runtime事件。随后各JOIN-OBSERVE记录责任渠道。若访问unknown而Task已真实终结，只能让仍Active的Job按join记Safe，不能恢复Job自身或Access/Pin/Registration的Unresolved；结束rJob也不等于join，更不删除未清偿物理根。

## 9. 等待、提前取消、一次性仲裁与返回

`Control(p)` 只映射 J(p).cancelBit。`REQUEST-CANCEL` 把它单调设为 true；重复请求幂等。已 terminal 的 job 仍允许记录控制请求，但不修改其 outcome。这个规则不直接停止 code、不修改 A/P/D/Ω，不触发 scope body exit，也不自动取消子 job。

### 9.1 直接等待

`AWAIT-NEW`求出Shared target后分配terminal-mode w，把`Bind(x,next,EΓcaller,EΔcaller,Θcaller)::原stack`移动到W.kont，清空caller活动env/stack并控制为wait(w)。初始化handlerInstalled=false（此模式没有handler）、continuationInstalled=true、resumeIssued=false、returned=false。若target真实terminal且publishedOutcome存在，registration=cached、choice=delivered(publishedOutcome)；否则attached、choice=⊥。publication alone不交付，cancelBit对此模式无效。TASK-TERMINAL给undecided terminal waiters设置delivered；还需RESUME-ISSUE及RETURN-WAIT恢复caller，不能跳过continuation条件。

### 9.2 可取消等待

这组规则以 cell 线性化语义描述源码，未引入可被用户复制的 continuation 值：

1. **WAIT-NEW?** 分配publication-mode w，handlerInstalled=false、continuationInstalled=false、choice=⊥、resumeIssued=false、returned=false。如直接等待一样，把后继Bind/env/Θ/余栈唯一移入W.kont；caller进入wait(w)。仅cell创建尚未安装handler或continuation。
2. **HANDLER-INSTALL** 与当前caller取消标志同步注册handler，置handlerInstalled=true。若此刻已cancelled，必须先以同一安装操作兑现choice=cancelled，再允许后续continuation operation；包括“cell创建后、handler安装前取消”的窗口。若此刻未取消，之后的REQUEST-CANCEL使handler eligible，但与delivery竞争不预定winner。
3. **WAIT-INSTALL** 必须handlerInstalled且continuation未安装，置continuationInstalled=true。若choice已cancelled，registration=skipped；否则若target.publishedOutcome存在，registration=cached并原子claim delivered；否则attached到registry。它不读取preparedOutcome。此步不能越过已取消HANDLER-INSTALL所要求的先行cancel兑现。
4. **CANCEL-HANDLER** handlerInstalled、caller cancelBit=true、尚未returned时可发生。choice=⊥则claim cancelled，否则保留原choice并记lost；一次取消请求最多执行该cell的一个handler invocation。handler执行上下文不等于target waiter自身的采样。取消早于handler安装由第2项处理，不凭空漏掉。
5. **DELIVERY-CLAIM** target published后，对attached且choice=⊥者claim delivered(publishedOutcome)；cancelled则记delivery-lost。publication与逐cell claim之间可以插入cancel；publication先行不必胜。choice从不重写。
6. **REGISTRY-RETIRE** 对publication时存在的attached cells完成claim尝试后清registry；已取消cell此前仍可占entry，entry不是active waiter或资源义务。
7. **RESUME-ISSUE** continuationInstalled=true、choice≠⊥、resumeIssued=false，置resumeIssued=true并向caller发出一次可执行返回动作。实现须在hub/cell锁外真正resume；抽象不把调用后的日志排在caller返回之前。对terminal-mode使用同一规则。
8. **RETURN-WAIT** caller获得服务且resumeIssued、未returned、kont存在时，先将kont唯一移动回caller、W.kont=none、returned=true，并注销可取消handler。delivered(ok(v))在await?形成val(inl v)，在await形成val(v)；delivered(err(Failure))形成raise原Failure；cancelled仅形成val(inr Unit)。这些控制继续通过保存的Bind及余栈，成功会bind x执行next，异常按UNWIND寻找Try/ScopeExit。后来的取消不改choice或已经交付的值。

返回后保留必要的 cell 历史供证明，允许实现 GC 已无观察需要的记录，但 GC 不属于本稿核心语义；尤其不能借 GC 丢掉 Active/Unresolved resource root。

取消 handler 的安装、锁竞争、resume 调用和 caller 获得 CPU 服务都是实际实现动作。本抽象规则没有给它们时间上界，也没有把 `resumeIssued` 当成 caller 已经返回。C1 实验已经说明这两点必须分开。

## 10. 可 typing 的 origin=Y / resource=X 例子

以下省略无关的 existential pack；X/Y 是外部 Stable Owner handle，`sum` 为只读多遍计算函数。每个 K 成功后的 cleanup 仍由其原 producer 执行。

```text
run X as sx {
  produce sx Kx(dataX) cleanup (rx ⇒ sum(rx)) as (px,hx,kx) in
  lease sx hx as (lx,lX) in
  run Y as sy {
    produce sy Ky(dataY) cleanup (ry ⇒ sum(ry)) as (py,hy,ky) in
    fork sy read lX as aX {
      let first ← load aX 0 in
      let yCopy ← await hy in
      return (first,yCopy)
    } as (pr,hr,kr) in
    return ()
  }
}
```

`lX:Lease(sx,ρX,γX,lx,rX)`；登记目标为 `sy:Scope(sy,rY,owned(ρY,γY))`。reader 的其他捕获只有 Stable hy；其 aX 类型是 `Read(αr,ρX,γX,rr)`，αr 的授权 job 是 pr，origin invocation 是 sy 的 body ιY 创建的 ιr。返回 `(Word,YCopied)` Stable。没有把 lX 本身交给 child，没有把 Y 的 owner brand 当成 X 的存储 brand。

登记事务在 S(sy).roots 加 pr/τr，在 O(ρX) 的 pin 集加 ηr，并记录 `origin=(ιY→ιr,pr,τr)` 与 `resource=(sx,ρX,γX,lx,αr,ηr)`。sy body 随即返回，所以 Y 关闭新登记并等待 py 与 pr；pr 的真实 X 读取结束后等待 hy，可继续保留 X pin。若 py 失败，await hy 重抛 py 原 fault，pr 失败和 sy 聚合保留同一个 ID 的不同 reportedVia。

这个例子具体给出了双身份 typing。它不声称复现 C0 的每一个动态阶段：此同步嵌套写法中 sx 的 body 要等内层 run Y 返回才退出。另一个同源 scope 的示例 `fork sx read lX { multiPass(a) }; return ()` 才直接展示 sx close 后旧 reader 继续工作。若要在源前端表达更多跨 scope 并行借用，需要满足明确的 scope outlive／capture 规则，不能用这个例子偷偷宣称任意动态借用都已可表达。

whole-child failure政策还有额外代价：把reader body改为`load aX 0; throw error("unrelated-after-read",(),empty)`，并让X body catch整个run Y的错误后正常返回；即使所有X读取早已安全完成，X仍从其foreign pin所依赖的pr.err收集同一f，最终报告失败。这样无需在一个child内部再推断哪些业务错误“属于”X，但隔离性较粗。**C0只证明借读实际抛错时X/Y保留同一fault，没有证明此额外政策是唯一或必要选择。** 将来的access-linked fault投影可只汇入真正关联α的错误，但必须明确关联规则，同时保留整个Task join的资源义务，不能为了错误隔离提前释放pin。

持久接口的合法逃逸／恢复例子（NEW的N=0分支仍可Raise）：

```text
let pkg ← new_owner inputN as (n̂,ρ,o) in
  return pack[(n,ρ0):=(n̂,ρ)] o
    as ∃(n:Size,ρ0:OwnerName; n>0,owner(ρ0,n)). Owner(ρ0,n)
in unpack ((m,σ; m>0,owner(σ,m)),outsideOwner) = pkg in
  run outsideOwner as s { return () }
```

unpack得到的是既有owner的brand witness与owner(σ,m)，不是重新分配资源。相反，拥有任意Shared(p,T)与Scope(s,owned(ρ,γ))不能打包声称`producerOf(p,s,ρ,γ)`；PACK需要在Ψ证明此持久事实，仅T相同或owner的尺寸相同不够。这样不能借existential伪造lease的producer所属关系。

合法／拒绝控制包括：复制 Control 后两次请求合法；对 Shared 调 request_cancel 类型错误。复制 Lease 后两次 REG-READ 合法；把 Read 隐藏在普通 fork 的 closure 捕获被拒绝。返回 Shared 的存在 package 合法；返回 Lease／捕获它的 closure 被 Stable 拒绝。scope close 后新的注册动态失败，但已 commit 的 reader继续 load 合法。重复等待旧 Shared 不校验 owner 当前 epoch，因为它没有 live storage authority。

## 11. 可阻塞、循环依赖与没有宣称的进展

本稿的配置 typing 不包含“依赖图无环”。例如开放程序接收 `h:Shared(p,T)` 并 await h，是良类型的；其宿主配置可以有 p 等待另一个 q，而 q 的外部调用最终依赖当前 scope 返回。只要各 handle、region 和账本成立，这个循环不被类型规则排除。源码没有一般递归和 mutable promise cell，因此并非所有循环配置都能从空的 closed main 构造；**是否能从这一受限封闭源程序排除某类循环，另需可达性定理，本稿没有证明，也不把配置 typing 偷换为该定理。**

即使所有源码 for 都有界、没有可达等待环，也有良类型不终止执行：backend 永远 Pending；某个 enabled Task 永远得不到调度；一个等待者收到 cancel 后一直得不到 handler/返回服务；unknown 使 drain 永久保留根。`run` 的 busy-拒绝避免一种同 owner 自等待，不推出一般死锁自由。

响应成本图至少含实际 command completion、host observation、必要 read cleanup、Task terminal、join observation、pin retire、owner release、B admission/service。await? detach 只缩短本 waiter 的等待关系；它不删除 owner 的依赖路径。在额外证明无环、所有原语终止、无 unknown 且每个节点有成本 C(v) 和服务延迟上界 S(v) 后，才可使用 `E(v) ≤ max E(pred(v)) + S(v)+C(v)`。这些条件均非本类型系统已给的结论。

PriML 方向仍需单独给 priority 索引、允许的 Need 边、需求传播和异构执行服务假设。保留 p 身份不等于 Swift runtime 提优；公平不等于墙钟界；CPU worker 与 GPU lane 不是可相加的同质处理器。

## 12. 证明目标、所需引理与逐转移义务

以下是待完成的证明计划，不使用“已证明”措辞。证明量化任意良类型初始配置和任意有限执行前缀，名字数量不预先固定。

### 12.1 必需的元理论引理

1. **名称替换／存在打开**：fresh brand 的 α-renaming 保留 owner/epoch/job/access 关系，隐藏 brand 不隐藏 live 支持；runtime tag 实例化保持类型。
2. **Copy 收缩／affine 分离**：Γ 的弱化收缩合法；Δ 的移动、pair、闭包捕获、参数传递和分支汇合保持唯一写权限。frame 不被执行体隐式使用。
3. **值／命令替换**：按 bind 对 Copy／affine 值替换，支持残余 Δ 以及正常／异常出口；closure latent effect/capture 与实际 env 一致。
4. **region支持、闭包逃逸与源退休**：Stable值无live支持不直接推出整个配置安全。须证明到唯一JobFinish／可信host控制结束时，所有源env、局部closure、NameEnv和外置kont能按RETIRE条件唯一移出或删除，结果witness仍可解释；RuntimeExit不含可恢复源权限。Λ的父子寿命、scope joined、短源region退休和actual Task terminal各自保持，物理未知根不依赖已退休源值而继续保活。
5. **登记原子性／启动隔离**：fresh job/α/η/Ω/根同时commit；未commit无访问；post-commit失败不能丢记录。creationUnknown诊断返回唯一归还父kont，与永久禁启封套／未决Task根分开；never-started和host控制停止证据不能由timeout推断。实际START的唯一环境/region移交先于资源程序，观察Unknown叠加但不重写实际控制；closed分支后来的START与真实disable竞争都须保持typing。实现refinement必须另外证明多锁／失败路径及runtime capture保活对应。
6. **backend confinement／completion soundness**：所有物理读写由已登记 descriptor 产生；safe observation 的确覆盖全部可能访问；seal 后不再提交。它们是可信 backend 假设，不能从源码类型 alone 推出。
7. **必要工作闭包**：S.close 的根集合虽固定，但 Req 动态闭包包含以后产生的 command／同步嵌套工作；任务不能在这些内部 scope 未退出时 terminal。
8. **fault identity 与渠道**：throw fresh，rethrow/await/scope union 不 fresh；同一原 ID 的多条 reportedVia 不合并掉其他原错误；catch 不删除 F 历史。
9. **wait单调机**：choice选择、handlerInstalled/continuationInstalled/resumeIssued/returned的一次性、kont唯一移交，以及提前取消/cached delivery覆盖；无需假设resume_returned日志先于caller返回。
10. **configuration extension／历史保留**：fresh map extension、Safe/Unresolved 状态更新、必要强根和不可变 outcome 的保存保持 wf；任何 GC 扩展需另证明。
11. **初态与完成消费**：INIT-MAIN建立host根和无自join的region/责任图，HOST-JOIN/RETURN不跳过scope；command Safe与load reply冻结后仍可进行唯一结果消费，消费不重复终结command，重复／迟到回调不恢复第二份kont。这两项仍需真实推导，不以本稿中的纸面最短路径代替。

### 12.2 候选不变量与目标命题

- **保持性目标**：`Σ⊢C wf ∧ C→C′` 且 backend 契约成立，推出 `Σ⊢C′ wf`（可能扩展 Ψdyn）。没有通常的“良类型必能前进”结论。
- **账本守恒目标**：每个前缀 Issued 恰为 Active ⊎ Safe ⊎ Unresolved；每个 Safe 附对应 kind 的真实证据；所有 Active/Unresolved 有 owner。wait/drop/cancel 没有减少这三个集合之和。
- **epoch 安全目标**：新 γ′ 的实际 writer ISSUE 前，旧 γ 所有可能访问与 pins 均 Safe；同一物理 allocation 没有跨代重叠访问。依赖封装、登记、完成soundness与RELEASE，不是单独的 affine theorem。
- **关闭目标**：S.closed 后不再新增该 scope 的 source-level roots/leases/access；此前 commit 的 job 和后续合法 command 都保留在 Req。已登记读取继续与“关闭后新登记禁止”并不冲突。
- **等待取消非干扰目标**：CANCEL-HANDLER/RETURN-WAIT 的本地 cancelled 分支不修改 target cancelBit 或任何资源义务；REQUEST-CANCEL 修改的 target 由显式 Control 决定。
- **故障与未知保留目标**：业务失败不能被“安全结束”改写为成功；unknown 不可由局部 task terminal、其他 owner 成功或异常聚合消除；同一 original fault 经多个渠道仍保持 origin。
- **源执行退场目标**：sourceRetired后没有原job的源env、可恢复kont或能恢复原job源执行／权限的closure；合法Stable函数结果不因此被删除，其他job以后调用它须用自己的Θ与权限。publication时job region仍live，actual terminal后无可调度Θ。rα/rC/rJob结束不授予访问Safe、pin退休或release；有未joined ScopeExit则源退休不可发生。这是新增待证明命题，不是把wf定义写出便已取得的结论。

### 12.3 逐转移的证明义务清单

| 转移家族 | 必须在保持性／不变量归纳中实际证明的事项 |
| --- | --- |
| LET/CALL/RETURN/IF/CASE/UNPACK/FOR/DROP | 替换与 Δ 移动；选择分支不执行另一分支；bounded iteration 不复制 affine capture；drop 不触及 Ω；返回的 live 支持与 frame 匹配。 |
| THROW/RETHROW/TRY/UNWIND | fault fresh 或保留；catch 的 affine frame 限制；unwind 不跳过 ScopeExit；丢掉环境不删除根；handled local fault 与 child terminal failure 的区别。 |
| NEW/ENTER/ACQUIRE/BUSY | fresh brands/regions与O唯一epoch；busy分支零发行；Owner Stable不暴露live pointer；空run可安全关闭。 |
| REG-JOB/REG-PRODUCER | 子环境类型与捕获策略；Task/Ω/强根先于START；单producer slot与唯一Write；cleanup dormant不能提前load。 |
| REG-PREPARE/TASK-PREPARED/UNKNOWN-RETURN/ROLLBACK/COMMIT | 真实gated Task与未创建证明分离；Tx唯一kont、capture/Task根；诊断返回只移回kont而不释放未知根；封套禁启／源权限撤销不删除实际引用内存；rollback实际abort/join；unknown粘性；commit移交后不能回滚删除job。 |
| LEASE-WAIT/LEASE-ISSUE | producer所属关系、publication成功和readable匹配；等待后重新guard；拒绝不泄露ℓ或原始存储。 |
| REG-READ/START/DISABLE/ABORT/启动观察 | X资源身份与Y origin分别保留；两scope原子guard；α/η/τ全或无；实际gate单次竞争和唯一环境移交；Unknown观察不伪停止／closed、不漏掉继续执行的源工作；实际term/join不解除Unresolved Job；迟到观察不重启已退休region。 |
| ISSUE/REJECT/DEVICE-DONE/OBSERVE/SEAL | 每δ唯一parent；当前epoch与权限；已提交集合守恒；device与host证据分离；seal不可逆；unknown保留。 |
| OBSERVE-SAFE/UNKNOWN/LOAD-RETURN/FAIL | reply的身份、类型及冻结；完成class与replyTaken分离；Safe之后可消费；只有匹配的唯一loadWait能恢复；重复/迟到回调不能产生第二次返回。 |
| K-SUCCESS/K-FAIL/CLEANUP/LOAD/READER-EXIT/ORDINARY-EXIT/RETIRE | 初始化前无Read；K-SUCCESS只转移封套并切源阶段；K-FAIL要求host控制停止，device unknown不能代替；source env/NameEnv/closure/Write删除与物理根保留分离；唯一JobFinish和全部同步scope已joined；短源region结束、rJob仍live；直接unknown诊断合并不复活handled嵌套fault。 |
| PUBLISH/TASK-TERMINAL/JOIN | RuntimeExit typing、sourceRetired与不可恢复源权限；outcome不可变；publish不能冒充term；真实terminal才结束job region／清Θ；term不能冒充owner join或访问安全；Ω.Job Safe不自动使α Safe。 |
| WAIT-NEW/INSTALL/CLAIM/RETIRE/RESUME/RETURN | 一次winner/一次resume/一次return；earlycancel可兑现；cached旧结果合法；registry不是资源计数；取消不修改生产者义务。 |
| BODY-EXIT/WAIT-DRAIN | gate关闭但既有工作继续；cancel位不等于bodyreturn；固定根动态Req闭包；一个fault不能跳过兄弟。 |
| PIN-RETIRE/SCOPE-FINALIZE/RELEASE | taskjoin与accesssafe合取；stable逃逸；foreign pin仍阻止X；fault union原ID；下一epoch前全部相关访问无可能继续。 |
| DRAIN-UNKNOWN | 保持region/root而不假装scope已返回；无恢复／release反向边；安全独立owner与依赖未知owner区别。 |
| INIT-MAIN/HOST-JOIN/HOST-RETURN | host非源Scope、main不在隐式scope roots、rHost→rMain→r0无环；implicit ScopeExit先joined；真实main terminal/host join分开；返回只消费一次Stable outcome，Unknown不伪收束。 |

这张表列出了每条操作规则的证明入口，尚未给出所有归纳推导。例如跨 scope region extension 与捕获替换、post-commit startup failure 的 runtime refinement、Req 的动态闭包、fault/cause 可达性都仍需正式推导。不能把“看起来每行都有检查”当作证明已经完成。

## 13. 下一步实现边界和验收要求

**AST／静态检查器**应完整实现第2节所有节点、Θ执行者参数、显式capture、带Φ关系的existential telescope/NameEnv解释、NEW size witness、Failure非空类型、Copy/Stable/Foot、region-outlive、Δ残余/运行时frame切分以及带ν的总体may-effect。ΣK的size schema实例化规则必须显式实现；不默认拥有任意依赖类型求解器。不能只做字符串扫描。正负例须包含pack→unpack→run、动态正size/零值失败、伪造producerOf拒绝、empty rethrow拒绝、affine let/call frame不复制、多读者、复制Control、lateShared、live closure逃逸、ordinary fork捕获Read、跨scope登记与故障聚合。

**参考解释器**实现上述无限新鲜名 machine 的有限执行前缀，并接受可脚本化的backend events；脚本是语义测试输入，不能称为真实GPU测量。输出Issued历史、全部state transitions、origin/resource双身份、Λ生命周期、sourceRetired/Θ/外置kont所有权、实际startGate与启动观察、load reply/replyTaken、真实抽象terminal/join与unknown根。必须包含reader/cleanup/ordinary的正常、throw、load-unknown、K失败、post-commit未启动、creationUnknown归还／未归还kont和嵌套scope阻断退休的正负路径；另测closed/released启动Unknown及迟到观测、OBSERVE-SAFE后单次load消费、main正常/throw/unknown三条host路径。scheduler可枚举有限交错用于找反例，但状态穷举不是类型健全性证明。必须支持开放参数配置与长期Pending，不把超时改成安全结束。动态解释器与完整AST／静态checker实现分开验收，不要求先完成所有元理论才开展已明确的静态实现。

**证据分层**依次为：规则规格审阅；parser/checker真实AST正负例；解释器按每规则的语义测试与独立账本核对；引理／归纳证明；Swift/CPU/Metal到抽象转移的精化；最后独立测接口开销和调度成本。原型可以逐步实现，但报告必须标明尚未实现的构造，不能用只有两个owner、一个固定trace的解释器声称完成整个语言。

本稿没有降低研究路线：真实 tiny-llm 的共享/取消/工具闭环、Apple执行安排、设备异步控制、真实设备故障、类型接口成本及响应性证明仍各自需要完成。核心语言目前统一的是授权、登记、依赖、访问完成与故障契约；哪些 backend 和应用策略能有效实现这些契约，仍由相应实验和精化证据决定。
