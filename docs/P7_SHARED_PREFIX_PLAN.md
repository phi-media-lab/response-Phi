# P7：共享冻结前缀，选中一支后停止另一支

2026-10-29。首轮限定真实模型共享状态；没有工具调用、文件副作用、崩溃恢复或 GPU 在途取消实验。GPU 执行仍由 root 排程。

当前状态：28 项轻 CPU tests 通过；root 已从独立冻结 attempt 执行 selected=A/B 各 1 个真实模型 trial，均 exit 0。两份 raw 的 28 层引用／slot 代次与完整 winner12／loser4 输出通过作者复核；独立 checker 的事件／数值／完整来源验收与 32 项针对性 tests 均通过。结果与证据入口见 [P7 首轮报告](../results/batch4/shared-branch-REPORT.md)；以下设计保留首轮范围，不据这两次 smoke 宣称性能收益。

## 从已有实现得到的边界

tiny-llm 的 [KvPrefixGenerator](../../tiny-llm/src/tiny_llm_ref/agent/branching.py) 使用 `TinyKvFullCache`：保存并物化一次 dense K/V 前缀，`fork()` 复用同一不可变前缀数组，分支续写时 dense cache 通过 concat 生成后续数组。[resume_with_steering](../../tiny-llm/src/tiny_llm_ref/agent/steering.py) 在已完成工具观察的 checkpoint 后追加 operator 消息。[select_branch](../../tiny-llm/src/tiny_llm_ref/agent/branching.py) 只返回一份已完成且评估通过的 outcome，**没有取消其他分支的语义**。

Week3 的 [TinyKvPagedPool / TinyKvPagedCache](../../tiny-llm/src/tiny_llm_ref/paged_kv_cache.py) 是另一个存储路径。pool 仅维护 used/free 页 ID；cache.release 会归还自己的全部页，没有共享引用计数。直接复制 page_ids 后分别 release 会提前释放／重复释放。append 还会先写满现有尾页，因此共享未满尾页需要 copy-on-write；本轮不实现这个机制。

因此，实验复用真实 Week3 模型、paged cache、paged attention 和本地 Qwen3-0.6B 权重，但增加独立 harness 的整页共享适配器、页引用账本和选择／停止控制器。不修改参考仓库，也不把这些能力归因于上游 agent API。dense fork 仅提供应用动机，不能假称本轮直接运行它就获得 paged fork。

## 一个可重放的具体情境

假设代理已读取一份关于“取消后资源何时可安全复用”的只读观察，现要比较两条解释路线：A 先解释资源所有权，B 先解释请求响应时间。两支看到同一段观察，各自附加不同的 operator steering 文本。用户在两支各产生 4 个 token 后明确选 A 或 B；未选分支停止新增 decode，选中分支再产生 8 个 token。

输入来自仓库内固定自然语言 fixture，经本地 tokenizer 编码后按确定规则重复／截取到目标长度，并保留原文、实际 token 列表和解码文本。它是模型状态探针，不是完整 agent loop 或回答质量评测；截取可能落在句中，不能把选择解释成自动评估后得到的最佳方案。

首轮固定：

- page_size=128，公共前缀恰好 128 token，即每层一个完整共享页。
- A/B 私有 steering 后缀各 120 token，固定 greedy argmax，忽略 EOS 做有界数值工作。
- suffix prefill 产生 output index 0，再各 decode 3 次：选择时各输出 4 token，私有缓存各写入 123 token。
- 选中支再 decode 8 次，私有缓存达到 131 token，越过一个页边界；应真实重新分配刚由 loser 归还的私有页 ID。
- 做 selected=A/B 两个固定选择 trace 对照。每次完整输入工作量相同：128+120+120+3+3+8=382 token；17 次 model forward，均构造输出 head 的惰性图，但仅 16 次实际求值 head 并交付输出 token。公共前缀只物化 KV，其 head 图不会额外求值；这不等于 17 次完全相同 FLOPs 的完整模型计算。

选择是模型线程在已完成 eval 边界观察到的确定控制事件，不是假装固定墙钟外部流量；未覆盖 GPU 在途抢占或控制线程响应时间。

## 真实存储与最小协议

每层继续使用原始 TinyKvPagedPool 的实际 storage、allocate/free、write 和 paged attention。一个轻量 pool view 拦截 cache 的页分配、写和释放，将操作关联到 owner；底层仍是同一层的 pool。cache 适配器只支持整页冻结 prefix、私有追加和整请求 release，不支持 rewind、部分共享尾页或一般 fork。

页身份使用 `(trial, layer, page_id, allocation_generation)`；page_id 仅在层内有意义。引用账本记录持有者集合，引用数由集合大小推导。分配到已 free 的同一 ID 必须增加 allocation_generation，不能让旧引用释放重用后的新页。共享页被冻结后，所有针对该页的显式 write 请求在进入底层更新 API 前拒绝。

正常 trace：

```text
prefix owner prefill → actual KV eval returned → freeze full pages
→ fork A / fork B（每层 prefix 引用数 1→2→3）
→ safe completion observation → parent owner release（3→2）
→ A/B 私有后缀和 decode，交替各产生 4 token
→ selection.requested / selection.accepted → loser.submissions_stopped
→ actual synchronize returned → loser release
   （loser 私有页归还；prefix 引用数 2→1，仍被 winner 持有）
→ winner 继续 decode，跨页时真实重用 loser 原私有页 ID
→ winner 完成，actual synchronize returned → winner release
   （prefix 引用数 1→0 后才归还；全部页与引用归零）
```

调用开始登记完成义务；eval 返回或异常后的成功同步才能消除义务。停止新增工作不消除已登记义务。释放前要求全部共享 pool 相关工作已收束；最后一个引用才调用真实 pool.free_page。使用普通 cache.release 的循环完成元数据清空，但其 pool view 会执行引用协议，不能直接绕过 view 释放底层页。

partial-tail fork 必须在新增引用／cache 图构造前拒绝，作为 CPU 负例保存。负例还覆盖：未 materialize 就 freeze、冻结页写入、共享引用未归零就物理 free、未完成 eval 就 release、stop 后再提交、重复 release、旧 allocation_generation 操作已重用页、原 append 事务失败时恢复账本。不给负例提交实际数据竞争。

## 数值与资源验收

同一模型单线程分别生成隔离 A/B 参考：公共 128-token prefix 用相同 pools-only 路径，120-token suffix prefill，再 decode 至 12 个输出 token。参考使用原始非共享 paged cache；每层 offset、chunk 和 greedy 策略相同。选中支全部 12 个 token 必须与各自参考一致，未选支前 4 个也须逐 token 一致；参考与试验的完整输入／输出列表分别保存。

每个 eval 后保存每层实际 cache page_ids/page_lens/offset、pool used/free/allocated/capacity 和引用账本。要求：

1. A/B 的页 ID 交集恰是冻结 prefix；私有页不重叠。
2. parent 释放后仍有 A/B 两个 prefix 引用；loser 释放后共享页仍 used，引用只剩 winner。
3. loser 私有页进入实际 free list；winner 跨页后确实重用该 ID，代次增加，逐 token 结果仍正确。
4. 所有显式写入都属于当前 owner 的非冻结页；共享 prefix 从冻结到最后 release 没有新写入。
5. winner/loser/parent 最终均关闭；所有层 refs=0、used=0，free+used=allocated 且无重复页 ID。
6. stop 后 loser 没有新构图／eval；选择之前已有的工作计入实际工作量，不宣称减少了固定对照之外的工作。

pool 的 MLX backing 可能因更新或增长而替换；本实验观察的是层内 slot/page ID 与有效引用，不证明同一物理 MTLBuffer、不把 free slot 称为归还系统内存。另记录 backing capacity/nbytes、storage_growths 和 copied_bytes_on_growth，不能将逻辑前缀复用量直接当成 DRAM 复制节省。

## 指标、失败与交付

schema v1，新 `experiment="tiny-shared-branch"`。原始事件包括 prefix freeze、fork、parent transfer、每步 graph/eval/交付、selection、stop、同步、每层引用变化／分配／释放、winner 续算和最终 drain。token 交付时间是 token 求值后写入实验消费者列表的主机观察；选择反馈、loser 私有页可复用、winner 下一个 token 的时间分别报告。总时间包含 host 构图、同步、事件和页快照成本，不称纯 GPU 吞吐。

异常保留原始 traceback、已完成 token／事件／页状态。同步失败不能伪造 ref0、free 或 drained，也不能继续下一 trial；保留尚存活 cache/pool 引用至进程退出。模型与来源 hash、tiny dirty 状态、实际导入模块、缓存模型 snapshot、选择 trace 和源码版本均归档。不得下载模型或修改依赖。

代码独立于冻结实验；可只读复用 tiny-interleave 的固定 I/O／模型辅助函数并核对 hash，不能导入其调度循环改变语义。先 CPU 协议与 fake backend 测试；root 审阅后再安排小规模真实 GPU smoke。共享前缀、拒绝非法释放和 token 一致性是首轮完成门槛，不预先承诺性能收益。外部工具意图、动作准入、共享副作用和崩溃语义另立实验。

## 审阅后补充与运行来源绑定

已发现并修复：若资源快照自身持续失败，清理中的备用快照也可能抛错并丢失整个 trial。备用诊断现在单独捕获错误，保留已有事件／输出、原错误、`diagnostic_errors` 与 quarantine；不能因诊断失败伪造 drained。旧 17 项 CPU 记录与独立 `verification/review-v1` 原样保存，当前 revision 的 CPU 记录独立建立。

证据边界进一步明确：完成见证布尔值由可信 harness 传入，不是防伪 capability；默认 stream 同步只确认已提交工作，不强迫全部惰性图执行。上游 forward 内的 attention 元数据 `.tolist()` 可以发生物化，因此不能称整个 forward 为完全纯构图。逐 token 匹配不等于对完整 KV 的逐字节等价证明。

`attempt.py prepare` 为每个选择 trace 单独复制源码、fixture、固定 helper、tiny Python 与本机 native extension／metallib／对应源码，生成包含实际内容 hash 的 manifest，并将 payload 设为只读。prepare 不 import MLX 或加载模型；权重先保存本机 HF content-address 声明。实际 `execute` 只能运行该 payload 的 runner，绑定 manifest SHA、固定参数和导入路径，运行前后完整核对外部模型／MLX 依赖的 bytes hash。NumPy／Transformers 仅固定包元数据，不冒称整个传递环境均已封存。

每次执行以 copied `run_recorded.py` 保存命令、退出码、stdout 与 stderr，exclusive 创建，失败不覆盖旧 attempt。普通 working runner 被拒绝。只读权限和前后哈希是意外修改检测，不构成同一用户权限下的安全沙箱；GPU 仍只由 root 明确调度。
