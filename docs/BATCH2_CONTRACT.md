# 固定工作量吞吐与请求交错契约

第二批补充首批缺失的无取消成本对照，并区分请求已经准入和实际取得计算机会。保留首批 JSON 外壳：`schema_version: 1`、`experiment`、`provenance`、`config`、`trials`。每个 trial 保存单调 CPU 事件、原始资源观察、指标和正确性检查。无取消实验不得为兼容旧分析器而制造取消事件。

独立分析入口为 `scripts/analyze_batch2.py`，首批 `scripts/analyze.py` 保持原状。两种实验按来源指纹与完整配置分别聚合；无效 trial／文档保留并排除出性能汇总。少于 100 个有效样本只标为 pilot，不能产生稳定尾延迟或最坏界的结论。

## 真实模型交错

`experiment = tiny-interleave`。固定 A 的完整 prompt，B 在 A 首次求值附近由控制线程提交。整个模型和缓存仍由单个计算线程管理；交错指在已完成的计算块之间切换请求，不表示 GPU 硬件并行。

配置包括 `policy`（`serial` 或 `interleaved`）、`capacity`（`constrained` 或 `relaxed`）、`b_present`、`prompt_length`、`b_prompt_length`、`chunk` 和 `intermediate_eval`。A-only 基线不产生 B 事件或 B 指标。

`pools-only` 模式在 A 的中间块只物化 KV，最后一块才求值 token head；`token-and-pools` 每块都求值 head，用于与首批路径对照。每块开始和完成事件中的 `head_evaluated` 必须符合配置；`evaluated_token_heads` 从完成事件加 B 的 head 独立重算。固定 prompt token 数不等于不同 chunk 下的完全相同 FLOP 数。相同 chunk 和求值模式下的 policy／capacity 比较保持数值步骤一致。

关键事件如下：

| 事件 | 含义 |
| --- | --- |
| `work.started` | A/B 工作计时起点，A 首 token 延迟也从此开始 |
| `a.chunk_started`、`a.chunk_completed` | 带 chunk 编号、连续 start_offset、token 数和 head_evaluated 的实际 A 工作 |
| `scheduler.boundary` | 已完成块之后的调度观察，包含剩余 A token、B 到达／准入／完成状态与选择 |
| `a.first_token`、`a.completed` | A 的最终 token 和完整 prompt 工作完成 |
| `b.requested`、`b.admitted`、`b.compute_started` | B 的到达、逻辑准入和实际计算开始 |
| `b.first_token`、`b.completed` | B 的真实 token 和完整 prompt 工作完成 |
| `a.completion_observed`、`b.completion_observed` | 主机确认该请求相关求值完成 |
| `resource.released` | 相应请求的缓存句柄和逻辑预留被归还 |
| `work.completed` | 所有配置要求的 A/B 工作完成 |
| `trial.drained` | 控制线程、设备工作与资源记账最终收束 |

`work_ms = work.completed - work.started`；吞吐为完整 prompt token 总量除以此区间。A-only 分子只有 A，双请求分子为 A+B。`b_ttft_ms` 从 B 到达计时，`b_admission_ms` 为到达至准入，`b_ready_wait_ms` 为准入至计算开始。不能将整体 throughput 作为 decode tokens/s。

分析器核查 A 的 chunk 覆盖完整连续 prompt，并对照保存的隔离 A/B 首 token。受限容量下 B 准入不得早于 A 释放；serial 策略下 B 计算不得早于 A 完成；interleaved 策略在实际存在已到达、已准入且 A 仍有工作时，应先推进 B，再恢复 A。没有实际机会的晚到或整段 prefill 样本应保留，不能因为没有提前完成就判性能失败。

B 的 `compute_started.tokens` 与 `completed.completed_tokens` 必须覆盖其固定 prompt；配置中的长度本身不算执行证据。每个 A 块必须对应求值开始、完成和调度边界；保存的主机求值窗口必须与这些事件一致。B 到达／准入／完成的边界状态从实际生命周期重算，不能用人为的 false 状态隐藏一次真实可交错机会。主机求值窗口内到达仍不证明当时 GPU 正在运行。

池页计数、最终预留和每层 A/B 活跃页槽从原始观察复核。页槽不重叠和 token 参考一致不等于所有 KV 元素已被独立逐项证明；逻辑配额也不是物理 backing 的硬上限。

## Metal 固定工作吞吐

`experiment = swift-metal-throughput`。固定 `total_chunks × elements × iterations`，窗口仅改变实际滚动在途提交上限。所有命令在同一队列有序使用同一 buffer，保持相同递推结果；完成回调归还窗口 credit，从而允许继续提交。

`work.submit` 是紧邻 Metal commit 前的 CPU 观察边界，具有唯一 `submission_id`。`work.command_completed` 为命令完成回调；`work.completion_observed` 表示全部回调与窗口 credit 已处理完毕。吞吐区间从首个 `work.submit` 到 `work.completion_observed`，包含持续提交、主机调度和完成通知成本。全量数值校验由 `work.validation_started`／`finished` 单独计时，不进入吞吐区间；`full_trial_ms` 另含校验和最终释放。

主机窗口还记录 `work.window_reserved` 和 `work.credit_returned`，其 `details.outstanding` 为在窗口锁内观察的计数。编码中的命令已消耗 credit，却可能尚未出现 `work.submit`，所以 credit 峰值与提交／完成事件重放的峰值分别检查，不能强行视为同一个数。两者都不得超过窗口上限。

每个 credit 必须先保留再提交，只能在对应命令完成之后归还；全部 credit 归还后才允许宣告整体完成观察。不能只凭最终余额为零，就忽略执行期间的错误复用。

每条提交必须恰有成功完成记录。最终在途计数与持有者为零；相关完成观察和全量数值校验之后才能无条件归还 buffer。全量 buffer 的实际 SHA-256 与独立 CPU 参考 SHA-256 一致，并记录检查的元素数。原始 GPU 起止时间独立存放，不用 CPU 回调时间冒充 GPU 结束时间。

指标包括 `work_wall_ms`、`total_element_iterations`、`element_iterations_per_second`、`commands_per_second`、`gpu_execution_sum_ms`、`gpu_span_ms`、`full_trial_ms`。element iteration 是本探针的数值工作单位，不能改称模型 token 或推理吞吐。

## 失败与证据边界

分析器拒绝顶层失败、错误、缺失 trial、失败 warmup、指标与事件不符、缺失完成、重复命令、窗口越界、重复资源释放和错误参考结果。它重算可用的原始数据，并保留所有失败信息；性能变快不是 PASS 条件。

Tiny 保存的 warmup 使用同一套事件与资源规则独立检查，结果单独保存，不进入性能样本；Metal 当前保存 warmup 的检查汇总，分析器明确检查该汇总，并不声称重放未保存的 warmup 轨迹。

这些检查验证保存的执行轨迹及其来源一致性，不能证明未观察到的所有设备调度。吞吐、响应与资源占用应一起解释，最终仍需更多相位、负载与重复样本。
