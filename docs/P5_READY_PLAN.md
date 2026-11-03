# P5：先检验输入就绪状态与计算放置

2026-10-29。状态：本地材料、已安装 API 和源码检查完成；本计划未执行 GPU、CPU 数值 benchmark、下载、安装或重编译。下面的性能判断均为待验证假设。

**推荐第一个直接实施的实验：用 Qwen3 MLP up projection 的真实维度，比较同一 FP32 数学运算在 CPU／GPU 上执行时，输入 ready、保留上游惰性依赖、显式求值边界三种条件。** 首轮用 `M=1,128; K=1024; N=3072`，共 12 个配置。使用 tiny-llm 已有 Python MLX 0.32.0 环境，无需新依赖或编译。先回答求值和放置是否影响端到端时间，再单独加入行切分、权重布局和竞争；不预设它们必须组合。

## 1. 已核实的本地实现

| 本地证据 | 可用于什么 | 仍不能推出什么 |
| --- | --- | --- |
| [MLX 0.32.0 安装信息](/Users/fbsh/tiny-llm/.venv/lib/python3.12/site-packages/mlx-0.32.0.dist-info/METADATA)，安装的 `core.cpython-312-darwin.so` 中 API 签名 | `matmul`、`maximum`、`transpose`、`contiguous`、`concatenate`、`quantized_matmul`、`dequantize` 均有显式 `stream` 参数；有 `eval`／`async_eval` | 没有运行新形状的能力或性能 smoke；API 存在不保证每个 dtype／后端组合可执行 |
| 同版本 [ops.h](/Users/fbsh/tiny-llm/.venv/lib/python3.12/site-packages/mlx/include/mlx/ops.h:1094)、[stream.h](/Users/fbsh/tiny-llm/.venv/lib/python3.12/site-packages/mlx/include/mlx/stream.h:32)、[transforms.h](/Users/fbsh/tiny-llm/.venv/lib/python3.12/site-packages/mlx/include/mlx/transforms.h:12) | 明确运算放置、新建 stream、同步／异步求值；`contiguous(..., allow_col_major=false)` 支持要求行连续 | `async_eval` 返回不是设备完成；默认 `synchronize()` 不是任意多 stream 的全局完成凭证 |
| `otool -L libmlx.dylib` 显示 Accelerate；`nm -u` 显示 `cblas_sgemm$NEWLAPACK`、BNNS 导入符号 | 安装版本确有系统 CPU 数值库依赖，可作为 CPU 后端来源记录 | 不能凭链接信息断定某次调用走哪个内核，更不能标为“实测 AMX” |
| 本机 MLX Swift 0.33.0 [CPU FP32 matmul](/Users/fbsh/swift/MLXLab/.build/xcode/SourcePackages/checkouts/mlx-swift/Source/Cmlx/mlx/mlx/backend/cpu/gemms/cblas.cpp:10)、[stream](/Users/fbsh/swift/MLXLab/.build/xcode/SourcePackages/checkouts/mlx-swift/Source/MLX/Stream.swift:23)、[contiguous](/Users/fbsh/swift/MLXLab/.build/xcode/SourcePackages/checkouts/mlx-swift/Source/MLX/Ops.swift:3630) | 本地完整源码明确有 FP32→`cblas_sgemm`、设备选择和强制布局接口；后续可以迁移相同实验契约到 Swift | 0.33.0 源码不能当 Python 0.32.0 的精确实现；两个版本的性能不能混组 |
| 当前 Xcode SDK 的 [cblas.h](/Applications/Xcode.app/Contents/Developer/Platforms/MacOSX.platform/Developer/SDKs/MacOSX.sdk/System/Library/Frameworks/Accelerate.framework/Versions/A/Frameworks/vecLib.framework/Versions/A/Headers/cblas.h:610)、[vDSP.h](/Applications/Xcode.app/Contents/Developer/Platforms/MacOSX.platform/Developer/SDKs/MacOSX.sdk/System/Library/Frameworks/Accelerate.framework/Versions/A/Frameworks/vecLib.framework/Versions/A/Headers/vDSP.h:2448) | 有 `cblas_sgemm`／`cblas_sgemv` 和实际转置 API，可做后续独立 Swift Accelerate 路径 | 需要另建、编译新 harness；不是本轮零编译的起点，也不是公开的 AMX 指令控制接口 |

本次通过读取文件、safetensors header、二进制签名／依赖完成检查，没有导入 MLX 来执行运算。Python 安装未提供 `.pyi`，所以 API 签名以安装二进制内嵌签名及同版本头文件核对。现有 Swift MLXLab 已有 [CPU／GPU linear 与计时实现](/Users/fbsh/swift/MLXLab/Sources/MLXLabCLI/Experiments.swift:88)，但本任务不修改或运行该参考目录。

查验锚点：Python MLX `0.32.0`；Swift MLX `0.33.0`、commit `7ede4fd6d05f63305c1c0ebf4f8c1909a637c574`；Python `core` SHA256 `f96aede5d6eee539d4826a52690914e79794e2ad2c691935d02dca6b0c421c56`。正式运行仍须重新记录实际导入二进制、库、源码哈希和环境。

## 2. 材料贡献分别落在哪里

FusionML 的 [最小 probe](/Users/fbsh/swift/References/apple-silicon-inference/sources/FusionML-benchmarks/repository/benchmarks/python/stream_parallelism_probe.py) 确实通过 `mx.matmul(..., stream=mx.cpu/gpu)` 分配输出行，并区分已求值输入、GPU `maximum` 形成的惰性依赖和显式 `eval` 边界。其 eager 路径把两个 `eval` 都放在计时区内。我们先取其中的就绪状态问题，不引入自动比例搜索或模型 monkey patch。归档是 benchmarks commit `4e7121a8f9c70fd6512b233c5705374626323471`；作者旧环境的速度比不作为本机预期。

inferc 的普通 [matmul.cc](/Users/fbsh/swift/References/apple-silicon-inference/sources/inferc/repository/src/kernels/matmul.cc:129) 使用 Accelerate。另一个明确的 [AMX prepack 接口](/Users/fbsh/swift/References/apple-silicon-inference/sources/inferc/repository/src/kernels/amx_prepack_gemm.h) 才分开 `AmxPackWeight` 与 `AmxPrepackedSgemm`；其 [实现](/Users/fbsh/swift/References/apple-silicon-inference/sources/inferc/repository/src/kernels/amx_prepack_gemm.cc:101) 要求 `M % 16 == 0`，直接使用 AMX 宏。归档中未发现现成 build/bin/dylib/so 产物。本轮采用“准备一次还是每次准备”的可检验问题，不以 MLX transpose 替代真正 AMX packing。`M=1` decode 也不满足这份直接 AMX 接口的形状条件。

该 prepack 对象的尾列可能借用原 B 指针，原权重生命周期是接口义务；这是后续 PL 所有权研究的具体切点。普通 CPU／Accelerate／直接 AMX、权重布局／数值精度／计算放置三个区别都要保留。更多材料界限见归档 [FusionML 笔记](/Users/fbsh/swift/References/apple-silicon-inference/FUSIONML.md)、[inferc 笔记](/Users/fbsh/swift/References/apple-silicon-inference/INFERC.md)。

## 3. 来自 tiny-llm 的形状，而非任意方阵

本机 Qwen3 snapshot `173234aa840d113125e9f2271100ddbaf16c9620` 的 [config.json](/Users/fbsh/.cache/huggingface/hub/models--Qwen--Qwen3-0.6B-MLX-4bit/snapshots/173234aa840d113125e9f2271100ddbaf16c9620/config.json) 给出 hidden 1024、intermediate 3072、Q heads 16、KV heads 8、head dim 128。读取权重 header 也核对了 layer 0 的相应 packed 形状。采用 `X[M,K] × W[N,K]ᵀ → Y[M,N]`：

| 路径 | K | N | 如何解释 M |
| --- | ---: | ---: | --- |
| MLP up／gate，首个实验采用 up 形状 | 1024 | 3072 | 单请求 decode 为 1；prefill chunk 为 32／128／512 |
| MLP down | 3072 | 1024 | 同上 |
| attention Q projection | 1024 | 2048 | 同上，不能把 hidden 1024 误当 Q 输出宽度 |
| attention K／V projection | 1024 | 1024 | 同上 |
| attention output projection | 2048 | 1024 | 同上 |

持续 decode 实验中的 B1024 可对应 M1024；第一轮只测 M1 和 M128 以缩小矩阵。其余形状和 M 值在正确性和方差有依据后增加。

当前真实模型是 4-bit packed 权重、BF16 activation/scales/biases，Week 3 默认进入 [mx.quantized_matmul](/Users/fbsh/tiny-llm/src/tiny_llm_ref/quantize.py:55)。**本计划首轮是形状取自模型的 FP32 算子实验，不是替换真实量化层的等价性能对照，更不是模型 TTFT。** 不能用 FP32 Accelerate 对 4-bit GPU 的结果来解释设备放置本身。真实权重／BF16／量化的接入另设数值与后端支持门槛。

## 4. 首个最小对照：相同消费者，不同输入状态

公共数学任务为 `H = maximum(X, 0); Y = H @ W.T`。`maximum` 是已确认可显式放在 GPU 上的最小上游生产者，不把它称为 Qwen 的真实 SwiGLU 或 RMSNorm。X/W/H/Y 均为 FP32，权重布局先固定为同一个 W 与其转置 view；CPU 和 GPU 消费者使用完全相同的数组数值和数学操作。

| 输入状态 | 处理方式 | 合法比较 |
| --- | --- | --- |
| `ready` | 计时前显式 `eval(H)`，确认输入已就绪；计时新建 Y 并 `eval(Y)` | CPU-ready 对 GPU-ready，比较就绪消费者；生产者准备耗时另存 |
| `lazy` | 计时开始后新建 GPU `H=maximum(X,0)`，接消费者，在一次 `eval(Y)` 中执行依赖图 | CPU-lazy 对 GPU-lazy；端到端包含生产者、依赖等待和消费者 |
| `boundary` | 计时开始后新建同一个 H 并 `eval(H)`，再构造 Y 并 `eval(Y)` | 同 placement 的 boundary 对 lazy；**两个 eval 和生产者成本都计入** |

每个状态分别使用 `mx.matmul(H, WT, stream=mx.cpu)` 与 `stream=mx.gpu`。生产者始终明确为 GPU。X/W 初始构造和求值、环境预热放在公共准备阶段。每个重复必须新建待测输出，lazy／boundary 还须新建 H；不能反复对已经完成的 Y 调 `eval` 充当重算。

ready 与 lazy 的 `request_ms` 分母覆盖的工作不同，禁止直接把二者的比值称作 eager 获益。ready 回答“输入已经存在时放哪”；lazy/boundary 回答“从产生输入开始，显式边界是否值得”。ready 的准备成本单列，若业务每次都需要新生产 H，则评价采用含该成本的完整路径。

初始数据使用有界二进制分数，如整数 `[-8,8]/128`，种子固定。K≤3072 时乘积和部分和可保持 FP32 可精确表示的范围；用独立 FP64／整数参考核对完整输出，排除 NaN/Inf，保存最大绝对／相对误差和错元素数量。首轮要求数值完全相等（不混称原始位模式相同）；非零误差保留为失败并调查精度／归约路径，不能为取得速度结论临时放宽容差。后续真实权重输入使用另行事先固定的误差契约。

最小执行顺序：先在 M1 和很小的 K/N 做每种接口的能力与数值 smoke；再用真实 up 维度 M1/M128。每配置预热至少 1 次，小 pilot 每配置 3 次，跨轮轮换 6 个状态／设备配置次序。GPU 由 root 串行排程。若设备或 dtype 不支持，保存 capability failure，不静默改 dtype、dequantize 或换后端。

## 5. 第二个独立对照：布局准备的真实成本

在 ready 输入上固定 placement，比较三个等数学的权重表达；先做 GPU，再独立做 CPU，不把设备与布局同时变更：

| 策略 | 准备与生命周期 |
| --- | --- |
| `transpose-view` | 保留行连续 W[N,K]，每次用 W.T。不能把 view 构造时间叫作权重 packing |
| `contiguous-each-request` | 每次从相同 W.T 构造 `mx.contiguous(..., allow_col_major=False, stream=<固定准备stream>)`，实际 eval 后再 matmul；准备计入请求 |
| `contiguous-once` | 同样的连续布局只物化一次并持有，后续不同 X 重复使用；分别记录准备时间、保留字节、请求时间及含准备的累计时间 |

使用非方阵 W，避免形状对称掩盖布局含义。默认准备 stream 固定为 CPU，以便两种复用策略只改变准备频率；如改为 GPU，另成配置。`contiguous` 是明确的布局物化，不宣称能控制 MLX／BLAS 的内部 packing。已读的 CPU matmul 实现还会识别合法转置布局，因此“预转置必然更快”没有依据，零收益或更慢都应保留。

记录 `prep_ms`、`steady_request_ms[]`、`total_for_R_requests_ms`、持有的原始／连续权重字节；初始 R=1、8、32。全部策略处理同样 R 个 X 和相同输出。一次编译／库初始化与每次新权重准备分别记录，不能把前者混成 prep。只有实测完整总时间才判断摊销；若 steady 每次没有节省，不能编造 break-even 次数。

缓存键包含权重内容身份／generation、形状、dtype 和布局。改变 W 后旧准备对象必须失效，负例应由元数据检查拒绝；原 W 和任何被借用的内容存活到消费者完成。缓存权重导致额外内存驻留，收益与字节成本同时报告。这一对照借鉴 inferc 的准备生命周期，但不复制其 AMX 内核或声称已复现论文 packing。

## 6. 再加入异构行切分与竞争

行切分是首轮之后的小扩展：只在 M≥32 时固定 CPU 25% 行、GPU 75% 行，输出不重叠，合并操作显式放在固定 GPU join stream，并计入拼接与等待成本。ready/lazy/boundary 各有 GPU-only 对照；M1 标记行切分不适用，不能用空 CPU 分支假冒有效 split。仅观察速度比不能证明 CPU/GPU 实际重叠，需要额外设备／CPU 时间线证据；否则只称“在两个执行域放置的组合路径”。

竞争对照先只用 ready 输入，逐项加入一种真实、固定工作量的干扰，避免把所有维度一次混起来：

- **GPU 竞争：** 在独立、复用的 GPU stream 上构造固定 GEMM，实际 `async_eval(G)` 后执行目标请求。目标 CPU／GPU／split 三策略都完成相同 G 和 Y，并数值验证两者。记录 async 请求／返回、目标结果可用、最后全部输出完成；提交返回不等于 G 已开始。若 G 实际已完成或排队关系不同，保留该限制，不假定存在重叠。
- **CPU 服务干扰：** 一个预先启动且不导入 MLX 的工作进程，在目标 eval 前的事件锚点收到固定服务请求，例如固定次数的已知字节块 SHA256，返回校验值。记录服务到达、真实开始／结束和父进程收到结果的时间，另测无目标算子的相同服务基线。没有 sleep 替代计算，也不把子进程启动算作服务成本。它衡量 CPU 执行资源受到的干扰，不冒充完整 agent 工具响应。

每对配置固定目标输入、背景输入／哈希工作量和触发规则，同时报告目标端到端、服务端到端及全部工作完成时间。不要把目标更快但挤慢 CPU 服务的配置称为无条件更好。没有硬件计数器／跟踪时，不能把竞争结果唯一归因于 UMA 带宽、AMX 或 CPU 核心占用。

所有 MLX 数组由同一宿主线程构图／提交，CPU 服务进程不访问模型和 MLX 数组。保留所有输入、输出和布局对象直到实际完成。多 stream 时对所有相关输出 `eval`，并在 finally 显式同步每条已使用 stream 后才释放；不可只调用默认 `synchronize()` 就声称全局排空。GPU 竞争是新 harness 内受控负载，不与其他实验进程同时跑。

## 7. 与真实 tiny-llm、Swift 和 PL 的后续连接

算子阶段通过后，再从独立的新驱动取得 layer 0 的实际输入 activation 和实际权重，记录 prefill M32/128/512、decode M1 的形状／dtype。静态配置只确认形状，尚未完成动态 activation 采样。

真实量化放置要先验证 `mx.quantized_matmul` 的 CPU/GPU 支持和输出契约，两端保持相同 packed weight、BF16 activation/scales/biases、group size128、bits4。任何一端不支持就停止该对照；不能把 CPU 静默换成 dense FP32。若研究“先 dequantize／重排再反复调用”，明确将其设为表示准备实验，单独计入准备、内存和数值变化。

只有算子获益且正确后，才把候选路径接入新的 A decode／B prefill 驱动，保存 A 全 token 序列、B TTFT、A 用户 ITL、固定总工作和缓存状态。不能仅凭 linear 的毫秒数推出完整模型或 agent 获益。已冻结的 `tiny-decode-interleave`、`tiny-interleave`、`tiny-fault-recovery` 不改；必要时由新驱动复用只读 helper，并固定来源。

Swift 候选接口可以分别表达“deferred 输入及其生产者”“已经观察完成的输入”“持有内容身份的 prepared weight”，设备选择另作执行策略。它们是否需要合为一个抽象，应由上述依赖与成本证据决定。第一轮 Python 使用现有 0.32.0 减少环境改动；Swift 0.33.0 对照以后单独归档，不跨版本混合性能。普通 CPU 路径始终标 `mlx-cpu` 或 `Accelerate`；只有实际编译、调用并核查直接 AMX 路径后才增加对应标签。

## 8. 首个实现的交付契约

建议新建 `experiments/mlx-ready-placement`，`experiment="mlx-ready-placement"`、schema v1。配置至少记录 M/K/N、dtype、placement、readiness、producer GPU stream、consumer stream、weight layout、seed、输入哈希、预热和 trial 数。记录实际软件／二进制、参考形状源码和模型 config 哈希；Python 与 Swift 的 provenance 分开。

原始事件包含 `request.started`、`producer.graph_created`、实际存在时的 `producer.eval_started/returned`、`consumer.graph_created`、`consumer.eval_started/returned`、`result.available`、各 stream 清理和 `trial.drained`。ready 的 `input.ready_observed` 在 request 前。lazy 内部生产者何时结束若不可见，**不补一个推测的 producer completion 时间戳**。验证导出和全矩阵参考在计时之后，成本另存。

输出保留 setup／preparation、构图、显式求值等待、端到端、全部数值检查及原失败信息。主结论限定为：在这台 M1、这一版本、这些形状与显式精度下，输入状态与设备放置如何影响已声明时间区间。不能由此证明 UMA 零拷贝、硬件并行、AMX 使用、完整量化模型收益或响应时间上界。

本计划可以直接转为零新增依赖的 Python harness。首次实施先做 12 格 ready/lazy/boundary × CPU/GPU × M1/M128；行切分、布局和竞争各自经过单独 smoke 后增加，不把它们做成一个尚未有证据支撑的综合调度器。
