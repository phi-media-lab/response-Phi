# P5：权重布局准备与有效期的最小实验

2026-10-29。本文是实施计划，尚未运行布局性能实验。P5 readiness 的首个 pilot 已保存；它暴露了毫秒以下／数毫秒路径的波动，布局收益必须通过同区组配对和累计时间验证，不能由一次测量宣布。

首个问题是：**同一份固定权重使用 R 次时，显式行连续布局准备一次，是否比每次准备或直接用转置 view 更合算？权重换代后，旧布局能否在执行前被拒绝？** 初版只用已有 MLX Python API，保持 CPU/GPU 放置、输入 ready 状态和精度固定，不构建综合运行时。

## 1. 与 inferc 的准确对应

归档 inferc 的 [AmxPackWeight](/Users/fbsh/swift/References/apple-silicon-inference/sources/inferc/repository/src/kernels/amx_prepack_gemm.cc:85) 分配并复制权重面板，调用者能够控制准备频率。[AmxPrepackedSgemm](/Users/fbsh/swift/References/apple-silicon-inference/sources/inferc/repository/src/kernels/amx_prepack_gemm.cc:101) 每次还会转置 activation，准备好权重不等于全部准备成本消失。[接口](/Users/fbsh/swift/References/apple-silicon-inference/sources/inferc/repository/src/kernels/amx_prepack_gemm.h) 的尾列借用原权重指针，因此布局对象的生命周期不能脱离原权重。

本实验取其“准备成本与有效期”的问题，执行的是 `mx.contiguous(W.T, allow_col_major=False)`，不是 inferc 的 AMX 面板布局。MLX 的 CPU matmul 能识别合法转置输入；因此显式转置复制可能毫无收益，甚至更慢。不能把 view 构造命名为 packing，也不能把 CPU／Accelerate 标签称为直接 AMX。

## 2. 固定输入与最小范围

首轮固定 `M=128, K=1024, N=3072`，即 Qwen3 MLP up projection 的 prefill 形状；消费者先固定 `mlx-gpu`，准备 stream 固定 `mlx-cpu`。保持 FP32 dense `Y_r = X_r @ Wᵀ`，没有量化、activation producer、行切分和外部竞争。单独完成 GPU 对照后，如值得再用同样协议测试 CPU 消费者，不能把两种 placement 混成一个布局收益。

R 取 **1、8、32**。每个配置完整计算相同 R 份输入／输出；首轮为 3 个布局策略 × 3 个 R，共 9 个配置。先 R1 的三策略 smoke，随后按相同 R 将三策略相邻、轮换／反转顺序；初步每配置至少 3 次，观察方差后再确定正式重复数。

固定 seed=7，用整数生成精确 dyadic 数据，所有算子仍收到完整 dense 数组：

```text
W_g[n,k] = (((7*n + 3*k + seed + g) mod 16) - 8) / 128
X_r[i,k] = (32 * (((5*i + 11*k + seed) mod 16) - 8) + r) / 4096
r = 0 ... R-1；性能阶段 g=0；换代验证阶段 g=1
```

这给 R≤32 提供每次不同的 X，避免不断对同一个结果 eval。K 方向周期仍为 16，可以独立用 int64 每周期点积乘 K/16，再除以 `4096*128` 得到全量参考。输入必须先核对周期，MLX 后端不接收压缩表示。最大整数部分和小于 2²⁴，采用 FP32 全量数值相等契约，全部 `M*N` 元素都检查；正负零通过规范化哈希处理。周期输入不代表真实 activation 分布，真实权重接入须另设实验。

X 序列、原始 W 和参考值全部在公共 setup 阶段准备／求值完毕；记录字节哈希和 setup 时间。性能阶段不生成随机输入、不导出输出、不做 CPU oracle。每个 trial 都创建新的布局对象／输出，缓存只在该 trial 内持有，不能把上个 trial 的一次准备偷偷沿用。

## 3. 三个布局策略与计时

| 策略 | 每个 trial 的操作 | 必须保存的准备次数 |
| --- | --- | ---: |
| `transpose-view` | 每个请求用原始 W 的转置 view，新建 Y 并 eval；不显式复制权重 | 0 次连续化；另记录 R 次 view 构造 |
| `contiguous-each-request` | 每次从原始 W.T 新建 P=`contiguous(..., False, stream=cpu)`，实际 eval(P) 完成后执行 Y=X_r@P，结果完成后才结束该次布局使用 | R 次连续化与 R 次实际准备 eval |
| `contiguous-once` | batch 开始后先从 W.T 新建并 eval(P) 一次，R 个请求依次重用同一 P；最后一个结果完成后再释放 | 1 次连续化与 1 次实际准备 eval |

不得对第一次完成的 P 再 `eval(P)` 冒充“每次准备”。每次准备的构造边界、对象 ID、来源 generation、实际 eval 次数必须可审计。所有策略都用同一 W 内容，不通过改变值来强迫复制；策略变更只有显式布局表达和准备频率。

时间起点具体固定如下：

- `batch_ms = batch.results_available - batch.started`。batch.started 在任何策略专属准备之前，batch.results_available 在第 R 个 Y 的 eval 返回后。它包含 once 的初始准备、each 的重复准备，以及所有请求构图／求值与记录，是首要摊销指标。
- `request_ms[r] = result.available[r] - request.started[r]`。each 在 request.started 后准备，once 在首次 request.started 前完成准备。因此两者的单次 request 不覆盖相同工作，**不能只拿单次 request 判断 once 获益**。
- `prepare_ms[j] = layout.ready_observed[j] - layout.prepare_started[j]`，包含转置 view／contiguous 构图、实际 eval 和主机记录。另存显式 eval 区间；不能用未求值的图构造耗时充当复制成本。
- `consumer_ms[r] = result.available[r] - consumer.graph_started[r]`，只回答该布局下调用 matmul 的服务成本。它不含之前的准备，亦不是纯 GPU 内核时间。
- `amortized_ms = batch_ms / R`，不减去公共开销、不用独立测出的 prep 中位数与 consumer 中位数拼成假想 total。不同指标的中位数不具备可加性。

R 个 Y 全部保留到 batch.results_available，之后做完整 CPU 导出与参考校验，使验证不会插进请求序列扰动缓存／服务间隔。导出验证时间和最终 drain 单独计时；它们不在 `batch_ms`，不得把 batch_ms 称为带验证的整进程墙钟。每个策略保留同样 R 个 Y，输出驻留成本一致。同步所有使用的 stream 后才释放仍在用的布局／输入。

## 4. 明确字节口径

FP32 W[N,K] 的 payload 为 `3072*1024*4 = 12,582,912 bytes = 12 MiB`。连续 P[K,N] 也是 12 MiB。原始 W 在三策略中都保持可用，不为 once 悄悄释放原 W；这种控制保留了换代／来源契约，亦明确 once 的额外驻留。

| 项目 | M128 数值 | 记录方式 |
| --- | ---: | --- |
| 原始 W payload | 12 MiB | `source_weight_bytes`，三策略相同 |
| P payload | 12 MiB／对象 | `prepared_payload_bytes`；view 不重复累计原始 W 的存储 |
| 每个 X | 0.5 MiB | 全序列预备，`input_sequence_bytes=R*0.5MiB` |
| 每个 Y | 1.5 MiB | 保留至整批验证，`retained_output_bytes=R*1.5MiB` |
| each 累计准备量 | R*12 MiB | `total_prepared_payload_bytes`；不同于同时驻留峰值 |
| once 累计准备量 | 12 MiB | 同上 |

初版报告的是 **张量 payload 和 harness 持有关系**，不是物理 DRAM 流量、实际复制字节或系统内存峰值。`transpose-view` 的 `.nbytes` 也会显示 12 MiB，不能据此把别名算成另一份分配。MLX 分配器可能缓存已释放 backing，each 的逻辑同时活跃 P 可只有一个，但不能宣称物理峰值严格只有 12 MiB。

当前 Python API 没有已核实的通用 raw stride／底层 buffer 身份观察接口；布局证据首先来自明确调用 `contiguous(..., allow_col_major=False)` 的 API 契约及实际 eval。输出 NumPy 数组的连续性可能来自导出复制，不能拿它证明 MLX 内部布局。若后续增加底层观察，应作为额外证据记录，不能据猜测补字段。

## 5. 最小有效期协议与过期 generation 负例

只实现一个小的 prepared-weight 句柄，不引入设备调度器。元数据为 `source_id`、单调 `generation`、不可变 `content_sha256`、shape、dtype、目标 layout、状态以及当前使用次数。原 W 和 P 都由句柄／来源对象显式持有；不允许绕过接口原地改 W 后继续复用旧 digest。

每次 `acquire_for(source)` **在构造 matmul 或调用 eval 之前**检查：

1. source_id、generation、content hash 全部匹配当前来源；shape/dtype/layout 满足消费者契约。
2. 布局已经实际准备完成，未被 close/retire；不把“已构图”视为 ready。
3. 成功获取后登记一次使用，实际 Y eval 返回或失败路径同步完成后才归还；仍有使用者时拒绝 close／源数据释放。

权重换代创建新不可变 W₁、generation=1，旧句柄 P₀ 被 retire。已经取得的旧使用者仍须完成／drain；从换代后开始的新 acquire 必须拒绝旧 P₀，不能偷偷计算旧值。新 P₁ 准备完成后，使用同一 X 得到与 W₁ 独立参考相同的 Y₁。

固定验证 trace 与性能分开保存：

```text
source(g0) → prepare(P0) → acquire/use/eval/return(P0) → publish(g1)
→ try acquire(P0, current source g1) → rejected_before_submission
→ prepare(P1) → acquire/use/eval/return(P1) → close → all streams drained
```

过期尝试的门槛是：恰好一次预期异常，拒绝前后 `consumer.graph_started`／`consumer.eval_started`／提交计数都不增加；不存在过期输出；随后新 generation 完整数值参考通过。预期拒绝记录与后续成功分开，未知异常、清理失败或数值错误不能被“这是负例”洗成 PASS。

纯 CPU 状态测试至少覆盖旧 generation、相同 generation 却错 source/hash、错 shape/dtype/layout、未准备 ready、重复 close、使用中 close。使用 gate／假完成事件检查拒绝条件，不实际制造 GPU 提前释放或共享数据竞争。正常 GPU smoke只执行已验证合法路径；禁止通过“试一下旧布局算错多少”来证明拒绝协议。

## 6. 验收与结论边界

实施目录为 `experiments/mlx-weight-layout`，不改已冻结的 readiness harness。schema v1 必须保留 R、策略、固定消费者／准备 stream、输入序列哈希、source generation、每次准备／使用／完成／释放事件，实际准备次数，R 个输出规范化哈希、完整数值检查、payload 统计和所有失败信息。

独立分析器要核对：三策略各完成 R 次 dense 工作；输出对相同 X/W；once=1、each=R、view=0 次连续化；prepare 在实际 eval 后才 ready；使用前必须 ready 且代次匹配；close 在使用计数归零后；batch 时钟包含策略专属准备；全部 stream 安全排空。负例单独核对拒绝发生在新构图／提交前及恢复后的新代次数值。

先回答 R1、R8、R32 的实际累计时间和驻留取舍。没有观察到累计收益就保留负结果；不能用理论 packing 次数代替速度证据，也不把周期 dense 算子收益推广成完整量化 LLM／agent 收益。本实验仅把 inferc 启发的准备生命周期变成一个可核验接口；真正 AMX 面板布局、借用尾指针和 Swift 静态所有权表达应分别设后续门槛。
