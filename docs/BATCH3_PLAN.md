# 第三批：持续输出与完成义务接口

第二批已确认完整模型工作量下的 B 提前执行，以及 prepared/device-ready 条件下的 Swift 依赖差异。第三批先将应用观察点扩展到持续输出，再固定数值策略检查接口是否保留完成义务。两项不合并为一个运行时，不用接口标签解释模型调度收益。

## 持续 decode 与长 prefill

A prompt 64 token、prefill 首 token 加 40 次单 token decode。B prompt 1024 token、一个首 token，固定在 A 第 4 个 decode token 交付之后到达。whole/32/128/512 四种 B 分块策略均使用同一“B 一块、A 一步”轮转规则，完整完成 1128 个模型输入、42 个输出和 42 次 head 求值。

五个随机区组，每组四策略各一次，每项独立进程、一次完整预热。固定 seed、完整 token 参考及到达 trace。到达按程序进度锚定，不是固定墙钟开放环流量。没有无 B 的性能基线，本轮先检验策略间取舍。

重点同时报告：

- 每 trial A 最长交付间隔与间隔中位数，保留全部 token 序列；不要把一个 trial 的 40 个间隔算成 40 次独立重复。
- A 单次 decode 调用服务时间、任意请求 step 完成间隔、全局 token 交付间隔，四种口径分别重算。
- B TTFT、全部工作完成时间、模型输入/输出吞吐及页槽快照高水位。
- B 同 chunk/whole 隔离参考，A 完整输出参考，live 页不重叠，cache offset 与已处理输入相符，最终页和预留归零。

whole 可能把 A 停顿集中成一次，细分块可能将其分散成多次，同时延长 B prefill；这些只是待验证假设，不写成必须通过的性能条件。固定输入与 head 次数也不等于固定 FLOP 数、物化或主机记录成本。

入口为 `scripts/run_decode_matrix.py`，参数在 `configs/batch3-decode.json`；结果放在 `results/batch3/decode`，来源哈希在整个矩阵中必须保持不变。既有 tiny-interleave 帮助函数及 tiny-llm 参考目录只读。

## Swift 完成义务接口

独立 package 共享数值 backend、kernel、扫描与直接等待策略，只对照 manual 显式 drain 和 scoped 持有 registry 后自动 async close。scope 退出须完成真实设备访问和必要 CPU 收尾，即使业务调用者丢弃结果、取消或抛错；禁止用析构冒充异步完成。

误用集分别记录：丢弃句柄、提交后异常/取消、早归还/重复归还、旧代次、资源能力逃逸、合法只读值逃逸、取消一个共享结果等待者、continuation 再包装、不可复制约束，以及尚未支持的共享设备读租约/嵌套异步清理。静态约束必须保存真实编译诊断，不能从不可复制推出必须 await。

先完成 CPU 协议与 compile-pass/fail，再由主任务统一安排真实 Metal smoke 和相同工作量对照。错误触发在设备提交之前被拒绝，不故意执行 GPU 数据竞争。接口必须说明漏检、误报和组合边界；单纯改变语法不算新增保证。

第三批不取消尚未完成的 P3 自然到达/在途设备组合、P5–P8 实验与形式化门槛。
