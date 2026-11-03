# 平台事实与决策备忘

2026-11-03。本文把 legacy 仓库里已经测到的平台事实写成结论，并就是否继续追求 PriML 式响应界给出备忘。每条事实的证据都来自 `legacy/`（即 /Users/fbsh/response-lab 的只读符号链接）下的报告或源码；所引文件的字节数与 SHA256 见 [PLATFORM_FACTS.evidence.json](PLATFORM_FACTS.evidence.json)。本文不新增任何运行，所有数字都是原报告的中位数或单次观察，样本数随行标注。

## 第一部分 平台事实

### F1 MLX 路径上取消的粒度等于分块

事实：在 MLX 路径上取消只能在 host 的分块 eval 调用边界生效，取消后必须等当前块的 `mx.eval` 返回。

证据。`legacy/experiments/tiny-cancel/run.py` 第 227 行 `mx.eval(last_token, *pool_arrays(model))` 是阻塞调用；第 164 行把取消触发定义为 "control thread awakened immediately before selected mx.eval call"；第 223 行把边界注为 "host enters mx.eval for constructed graph; not Metal commit"。`legacy/experiments/source-response-parent-host-close-v1/runtime/backend/worker.py` 第 179 行 `self.mx.eval(token, *arrays)`，第 391 行 `require(self.active is None, 'EVAL while a prior lease is live')`，一次只允许一个 lease。`legacy/experiments/swift-mlx-wait-owner-v1/PLAN.md` 第 31 行写明 "单 worker/单模型线程串行，控制/IO 不访问 MLX，一次一个模型命令"。`legacy/results/PILOT_REPORT.md` 受限配额表，n=3 中位数：

| prefill 块 | 取消到归还 ms |
| ---: | ---: |
| 32 | 46.683 |
| 128 | 147.884 |
| 512 | 670.265 |

含义。取消延迟由当前块的工作量决定，与宿主侧的责任机制无关。任何 "在途取消" 在 MLX 路径上都只能是块边界取消；细化粒度等于缩小块，而缩小块的代价在 F6 的成本分块矩阵里已经显现。

### F2 MLX 路径没有设备完成事件

事实：host 能观察到的 "完成" 是 `mx.eval` 返回加 `token.item()` 成功，不是 Metal 命令缓冲的完成回调。

证据。`legacy/experiments/tiny-cancel/run.py` 第 99 行把 device_completion_evidence 定义为 "host mx.eval return over token and all live pool arrays, not hardware timestamps"。`legacy/experiments/swift-mlx-wait-owner-v1/PLAN.md` 第 19 行 HostEvalReceipt 的定义是 "是 host 观察，非 Metal receipt/设备时间"。`legacy/results/core-language-runtime-mlx-cm1/REPORT.md` 第 58 行与 `legacy/results/core-language-runtime-mlx-fork-cm2/REPORT.md` 第 50 行都声明 `mx.eval` 返回只是主机完成观察。对照 Swift Metal 探针：`legacy/experiments/metal-handoff/Sources/MetalHandoff/MetalHandoff.swift` 第 371 行 addScheduledHandler、第 378 行 addCompletedHandler、第 204 行记录 gpuStartTime 与 gpuEndTime；`legacy/results/PILOT_REPORT.md` 的 "已经运行时的取消" 表中 metal-running-final 10 次全部确证为设备执行中取消，归还中位数 0.972 ms（constrained）与 1.184 ms（relaxed），n=5。

含义。真正的设备完成事件与在途取消证据只在 Swift Metal 数值探针里存在。在 LLM 负载上，实验合同里的 a.device_completed 永远由 host 的 eval 返回代替。要拿到设备完成事件，要么绕开 `mx.eval` 直接走 Metal，要么明确接受 host 完成观察作为上界。

### F3 物理内存从未释放

事实：所有 "资源归还" 都是逻辑页槽归还；每个 worker 归还后仍保留 56 MiB 池 backing，仓库里没有进程 RSS 或 MLX 活跃内存的测量。

证据。`legacy/experiments/source-response-parent-host-close-v1/runtime/Sources/RuntimeCore/Ledger.swift` 第 1467 行 resource.released 事件带 `"physical_free_claimed": false`。`legacy/results/core-language-runtime-mlx-cm1/REPORT.md` 第 58 行：最后归还后池数组 backing 仍为 56 MiB。`legacy/results/core-language-runtime-mlx-fork-cm2/REPORT.md` 第 27 行、`legacy/results/swift-mlx-wait-owner-smwo1/REPORT.md` 第 41 行、`legacy/results/joined-result-delivery-jrd1/REPORT.md` 第 51 行同样写明 56 MiB 保留；`legacy/results/return-service-priority-rsp2/REPORT.md` 第 104 行给出精确值 58720256 字节。在 legacy 全部 REPORT 中搜索 RSS，只有 decode-cost 系列提到，而且都是声明 "不是进程 RSS、MLX 总缓存"（例如 `legacy/results/batch5/decode-cost-eight-shape-synthesis-REPORT.md` 第 94 行）。未核：实验代码里没有任何 `mx.get_active_memory` 或 `get_peak_memory` 调用，getrusage 只出现在 T01 oracle 的副本里，与模型路径无关。

含义。方案证明的是页表账本守恒，不是物理内存可用性。在 16 GiB 机器上的多模型或长上下文情境里，这个差别会变成真实限制。若要宣称 B 能使用 A 释放的内存，必须补 MLX 活跃内存或 RSS 测量。

### F4 毫秒级 CPU 提优对秒级结构延迟没有意义

事实：对原 main 的优先级提升带来的归还窗口差异在 −2.5 到 +5.3 ms 之间且方向不稳定，而同一链路上的后端关闭耗时约 1.05 到 1.09 秒。

证据。`legacy/results/return-service-priority-rsp1/REPORT.md` 第 5 行：K8 的 unchanged−raise-main 差值 +5.287000 ms，K0 为 +0.109334 ms，每格 n=1。`legacy/results/return-service-priority-rsp2/REPORT.md` 第 3 与 33 行：四区组配对差值中位数 −1.405896 ms，最小 −2.51075 ms，最大 +3.914917 ms，1 正 3 负。`legacy/results/external-demand-ebd1/REPORT.md` 两张表，n=1：

| 条件 | 到达→main Stable ms | 到达→客户端 ms | backend close ms |
| --- | ---: | ---: | ---: |
| N0 | 66.854084 | 1156.746167 | 1089.804500 |
| P0 | 65.176250 | 1112.487292 | 1047.242792 |
| P8 | 76.834750 | 1126.911125 | 1049.988541 |
| N8 | 70.645959 | 1163.416500 | 1092.697958 |

`legacy/results/joined-result-delivery-jrd1/REPORT.md` 第 3 行：原策略 1106.116250 与 1101.833542 ms 交付，候选 65.205125 与 65.276833 ms，每策略 n=2。`legacy/results/source-response-contract-src1/REPORT.md` 时间表：B token receipt 52.785375 ms，完整源安全边界 65.855542 ms，客户端收到 65.895292 ms，后端关闭完成 1104.043625 ms，n=1。`legacy/results/backend-return-service-brs1/REPORT.md`：整段归还调用到 Owner 释放 12.726250 ms，n=1。

含义。这条链上的结构性延迟按量级排序是：后端关闭约 1 秒，prefill 约 50 ms，归还约 12 ms，提优效应几毫秒且正负不定。JRD1 消除了最大的一项，靠的是宿主交付策略而不是优先级或类型。在处理关闭成本与 prefill 之前，毫秒级的 CPU 调度优化没有可观察收益。

### F5 宽松配额不改变 B 的首 token

事实：放宽逻辑页配额让 B 几乎立即准入，但 B 的首 token 时间几乎不变。

证据。`legacy/results/PILOT_REPORT.md` 真实模型表，n=3 中位数：

| prefill 块 | B 准入 constrained / relaxed ms | B TTFT constrained / relaxed ms |
| ---: | ---: | ---: |
| 32 | 47.107 / 0.003 | 98.068 / 93.736 |
| 128 | 147.891 / 0.006 | 197.035 / 196.901 |
| 512 | 670.267 / 0.008 | 718.357 / 716.637 |

报告原文："提前准入没有让 B 的计算提前执行。"

含义。瓶颈是单模型线程的串行执行，不是页面归还。资源账本只能保证安全，不能缩短 B 的等待；缩短 B 的等待需要执行层的交错或抢占，这不在 P8 的责任模型里。

### F6 成本分块相对固定 32 没有统一收益

事实：cost-200 相对 fixed-32，八个形状里 A 的最长间隔没有一个形状多数更短，B TTFT 有六个形状多数更短；B 超过 200 ms 的预算超支大量出现在 A 完成后的尾段。

证据。`legacy/results/batch5/decode-cost-eight-shape-synthesis-REPORT.md` 比较表，每格是 cost 相对 fixed-32 更短／更长的配对数，每形状 6 对：

| 形状 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| A 最长间隔 | 1/5 | 0/6 | 1/5 | 0/6 | 1/5 | 1/5 | 3/3 | 3/3 |
| B TTFT | 6/0 | 4/2 | 5/1 | 5/1 | 3/3 | 3/3 | 6/0 | 5/1 |

同一报告的超支表，main 条件下 cost 的 B 超 200 ms 计数按共存／尾段分列：形状 3 为 5／446，形状 4 为 0／479，形状 7 为 28／498，形状 5 为 21／0。报告自己警告 "不能把八行汇成某策略获胜几次"；上面的 0/8 与 6/8 是本文对各行多数方向的读法，不是报告的结论。

含义。按成本预算分块是一个调度策略，它在 A 的尾延迟和 B 的首 token 之间做交换，不是免费改进。响应性的核心变量在执行层的分块与交错策略，而不在宿主侧的义务账本。

### F7 已有的取消实验都是早取消

事实：CM2 的 N 与 D、BRS1、SMWO1、JRD1、RSP2 覆盖的都是 W 在 A 求值开始前或完成后取消，没有一次是 A 求值进行中被取消。

证据。`legacy/results/core-language-runtime-mlx-fork-cm2/REPORT.md` 第 31 行："N 本次是提前取消……D 验证的是已挂起等待的功能次序，A 的求值在取消前已经完成，不是 GPU 在途取消实验。" `legacy/results/backend-return-service-brs1/REPORT.md` 第 11 行：W 返回早于 A 的 EVAL 登记 0.714084 ms，"未覆盖 A 已在求值期间取消已登记 W"。`legacy/results/swift-mlx-wait-owner-smwo1/REPORT.md` 第 60 行：D 中取消前已取得真实求值 receipt。`legacy/results/joined-result-delivery-jrd1/REPORT.md` 第 51 行与 `legacy/results/return-service-priority-rsp2/REPORT.md` 第 104 行："仍只覆盖自然早取消，不覆盖 GPU 在途取消。"

含义。P8 链上所有 "取消不影响归还" 的证据都在取消与求值不重叠的相位取得。结合 F1，MLX 路径上可做的在途取消也只是块边界取消。核心情境 "取消正在占用 KV 的 A" 在真实模型上尚无数据。

### F8 真实模型的工具闭环是空结果

事实：P7 model-effects 的 M0、M1、M2 三次真实运行都在提案阶段之前失败，没有任何效果提交被观察。

证据。`legacy/results/batch5/model-effects-REPORT.md` 第 3 行：三次首次模型响应都是 `{"final":"brief summary"}`，parser 识别为 FinalAction，准入条件拒绝非 ToolAction，三次进程退出 1。第 39 行：system prompt 里含有同样的示例输出，原因尚未区分。

含义。Agent 情境下的效果与 epoch 机制至今没有真实模型证据。P8 的工具后继（SRC1 B）建立在一个尚未在真实模型上成立的应用情境上。

## 第二部分 决策备忘：是否继续追求 PriML 式响应界

### PriML 的目标

PriML 系列用优先级类型保证高优先级线程不会等待低优先级线程，再以成本语义和调度定理推出响应时间界。`/Users/fbsh/swift/RESEARCH_DIRECTION.md` 第 91 到 92 行已经写明两条边界：长 prefill 首先是资源竞争与调度粒度问题，只有引入依赖与优先级关系才适合按优先级反转分析；固定 token 预算不能直接变成墙钟上界，PriML 的成本论证依赖明确的调度假设。

### 本项目的现状

- 没有优先级索引、调度模型或成本语义。`legacy/docs/P8_LANGUAGE_SUBSET_PROPOSAL.md` 第 80 行："Need(p) 描述语义依赖，不承诺某个 Swift continuation 会向 p 传播 priority。" 第 204 行："若要 PriML 风格结论，应另给 priority 索引、允许等待规则、需求传播语义及对应调度假设。"
- WRP1 的全部进展命题以 eventual-service 前提为条件。`legacy/experiments/wait-responsibility-proof-v1/CONDITIONAL-PROGRESS.md` 第 15 到 20 行列出 S-install 与 S-cancel 等前提，第 44 行声明 "不从类型正确、Stable 或公平调度推导目标一定完成"。
- 有限协议模型给出的是安全而非进展。`legacy/results/batch5/protocol-model-REPORT.md` 第 3 行：836 个可达状态、2704 条转移，"安全协议仍允许在缺少服务假设时无限等待"；第 48 行："公平性可排除永久饥饿，却不单独给出统一等待上界。"
- 平台事实 F1、F2、F4、F5 表明：取消粒度是块，完成只有 host 观察，瓶颈是单线程执行，提优效应在噪声之内。

### 选项

**A 放弃响应界，只做安全交付。** 代价是标题与研究方向中的 PriML 动机要改写成 "责任与交付" 研究，已有的 Need 与 WaitDetach 效果只保留为依赖记录，不再承诺定理。不需要新实验，需要一份明确的范围声明。

**B 保留响应性目标，但降级为经验界。** 上界写成当前块剩余工作、归还路径、交付路径三项之和，每项由测量给出。代价是要为每个端点维护组成项、样本数和环境的账本，每个新策略都要重测。需要的证据：JRD1 类型的对照以 n 不少于 5 重复；prefill 块大小与关闭成本的分解表；明确哪些项是平台常数。

**C 完整引入优先级与调度模型。** 需要优先级索引进入类型、允许等待规则、需求传播语义，以及一个覆盖单模型线程加 Swift executor 的调度假设。同时需要执行层能响应优先级，也就是块级交错或抢占，而 F5 表明现有 worker 做不到。需要的证据：先做执行层交错实验，证明优先级能改变 B 的首 token，之后再谈类型。

### 推荐

推荐 B，并把 C 的第一步作为可选的独立实验。F4 与 F5 说明当前的杠杆在执行层和宿主交付策略，类型层的优先级在这个平台上没有可观察效应；A 会丢掉研究方向里 "同时考察吞吐和响应" 的问题意识。B 让已有测量继续有价值，并给出一个诚实的响应性陈述：上界等于当前块剩余工作加归还加交付路径，各项由测量给出。

最终决定由 root 作出。
