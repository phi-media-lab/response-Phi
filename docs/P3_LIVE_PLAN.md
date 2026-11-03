# P3 新阶段：CPU 工作运行中的请求到达

本阶段独立实现于 `experiments/swift-live-dependency`，冻结的 `swift-cleanup-dependency`、Core、P3 原始记录均只读。当前仅准备代码、协议测试与编译，不运行性能测量或 GPU；实际运行由 root 串行排时隙。

该初始计划之后，root 单独授权了 v1 三模式各一次固定 pilot；三次均为竞争已经运行、owner 尚未开算的 `before_cleanup_start`，原始记录完整保留。v1 源码/binary/三份 raw/CPU tests 已冻结到 `versions/v1`，不将它们解释成 during-cleanup。

下一独立 v2 增加 `arrival_owner_passes`，默认 0 保持旧资格政策；root 预先指定下一 pilot 用 K=8。触发需要竞争进度达标且 owner 实际完成至少 K 遍。owner 每遍只上报 `owner.progress_reported` 然后继续扫描，不 await、不 yield、不等待前台依赖。三个模式新增完全相同的检查点，跨 v1/v2 的毫秒差仍不能归因为单一参数；所有早/晚 phase 与竞争是否仍活跃继续保留。

## 问题与现有结果的区别

旧 P3 的 prepared/device-ready 版本先观察 A GPU 完成，再等 direct/forwarded 的提升回调或 hidden 的通知登记完成，最后共同放行 owner 清理与竞争。因此它测的是依赖已准备后的条件效应。

本阶段在 owner 的必要读取与真实竞争工作已经放行后，才由实际计算进度触发前台请求。触发不暂停 owner/竞争者，不等依赖或提升完成后才继续工作。没有用 sleep 模拟清理、竞争或请求间隔。其余 task 仍继续算，直到固定工作全部完成。

初版采用 CPU-ready 私有数组：计时前真实执行 UInt32 递推填充 A 数据并预先计算独立参考；owner 在计时段内读取/校验整个数组，释放后 B 才能重写同一数组并给出完整校验结果。该阶段不初始化 Metal，不伪造 GPU 事件，不把 CPU 数据准备称为 GPU 完成。它与旧 device-ready P3 共享数值/依赖问题，但因数据生成路径、读取检查点与日志成本不同，不将跨 executable 的毫秒差直接归因为到达政策；若需要纯因果比较，应另行构造同一新实现中的 prepared 对照，而不改冻结旧版本。

## 固定工作与到达政策

owner 为 `.background`，前台等待者为 `.high`，竞争者为 `.medium`。owner 与固定数量的竞争 tasks 只用一次共同启动 gate 协调注册；该 gate 的释放不依赖前台等待关系，也不依赖 owner 的 priority 已提升。

每个竞争者执行固定段数，每段真实 UInt32 LCG 递推，并在段末 `Task.yield()`；保存每段进度、最终值、总迭代和 yield 数，独立 oracle 可复算。owner 执行固定扫描次数，每遍记录进度及实际 `Task.currentPriority`，不在遍间人为等待前台，也不因三种模式改变扫描次数。所有模式的数值、循环、扫描、记录点和竞争总量一致。

请求触发政策是显式的 **真实工作进度触发**：指定数量竞争者完成至少第 K 段时，打开单次通知，已等待该通知的 high task 才记录 `b.requested` 并建立依赖。每个竞争者发通知后继续自身工作，owner 不等待这个通知。记录 `arrival.triggered` 与 `b.requested`，二者间包含实际事件投递/调度成本。

这不是外生随机到达模型，也不保证到达时竞争仍占用 CPU。任务可能很快结束，或者 high task 才获得服务时 owner 已清理完成；这些样本保留并分类，不能按结果删除或自动扩大负载重跑。初始候选配置为 65536 元素、64 遍扫描、8 个竞争者、每个 32 段×250000 次递推；最终实际测量参数须在运行前由 root 固定。

## 三种等待接口

1. `direct`：前台记录 dependency 开始，直接 `await owner.value`。
2. `hidden`：等待不保存 producer handle 的 continuation 通知，再读取 owner 已发布的复制结果。
3. `forwarded`：显式把前台实际 priority 转发给 owner，然后通过同一个通知等待。

本版本不等待 `owner.escalated` 后才继续 owner 工作。回调只记录 runtime 报告的目标 priority；owner 每遍扫描实际采样的 priority 是另一份证据。`Task.value` 之前的事件只表示调用边界，不冒充 runtime 已完成边登记；direct 的提升回调是已观察到传播的证据，但不存在回调也可能因为 owner 已结束，须结合到达相位解释。hidden 的 `notification.registered` 是实际 continuation 入队记录；若结果早已就绪，则记录立即返回路径。

请求同时标记 A producer 取消，已开始的必要扫描继续完成。owner 不能因为取消跳过访问义务；但若取消到达时扫描已结束，也不能要求过去的采样凭空成为 cancelled。driver 在测量期只等 producer-free 完成通知；owner、B、全部竞争者工作结束后，才统一 join handles，防止观察者捐赠 priority。

## 事件、分类与指标

同一单调时钟记录 launch、竞争段开始/结束、arrival trigger、前台请求、cancel 请求/受理、dependency 调用/实际通知登记、priority callback、owner 每遍采样、cleanup 开始/结束、资源释放、B 准入/完整结果与最终 scope drain。记录同一数组身份和 generation；任何 B 重写必须晚于 A 全部读取完成及释放。

到达分类按原始事件重算：

- `before_cleanup_start`：owner 尚未开始读取。
- `during_cleanup`：开始后、结束前。
- `after_cleanup_before_release`：读取已结束、资源尚未释放。
- `after_release`：资源已归还。

另独立记录到达时已经实际开算、至少完成一段、尚未结束的竞争者数量，以及请求到清理结束间的竞争段重叠证据。这些计数不等于处理器饱和或 CPU 核心数。目标覆盖是清理未完成且存在真实未结束的竞争工作；若严格聚焦正在读取，可另外统计 `during_cleanup`，不能混淆 queued owner 与正在执行的 owner。

主指标包括 trigger→arrival、arrival→cleanup 完成、arrival→资源释放、arrival→B 首个完整数值结果、依赖调用→首次观察到 owner 提升、各段实际耗时、完整 scope drain。若 arrival 晚于相应终点，保留有符号事件差与 phase，不能钳制成 0 后声称改善。没有 callback/采样证明时对应 metric 缺省，不伪造时间。

数值/访问协议正确性、到达覆盖、priority 传播与性能差异是独立判断。三模式不得有硬编码的快慢 PASS 顺序，也不能把 `.high` 当作更短等待、GPU 抢占或 PriML 最坏响应界。

## 首版验证门槛

CPU 协议测试覆盖读取未完成不能释放/B 复用、取消不跳过扫描、真实数组损坏被识别、竞争递推独立校验、四种到达相位及临界点、通知早到/晚到、观察者不提前 join，以及结果先完成而前台请求后到的合法路径。构建记录源码/Core/binary hashes；正常与未命中相位原始记录都必须保留。

性能计划需在代码与独立分析器冻结后另订：固定全部工作及触发参数，轮换三模式，保留全部到达分类与热态/控制成本。此阶段不合并 GPU 在途取消；只有 live CPU 到达边界和收益/无收益得到独立证据后，再决定是否接入真实在途设备阶段。

## v2 执行与独立检查（2026-10-29）

v2 唯一编译完成，12/12 项 CPU 协议检查通过；随后 root 授权并串行运行预定 K=8、三模式各一次 pilot。三次均为 during_cleanup，owner 已上报 8 遍，竞争 started/progressed/eligible=8、unfinished=4。没有按 phase 重跑或扩样。六份 v1/v2 raw、完整数值与逐身份事件、CLI/source/binary/build/recorded-run 均通过独立检查，29 项检查器测试通过。

[详细报告](../results/batch4/live-dependency-v2-smoke/REPORT.md)明确 queued-owner 与 strict-reading 覆盖、priority 传播与性能判断的区别。原 v1 与 v2 pre-pilot 快照保留不动，新增 v2-measured 快照包含三份 raw；不把跨 binary 毫秒差当作到达策略的纯因果效应。此批是 CPU-ready phase pilot，未执行 GPU 在途组合，也没有完成响应时间界或重复性能矩阵。

## 同一 v2 binary 的后续对照准备（2026-10-30）

新[矩阵设计](../experiments/swift-live-dependency-matrix/DESIGN.md)已预声明 K=0/8 × direct/hidden/forwarded，固定相同数值工作；先六 smoke，再六 Williams 区组，每格原 CLI `--trials 2` 的 trial-0 为 warmup、trial-1 为 main。40项CPU测试已首次通过，唯一bundle已准备并全量verify；本轮六smoke及36格均已首次实际exit0，逐格独立检查及[全量离线汇总](../results/batch5/live-policy-v2/summary-v1/analysis.json)通过，429输入前后不变。复制同一 binary 仍依赖原 `#filePath` 对应七个实际路径，需与已接受的 v2 身份逐字绑定；新副本只提供归档，不代替运行时实际来源。全部到达相位和覆盖失败保留；K0 仍是 live 进度触发，不是 prepared。正式 prepared/live 同实现对照及在途设备组合继续保留。

驱动与[离线汇总器](../experiments/swift-live-dependency-matrix/summarize.py)的源码准备及两人静读已完成，23项驱动、17项汇总CPU测试均已首次执行通过，记录在[独占源码快照](../experiments/swift-live-dependency-matrix/verification/cpu-tests-v1/post-execution-binding.json)；[冻结bundle](../results/batch5/live-policy-v2/bundle/manifest.json)的manifest为`83090114471a5c55a02919b5583988747705f8f496a535858523ece7bcc41501`。汇总器从预声明配置重建六区组、36 格、完整 argv 与 warmup/main 角色，使用已冻结的独立检查器重放数值和事件；实际读取指定本地副本，历史绝对路径仅用于核对执行记录。主样本与预热分别统计，九类比较只在同一区组内先作有符号差，再汇总六对；缺少 optional 观察保留 null、原因与实际配对数，不补零、不换配对、不按 phase 筛选。六smoke、正式矩阵及冻结汇总器的实际复算已完成，[图表、正式报告](../results/batch5/live-policy-v2-REPORT.md)及另一套actual-raw统计复算已完成，K8 direct/forwarded相对hidden的B结果六对均更低但各保留一条竞争覆盖未中；同一运行会话是跨进程单调时钟比较的前提，单凭两个时间数值不能另行证明没有重启。
