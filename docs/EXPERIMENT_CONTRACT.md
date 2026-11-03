# 取消与资源转交实验契约

## 情境

A 正在处理长输入或分块数值计算，持有私有缓存页或物理 buffer。用户取消 A 并提交 B。有限容量条件下，B 必须等 A 归还指定资源；宽松容量条件是反事实对照。首版采用保守回收：停止 A 的后续相关提交，并确认所有相关设备访问完成后，才无条件归还资源。

取消受理仅表示控制器接受请求；停止新增提交有独立的线性化边界。已经提交的计算由原持有者或显式接管者负责收束。共享权重不算待回收的私有资源。

## 结果格式

每次运行输出一个 JSON 对象，顶层含 `schema_version: 1`、`experiment`、`provenance`、`config`、`trials`。`provenance` 包含平台、软件版本、源代码 revision 或哈希以及模型／kernel 标识。失败应保留到输出并返回非零退出码。

每个 trial 含 `trial_id`、`config`、`events`、`metrics`、`checks`，可增加 `observations`。事件至少含 `name`、`t_ms`（同一 trial 的单调时钟起点）、`request_id`；可增加 `resource_id`、`generation`、`submission_id`、`details`。关键事件如下，未实现的事件不得虚构时间戳：

| 事件 | 含义 |
| --- | --- |
| `a.submit` | 一个 A 计算块真正提交给执行路径；需说明边界是 MLX 求值调用还是 Metal commit |
| `cancel.requested` | 控制端发出取消 |
| `cancel.accepted` | 控制器受理取消 |
| `b.requested` | B 被提交给系统，是 B 结果延迟的起点 |
| `a.cancel_observed` | 计算持有者观察到取消 |
| `a.submissions_stopped` | A 不会再提交相关工作 |
| `a.device_completed` | 有设备计时证据的最后相关访问完成，仅在确有该证据时使用 |
| `a.completion_observed` | CPU 确认所有相关设备工作完成 |
| `a.cleanup_started` / `a.cleanup_finished` | CPU 收尾区间 |
| `resource.released` | 资源满足本实验声明的复用契约，归还池或额度 |
| `b.admitted` | B 取得所需资源与执行准入 |
| `b.first_result` | 数值探针的结果可用 |
| `b.first_token` | 真实模型首 token 求值并交给实验调用者 |
| `trial.drained` | 所有任务及设备访问结束，资源记账恢复 |

GPU 硬件时间与 CPU 回调时间分开存储，只有确认时钟域相容或显式校准后才置于同一时间轴。`eval` 返回是主机完成观察，不冒充精确 GPU 完成时刻。

## 指标

- `cancel_ack_ms = cancel.accepted - cancel.requested`。
- `cancel_observe_ms = a.cancel_observed - cancel.requested`。
- `reclaim_ms = resource.released - cancel.requested`。
- `b_admission_ms = b.admitted - b.requested`。
- 真实模型 `b_ttft_ms = b.first_token - b.requested`；数值探针使用 `b_first_result_ms`。
- `release_to_admit_ms` 仅在容量依赖成立且事件有序时报告。
- 同时报告取消后完成的块数／token 数、提交量、峰值在途量、资源高水位、正常无取消吞吐和数值正确性。

各延迟独立计算；不得把可重叠阶段简单相加。B 已不依赖 A 的对照中，不要求释放早于 B 准入。

## 正确性门槛

1. 停止提交边界之后不存在 A 的新提交。
2. 无条件归还发生在相关访问完成被确认之后；已知完成发生在取消之前也应如实记录。
3. 同一资源代次不能被重复归还；可用、保留、在途的计数保持一致。
4. 容量受限时，B 的准入依赖满足；宽松容量时取消此依赖。
5. B 的数值／token 结果有独立或隔离运行参考。结果错误不是性能改进。
6. 异常、取消、超时都必须有持有者完成收尾。超时不授权回收仍在使用的资源。
7. 最终没有未完成设备工作、遗失所有权或未归还容量。

不安全的提前归还通过状态检查的负例测试，不实际执行 GPU 数据竞争。

## 证据层级

- 逻辑事件测试证明被测试路径满足约束，不能证明硬件时延。
- Swift／Metal 探针验证真实 buffer 使用与转交，不称为完整模型推理。
- tiny-llm 基准记录真实 KV、prefill、首 token，新增控制器的行为不归因于上游原有功能。
- 有限样本支持机制比较与反例，不能直接证明最坏响应界或所有调度下的安全性。
