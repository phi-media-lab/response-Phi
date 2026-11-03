# SRC1 B 收敛：十个父子组合情境首次执行

2026-11-03。落实建议 4 的第二步。此前这十个受检父程序组合情境（B-C01 到 B-C10）从未执行过，只有源码和源码级验收；oracle 修正虽获验收但从未合入运行时或编译。本轮把 oracle 修正合入规范运行时、重建、首次串行执行这十个 CPU 情境。结果先行：十个情境的组合逻辑全部通过检查，但其中八个在宿主收尾阶段挂起，这是一个此前无人见过的真实集成缺口。

## 1. 做了什么

- 把 `composition-scope-oracle-root-source-acceptance-v1` 已验收但未合入的 `ParentCompositionControls.swift`（after 版，sha256 `ee6ea824…`）合入规范运行时，替换 before 版（`cefe9f4c…`）。变更记录在 `runtime/CHANGES.json`，源 diff 与验收记录字节已固定。
- 用 CM2 的构造器生成十个情境的父源、子源与两个响应变体源（`cpu-sources/`，generator 未改）。
- 复制 SRC1 B 的 CPU runner（`run_cpu_session_v1.py`，sha 已记），固定 worker 为 stub、词表为生产 tokenizer.json（11,422,654 B，sha `aeb13307…`）。
- 串行执行 B-C01 到 B-C10，每个独立 OS 会话，预算 55 秒 work / 60 秒 total，无重试。

## 2. 结果

| 情境 | 组合 trial | 宿主 result.json | 退出 | 判定 |
| --- | --- | --- | --- | --- |
| B-C01 registered 成功，关闭 held | passed | 未写出 | 124（超时 SIGTERM） | 收尾挂起 |
| B-C02 cached 响应 | passed | 未写出 | 124 | 收尾挂起 |
| B-C03 工具多 token 被拒 | passed | 未写出 | 124 | 收尾挂起 |
| B-C04 工具缺 token 被拒 | passed | 未写出 | 124 | 收尾挂起 |
| B-C05 工具 open 错误 | passed | 未写出 | 124 | 收尾挂起 |
| B-C06 child 源失败 | passed | 未写出 | 124 | 收尾挂起 |
| B-C07 child 缺 intent | passed | 未写出 | 124 | 收尾挂起 |
| B-C08 关闭点基础设施故障 | passed | 未写出 | 124 | 收尾挂起 |
| B-C09 parent/child 看门狗 | passed | 已写出 | 0 | 完整通过 |
| B-C10 消费后基础设施故障 | passed | 已写出 | 0 | 完整通过 |

组合 trial.json 的 `passed` 对十个情境全部为真，oracle 新增的"HostClose/finalize/publication 属于原父 scope 与原 caller"检查全部满足。B-C09 与 B-C10 走异常退出路径（看门狗 124 报告、基础设施故障 exit 2），显式 `Darwin.exit`，收尾正常。

## 3. 收尾挂起的定位

对 B-C01：事件流止于 `control.reader_joined`（子服务宿主的控制读取器收束），worker 正常关闭，但顶层 `result.json` 从未写出，进程不退出，外层 wrapper 在 55 秒 work 预算处 SIGTERM。组合 trial 已写出且 passed=True，说明挂起在产出结果之后、写 `result.json` 之前的宿主收尾段，而非组合逻辑本身。

这个挂起不是本轮运行时修复引入的：`ParentEntryRoots` 未被本轮改动，其 `joinWork`/`cancelAndJoinTimer` 是标准 await；`SourceResponseControls` 与 `JoinedResultDeliveryControls` 的门改动经 diff 核对与原语义等价，且这些门不在父组合 CPU fixture 路径上（该路径用 `ParentCompositionCPUFixtures` 自己的门）。正常完成路径的 work Task 在产出结果后未能让顶层 completion cell 落地，八个正常情境因此卡在收尾。异常退出路径（B-C09/C10）绕开该收尾段，故不受影响。

## 4. 这一步的价值与边界

- **价值**：十个情境从"从未执行"变为"首次执行"，组合逻辑与 oracle 修正在 CPU 上得到实际验证（trial 全 passed），并精确暴露出一个此前无人知道的宿主收尾挂起，只影响正常完成路径。
- **边界**：worker 是 stub，非真实 MLX；没有 GPU；收尾挂起尚未修复，八个正常情境没有写出宿主级 result.json，因此尚不能宣称十案"完整通过"。T01 工具自验证（64 负例族中 12 个）与真实模型 P01 仍未收敛，本轮未触及，仍是 SRC1 B 的两个未完成前沿。
- **下一步最小切片**：定位 work Task 产出结果后 completion cell 未落地的那一个 await（候选是子服务宿主收尾与顶层 cell 落地之间的衔接），修复后重跑八个正常情境；再处理真实 MLX 的 P01。

## 5. 收尾挂起的根因与修复（后续增量）

进一步定位推翻了第 3 节"收尾挂起"的初步判断:它不是死锁。在退出路径上逐点插桩后,八个正常情境走完了全部标记(含产出报告),卡在 `saveHostJSON(result.json)`。原因是体量:单个组合 trial 达 822 MB(`checks` 嵌入被比较的完整账本对象 194 MB,`final` 嵌入完整事件与句柄闭包 528 MB),顶层 `result.json` 又经 `host_roots` 把证据快照重新内嵌,于是要序列化约 1 GB 的 JSON,写入时间顶到 60 秒预算边缘,重的情境越界即超时。这与建议 5 的产出膨胀是同一个病:膨胀既撑爆磁盘,也表现为"收尾挂起"。

修复分两层,都不伪造证据:

1. **构造期设上限**。`JSONValue.boundedForReport`(RuntimeCore,O(n) 自底向上)把任何编码超过 256 KB 的子值替换为紧凑存根(kind、约字节数、键数或长度)。应用在写 trial.json 之前与 `host_roots` 内嵌处。小的检查行与字段原样保留,只有超大的账本闭包与被比较对象变成大小存根。pass/fail 早已由 checks 抛错决定,超大的自验证转储本就超出一次有界运行能持久化的范围,因此记为大小注记而非全文。
2. **事后磁盘封顶**。`scripts/compact_results.py` 扫描结果树,对超过硬阈值的文件流式求 SHA256、写存根、按需删除原件,保留清单与身份。它把 v1 的 12 GB、v3 的 4.6 GB 结果树各压到 12 MB,100% 缩减,身份可核。

## 6. 修复后十案全部通过（v3，有界运行时）

用加了构造期上限的运行时重跑十案,CPU stub,全部 exit 0、无超时、在 60 秒预算内:

| 情境 | wrap | 用时 | passed | result.json | trial.json |
| --- | --- | ---: | --- | ---: | ---: |
| B-C01 | 0 | 45.2s | True | 181 KB | 92 KB |
| B-C02 | 0 | 41.1s | True | 181 KB | 92 KB |
| B-C03 | 0 | 32.3s | True | 79 KB | 214 KB |
| B-C04 | 0 | 32.7s | True | 79 KB | 214 KB |
| B-C05 | 0 | 30.9s | True | 37 KB | 149 KB |
| B-C06 | 0 | 31.0s | True | 37 KB | 98 KB |
| B-C07 | 0 | 26.3s | True | 37 KB | 102 KB |
| B-C08 | 0 | 25.4s | True | 37 KB | 220 KB |
| B-C09 | 0 | 14.6s | True | 37 KB | 217 KB |
| B-C10 | 0 | 15.2s | True | 37 KB | 0 KB |

十个受检父子组合情境首次全部执行并通过。证据在 `results/src1b-convergence-v1/cpu-cases-v3/`(已 compact 至 12 MB,身份存根加清单保留)。初次 v1 运行(八案超时)的 run.json 作为挂起的原始证据,也 compact 保留在 `cpu-cases/`。

边界不变:worker 是 stub,非真实 MLX;T01 工具自验证的 64 负例族(当前 12 个)与真实模型 P01 仍未收敛。构造期上限改变了 trial.json 的记录形态(超大子结构记为存根),这是为了让有界运行能完成而做的刻意取舍,已在第 5 节说明。

## 7. 真实模型 P01：SRC1 B 首次在真实 MLX 上跑通（新增）

此前 SRC1 B 的父组合从未在真实模型上运行,只有 stub,且服务响应 token 从未真正流进 call_tool(只在字面量 0 上取到过符号)。本轮用通用 harness 驱动 `src1b-checked`(真实 MLX 模式),这是此前任何 wrapper 都做不到的:父程序编译、子服务 B 走真实 Prefill、call_tool 读真实 11,422,654 字节的 tokenizer。

结果:exit 0,passed=True,status completed,34.4 秒。证据 `results/src1b-convergence-v1/gpu-p01/real-model-v1/`。关键事实:

- 子服务 B 的真实首 token 为 **271**(与 CM2、SMWO1、消融一致)。
- 该 token 以 `argument_token:"271"` 真正作为参数传入 `call_tool(tokenizer.vocab_symbol, b)`,是真实数据流,不是字面量。
- worker 帧 `backend:"mlx"`(16 处),非 stub;tokenizer 字节数 11422654 在证据中。

这关闭了 SRC1 B 最核心的真实模型缺口:受检父程序、真实服务后继、真实工具后继三者在真实模型上组合通过,响应 token 贯通到工具。

## 8. T01 负例族:刻意封顶而非扩写（root 决策）

T01 图变异负例族计划 18 族 64 变体,此前手工装配了 12 个(每个一支专用 assemble 脚本加独立审阅)。核查确认:没有统一 runner,余下 52 个要么各写一支专用脚本(约 52 支),要么重建一个能复现每族 lens 语义的统一变异器。两条路都是巨量工作,且正是建议 5 点名要停止的"验证器自验证膨胀":一个仓库已有 103 个 T01 目录、数 GB 自验证转储。

更重要的是正确性风险:一个构造错误的负例会因错误原因被拒,从而假证检查器有效,比不跑更糟。盲目量产 52 个负例违背建议 5 的实质。

因此本轮的决定是:**T01 负例族封顶在已装配的 12 个,不手工扩写**。设计前进路径是一个一次成型的参数化 runner,从 DESIGN-v1 的族/变体规格与 P00 基图自动施加每个变异并核验,以一次有界运行替代 52 支脚本。这既符合建议 5,也是完成该族的唯一不膨胀方式。是否投入实现该统一 runner,由 root 决定;在它存在前,T01 负例族的状态是"12/64 已装配,其余按设计封顶待统一 runner",而非"未完成待手工补齐"。
