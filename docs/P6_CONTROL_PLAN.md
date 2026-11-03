# P6：有界评分循环的控制位置

2026-10-29。状态：四模式 × T16/T64正常及T16取消共16smoke通过独立全量核验；14项实验侧CPU和39项检查器测试通过。随后64main+64warmups正常矩阵完成并独立核验，另有11项编排CPU和16项矩阵检查器测试。[首次报告](../results/batch4/control-smoke/REPORT.md)与[矩阵报告](../results/batch4/control-matrix-REPORT.md)分列；执行中取消和动态控制需求仍未验证。实现为 [metal-control-location](../experiments/metal-control-location)。不改既有 P4/P5 实验。

问题是：**固定的一串相关计算，如果每轮都等 CPU 再启动下一轮，和一次预描述全部计算相比，会付出什么代价？如果进一步把循环放进一个设备 kernel，新增的变化又是什么？** 首要对照固定 kernel 集合和逻辑全局内存访问。设备循环另列，不能把 kernel 融合的收益写成控制位置的纯收益。

## 1. 具体情境与固定算法

设一个候选探索器有 64 个候选，每个候选具有 128 个有符号整数特征。当前 context 与候选做点积，减去已选择次数的惩罚；选择得分最高者，将 context 向其特征更新，再进入下一轮。它可以帮助理解 agent 中“评分—选择—更新”的局部结构，但**这是合成数值算法，不执行 LLM、token 解码、工具调用或 agent 端到端任务**。

首轮固定 `C=64, D=128, seed=7, penalty=257`，先 `T=16` 正确性 smoke，再按证据决定 `T=16/64` 的小型性能矩阵。所有模式使用相同输入、T、初始状态、完整历史输出。终止条件就是执行 T 轮，不按得分提前停止。

```text
V[j,d] = ((17*j + 13*d + seed + ((j+1)*(d+3) mod 23)
           + ((j*d + 3*j*j + seed) mod 29)) mod 17) - 8
q₀[d]  = ((7*d + seed) mod 17) - 8
visits₀[j] = 0

scoreₜ[j] = Σ(d=0..D-1) qₜ[d] * V[j,d] - 257*visitsₜ[j]
winnerₜ = 得分最大的最小候选 index
visitsₜ₊₁[winnerₜ] = visitsₜ[winnerₜ] + 1，其余不变
qₜ₊₁[d] = trunc_toward_zero((3*qₜ[d] + V[winnerₜ,d]) / 4)
```

CPU reference 使用 Python 任意精度整数、显式向零除法和逐元素循环；不调用 Swift/MSL 实现或其 helper。GPU 和 Swift 用有符号 `Int32/int`；不依赖 signed overflow。因为 `q,V∈[-8,8]`，每个乘积绝对值≤64，D=128 时点积绝对值≤8192；T≤64 时惩罚≤16448，score 的保守范围为 `[-24640,8192]`；更新分子绝对值≤32，向零除以 4 后仍在 `[-8,8]`。所有中间值严格位于 Int32 范围内。

tie-break 固定为最小 index。每个配置先检查整个 winner history 至少出现两个不同特征的候选、context 实际变化，并确认至少一个 score 向量差异不能仅由 visits penalty 解释；这是防止输入退化的门槛，不是性能门槛。CPU 设计检查得到 64 个不同特征行，T16 选择 3 种、T64 选择 11 种不同候选；冻结 context 的反事实给出不同 winner history。保留前缀与最后状态，不只检查一个 checksum。

最初纯 CPU 设计曾用仅含 mod23 的特征项和 penalty31，发现候选行重复、winner 只在重复行间交替；因此在任何设备执行之前加入 mod29 项并固定 penalty257。这个输入选择诊断不计为 GPU 样本，也不作为性能证据。上述公式是后续实现和独立分析采用的版本。

## 2. 四模式与可归因的对照

| 模式 | 每轮发生什么 | T 轮 command buffer／dispatch 数 | 能回答什么 |
| --- | --- | --- | --- |
| H `cpu-feedback` | score kernel 完成→CPU 读取本轮 scores、argmax 并更新 visits／winner→update kernel 完成 | 2T CB、2T dispatch | CPU 根据实际结果做动态选择的路径；同时包含 CPU 读回与选择位置差异 |
| G `gpu-step` | 同一个 CB 内 score→select→update，完成后 CPU 才编码／提交下一轮 | T CB、3T dispatch | 每轮 host 往返的 GPU 选择基线 |
| P `gpu-preencoded` | CPU 预描述固定的 3T 个 kernel；各轮 select 在 GPU 读取该轮真实 score | 1 CB、3T dispatch | 与 G 使用完全相同 kernel、参数、访问与输出，只改变 CB 封装和逐轮 host 往返 |
| F `device-loop-global` | 单一 threadgroup 内运行 T 次 score→select→update，保存同样全局历史 | 1 CB、1 dispatch | 单独的设备循环／融合对照；dispatch、barrier、状态驻留均可能改变 |

**主对照是 G↔P。** 二者必须调用同一 `encodeRound`，每轮均创建 score/select/update 三个独立 compute encoder，绑定相同的 tracked buffer，采用同一 queue 和 threadgroup 配置。全局 score/context 历史、visits 和 winner 的逻辑读写也相同。P 的后一轮选择不是 CPU 预测出来的常量：它读前一轮更新后的 q/visits，再真实评分和选择。P 可成立是因为轮数、kernel 类型和依赖结构预先已知，不代表能够预描述未知的 host I/O、工具效果或任意数据依赖停止条件。

H↔G 同时改变选择执行位置、CPU 数组访问及 dispatch 数，只作额外机制观察；不能称单变量控制位置效应。P↔F 包含 kernel 融合、跨 dispatch 状态保持和调度边界变化，另列结果。F 首版仍显式读写全局 q/score/history，不新增 threadgroup-local 存储优化；但相同逻辑读写不保证相同物理 DRAM 流量、cache 行为或寄存器分配。任何后续 local-storage 变体另开配置与归因。

F 没有 `Future`、`poll/Pending`、waker、设备 executor、hostcall、动态 spawn 或软件挂起。一个有界 MSL 循环不能命名为 device Future，也不能声称让出硬件驻留资源。

## 3. Metal 前提与资源生命周期

首版使用传统 `MTLCommandQueue/MTLCommandBuffer`、显式 `.storageModeShared` 与 `.hazardTrackingModeTracked`，直接绑定 buffer，不使用 heap、argument buffer、concurrent encoder 或 Metal 4 queue。Apple 文档将传统 queue 上直接绑定的 tracked 资源列入自动访问冲突同步范围；Metal 4 的规则不同，本实验不把两套 API 混用。[Resource synchronization](https://developer.apple.com/documentation/metal/resource-synchronization)、[tracked](https://developer.apple.com/documentation/metal/mtlhazardtrackingmode/tracked)（2026-10-29 查阅）。

G/P 每个 dispatch 独立 encoder；参数按轮使用 `setBytes` 的值快照或只读固定 offset。禁止反复覆盖一个 GPU 尚未读取的共享参数槽，从而使 P 各轮误用最后一轮参数。CPU 在 H 读取 score 或写 winner/visits 前必须已经观察 score CB 实际完成；任何模式的最终 CPU 验证都在相关 CB 完成后。

F 严格只 dispatch 一个 threadgroup（128 threads），score 阶段仅前 C 个线程工作、select 由 lane 0 顺序 argmax、update 阶段前 D 个线程工作。每阶段间所有线程一致经过 device-memory threadgroup barrier；不使用跨 threadgroup 自旋屏障。每个 pipeline 都读取并核查 `maxTotalThreadsPerThreadgroup`，不把 128 当所有 pipeline 天然保证。[Apple pipeline 限制](https://developer.apple.com/documentation/metal/mtlcomputepipelinestate/maxtotalthreadsperthreadgroup)。首次真实 T16 数值 smoke 已通过；这不替代其他设备/形状或所有调度的证明。

工作集为 V、q、visits、`scores[T,C]`、`winners[T]`、`q_history[T,D]`、完成轮数和固定参数。四模式分配相同逻辑 payload，并在每 trial 重新初始化 q/visits/history。每轮完整历史在 GPU 写入；H 的 CPU 选择额外读写是该模式本身成本，不能删掉其他模式历史以替它优化。原始最终数组、输入和完整参考 hash 一起保存。

资源只有在停止新增 host 提交、所有已提交 CB 完成状态得到处理、最终必要读取结束后才释放。完成 handler 是主机观察，另存 GPU 起止时间；同时检查 CB status/error，未知错误保留为失败。[Apple completion handler](https://developer.apple.com/documentation/metal/mtlcommandbuffer/addcompletedhandler(_:))。超时或取消都不授权释放仍有访问可能的 buffer。

## 4. 取消边界与工作量

先完成无取消固定 T 轮对照，再增加独立取消正确性记录；不能把不同已执行工作量合并进吞吐表。

- 未提交前取消：四模式均不提交；保留取消与零工作证据。
- 提交后取消：只记录 host 请求／受理，不声称 GPU 已运行或被抢占。H/G 在完整轮边界观察取消并停止下一轮；H 若 score 已提交，则仍完成 CPU select 与 update，履行这一轮的义务。
- P/F 的整批已提交后，CPU 不能撤销已描述的 kernel 或循环；等待完整 T 轮结束后归还。即使后续结果已没有业务价值，也不能把丢弃结果当作设备停止。
- 不从 CPU 直接读 GPU 正在更新的 progress counter，不让 GPU 轮询未经验证的 CPU shared flag，不以自旋、sleep 或未证明的共享原子协议伪造可取消设备 executor。

取消触发可先固定“首个 CB commit 之后”。它对四模式的已提交单位大小不同，必须报告实际提交／完成 round 数、完成的 dot 元素数和取消后继续的工作；它不构成相同 GPU 执行相位或相同剩余工作的对照。若后续按设备时间事后分类，仍按每条原始 GPU interval 判断，不凭 `scheduled` 或模式名认定执行中。

事件名区分 `host.submissions_closed` 和最终 `algorithm.completed_prefix`：P/F 关闭 host 提交不等于内部轮次停止。资源安全链仍是实际完成观察→必要 CPU 读取→release→drained。初版不引入 B 请求或声称 TTFT 改善。

## 5. 计时与日志契约

公共 pipeline 编译、buffer 分配／初始化和独立 oracle 在计时之外。`work.started` 在任何模式专属 command 编码前；`work.completed` 在最后相关 CB 完成被持有者观察到之后。主指标 `work_ms` 包含 P 的全部预编码成本、G 的逐轮编码／等待以及 H 的 CPU 选择，不能从首次 commit 才起表而隐藏 P 的前置工作。

另报每个 CB 的 encoding 区间、commit、completion callback 和持有者完成观察；H 报 `host.selection_started/ended`；CPU 数值验证区间单列。派生 `first_commit_to_completion_ms`、各 CB `gpuEndTime-gpuStartTime` 的总和与总 span，但它们不等于纯 kernel 运算时间或端到端工作时间，不能与有重叠的 host 阶段直接相加。

schema v1 拟为 `experiment="metal-control-location"`，每 trial 包含 mode/C/D/T/seed/penalty、source/ABI/binary hash、events、command IDs/每 CB 的静态 round/stage 描述、原始 GPU timestamps/status、完整 actual arrays/reference hashes、checks、metrics、取消状态和最终在途／持有计数。每轮编号与固定参数快照必须可审计；未实际观察的设备内部阶段不造 CPU 时间戳。

正常试验核对：G/P 各完成相同 3T dispatch、C×D×T 次点积元素乘积，完整 winner/score/q 历史及最终 visits 相同；H/F 计算同一算法，但各自 dispatch 数如实不同。记录 encode/commit 数量不当作硬件工作并行证明。

最初每模式 1 次数值 smoke，由 root 串行排 GPU。之后先比较 G/P，再分别加入 H/F；同参数以平衡顺序区组运行，保留每 trial 差与范围，不以必须加速作为 PASS。T 或形状不同的工作量不合并；早期 pilot 不报告稳定尾延迟、最优策略或最坏响应上界。

## 6. 与归档材料的对应

- [cuda-async 固定快照](../../swift/References/gpu-async-rust/CUDA_ASYNC.md)（`a4a939a9...`）区分操作描述、提交、完成和访问租约；P 借鉴其“描述可先于执行”的问题，**不是 CUDA Graphs 的 Metal 等价实现**。`.then` 的主机组合也不表示每步读取设备结果。
- [VectorWare](../../swift/References/gpu-async-rust/VECTORWARE.md) 与 [async-gpu](../../swift/References/gpu-async-rust/ASYNC_GPU.md)（`73b24a95...`）讨论设备执行器、Future 和 hostcall。F 只先测量保留多步控制在设备上的最小算法；其等待／调度／效果机制均未实现。
- PriML 的响应性问题仍需要依赖和进展假设。减少 host 往返不自动改善取消响应，整批提交反而扩大取消后必须收尾的单位；CPU priority 也不因此变成 GPU 抢占机制。

这些材料是设计来源，不是本机已复现的 CUDA 功能。完整语义与成本比较之后，才考虑是否值得引入设备 Future、hostcall 或新的 Swift 接口。
