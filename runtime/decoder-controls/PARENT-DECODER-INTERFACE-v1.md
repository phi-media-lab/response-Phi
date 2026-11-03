# SRC1 B 首次 decoder 控制接口

状态：源码准备，未编译、未运行、未 import 候选，也未生成任何源或变异输入。现有 frontend、15-case oracle 和 Program.swift 保持原字节。新入口只依赖 RuntimeCore；Package 的 product `src1b-decoder-cpu` / target `SRC1BDecoderCPU` 由 root 注册，源文件为 `Main.swift` 配合 `@main`。

## 固定观察人口

Swift 按以下顺序做 **20 次实际 production decode，5 次实际 configuration API 调用**。前五条全部必须 decode 成功；后四条是在指定配置 API 才拒绝。然后检查原有 15 个变异。

| 顺序 | 原始 exchange | configuration | 精确结果 |
| --- | --- | --- | --- |
| 1 | parent-golden | parent | 接受，domain=hostParent |
| 2 | parent-golden | leaf-configured=true | `external demand requires mlxLeaf domain` |
| 3 | parent-empty | parent | `parent service configuration requires one call_service occurrence` |
| 4 | parent-service-only | parent | `parent service configuration requires one call_tool occurrence` |
| 5 | child-empty | parent | `parent configuration requires hostParent domain` |
| 6–20 | parent-golden 的原 15 个独立变异 | 无 | Main.swift 固定 ID/message 表，与原 `parent_decoder_mutations.py` 一致 |

比较完整 ProtocolError JSON：`phase=protocol, code=invalid-compiler-exchange, path=[], message=固定原文`；同时比较错误出现于 decode 还是 configuration。manifest 不允许另给任意 expected 值。`command-direct-copy` 保持已审 S2 最早边界 `source-directed direct usage`，不为到达后续守卫修改 production decoder。

首条 golden 额外核对 decoder 返回的四项只读 metadata（domain、原 source bytes hash、canonical source hash、实际 response hash）。四个配置负例也核对对应 metadata，不把 decoder 错误冒充配置拒绝。

## 新 Python 准备器

调用使用固定 Python 与 `-I -S -B`。下列 `ABS_FROZEN_RUNTIME` 指完整冻结 runtime 根，包含原 frontend、`Sources/RuntimeCore/Program.swift` 及新 helper；不能把仅有 documents/Program.swift 的前端测试冻结目录直接当作该根。

```text
FIXED_PYTHON -I -S -B ABS_HELPER sources
  --source-root ABS_FROZEN_RUNTIME --output-dir ABS_NEW_SOURCES

FIXED_PYTHON -I -S -B ABS_HELPER mutations
  --source-root ABS_FROZEN_RUNTIME --source-manifest ABS_NEW_SOURCES/manifest.json
  --exchange-index ABS_EXCHANGE_INDEX --output-dir ABS_NEW_MUTATIONS

ABS_BUILT_SRC1B_DECODER --manifest ABS_NEW_MUTATIONS/manifest.json
  --output-dir ABS_NEW_OBSERVATIONS
```

`sources` 只导入 hash 固定的 `parent_fixture.py`，构造三个源文件：`golden.json`、`empty.json`、`service-only.json`。empty 是返回 Unit 的普通闭源；service-only 在实际 plain scope 内构造 Tokens8 并调用服务，结果为 Word。后两者特意缺少正式父配置要求的命令，其静态合法性和实际 ready 响应必须由后续 frontend 首次执行验证，不由 builder 自称。child-empty 复用同一 empty 文件，但必须通过原 child exporter 独立实际导出。

输出 source manifest schema=`src1b-decoder-sources-v1`，包含 `construction_complete`、固定三项 `sources:[{id,source:PIN}]`、所有读入 `inputs`、`constructor`、`program_source_basis`、`source_root` 和明确未启动 compiler/decoder/runtime/model 的字段。PIN 始终为 exact `{path,bytes,sha256}`，绝对路径。Program basis 固定 63836 B / `e7b982727e272bf34bece17c8554427a3ba6adfb51bf57ac6be0772272ff90c9`；oracle 固定 6539 B / `90dd52dae21c9383ea23638a8d0cea777fe3c5a6d2917b09ef04325ecaad01d5`。

## 四次真实导出及 exchange index

root 另行源码审阅、冻结并运行 bounded export runner。每次使用 source 文件的完整原 bytes 作为 stdin，固定 cwd=`ABS_FROZEN_RUNTIME`。固定 child argv 为：

```text
FIXED_PYTHON -I -S -B ABS_FROZEN_RUNTIME/frontend/export_parent.py
FIXED_PYTHON -I -S -B ABS_FROZEN_RUNTIME/frontend/export.py
```

第一行用于前三项，第二行只用于 child-empty。每项有新独占输出目录，保存实际 stdout/stderr 原 bytes；不能自行编造 ready JSON。失败完整保存并停止，不自动执行后续项或重跑。

index 的 exact 顶层字段如下（尖括号是接口占位，不是已产生数据）：

```json
{
  "schema": "src1b-decoder-exchange-index-v1",
  "source_root": "<ABS_FROZEN_RUNTIME>",
  "source_manifest": "<PIN of source manifest>",
  "exchanges": [
    {"id":"parent-golden","exporter":"parent","source":"<golden PIN>","response":"<actual stdout PIN>","stderr":"<actual stderr PIN>","run":"<run PIN>","actual_tool":"<actual outer tool record PIN>"},
    {"id":"parent-empty","exporter":"parent","source":"<empty PIN>","response":"<actual stdout PIN>","stderr":"<actual stderr PIN>","run":"<run PIN>","actual_tool":"<actual outer tool record PIN>"},
    {"id":"parent-service-only","exporter":"parent","source":"<service-only PIN>","response":"<actual stdout PIN>","stderr":"<actual stderr PIN>","run":"<run PIN>","actual_tool":"<actual outer tool record PIN>"},
    {"id":"child-empty","exporter":"child","source":"<same empty PIN>","response":"<actual stdout PIN>","stderr":"<actual stderr PIN>","run":"<run PIN>","actual_tool":"<actual outer tool record PIN>"}
  ]
}
```

每个 run record 必须有下列字段；可以保留 wrapper 的额外原观察，不要求删减：

- `schema="src1b-decoder-frontend-export-run-v1"`；`entry` 为上述 ID；`command` 为实际固定 child argv；`cwd` 为冻结 runtime 根。
- `source=PIN`，对应实际送入 stdin 的完整源文件；`files.stdout.log=PIN`、`files.stderr.log=PIN`，分别与 index 的 response/stderr 完全相同。
- `returncode=0`（整数）、`passed=true`、`direct_child_reaped=true`、`process_group_absent=true`；`timed_out=false`、`interrupted=[]`、`signals=[]`、`failure=null`。
- `budgets_seconds={"outer":60,"work":58,"term":1,"kill_reap":0.5,"report_reserve":0.5}`；`0 <= work_elapsed_seconds < 58` 且 `work_elapsed_seconds <= elapsed_seconds < 60`。
- `outer_zero_requires_post_save_post_flush_total_budget_gate=true`。run 的 passed/elapsed 仍为末门前观察，必须有下述真实 outer0 才可消费。

每个 actual tool record 由 root 在真正终态后保存，字段至少为：

```json
{
  "schema": "src1b-decoder-frontend-export-actual-tool-v1",
  "entry": "<fixed ID>",
  "run": "<exact run PIN>",
  "child_command": ["<same actual fixed child argv>"],
  "tool": "<actual tool name>",
  "initial": "<original launch/session result when yielded>",
  "observation": {"exit_code":0,"chunk_id":"<actual terminal chunk>","output":"<verbatim original tool output>"}
}
```

`initial` 无 yield 时可以省略。root 应保真实完整 tool 结果与精确外层 wrapper 命令；新准备器和 Swift 均核对 actual tool 的 schema、entry、run pin、child_command 和实际 terminal exit_code=0。它们不能凭文件格式自行证明外部 tool 记录真实，因此 root 原始调用和后续独立实际审阅仍是证据链的一部分。

## 变异及 Swift 原观察归档

`mutations` 先重核 source manifest、四项 run/tool/原 stdin/stdout/stderr 与嵌入的源 bytes；逐项保存原 bytes 副本后，才 import 固定的纯 oracle。oracle 每例 fresh deepcopy；helper 不另写新的 mutation 语义。

变异输出 manifest schema=`src1b-parent-decoder-inputs-v1`，固定 `mutation_count=15, observation_count=20, configuration_calls=5`。每例有 source.json、frontend.stdout、mutation.json。metadata 保存完整 wire difference（含所有 appended DAG 节点）、原始 static prefix 与 appended 数量、源/响应/实际 exit 来源。没有将多个 wire 变化误记成单字段，也没有修改原 source 以掩盖 tampered exchange。

Swift 独立复算 wire difference、检查原 static prefix、固定 oracle、四份实际导出来源，以及所有输入 pins。每条实际调用前保存 source/stdout；调用后先保存完整 observation，再 assert 匹配。首个不符写 first-failure.json、退出 1；不执行后续案例。成功需 20 decode/5 config 的实际计数和所有输入末次 hash 一致，再写 result.json。

新 helper 与 Swift 本身不承担 OS 生命周期/总预算监督；root 的独立 bounded runner 必须覆盖文件读写、末次 hash、报告 close/flush 和实际 outer0。准备、四次导出、变异和 Swift 观察均先审源再首次执行，分别保原首次失败。不能用已有 Python 70 方法通过替代新 Swift 结果。

这些控制没有创建 parent/child Ledger、原 requester、工具 FileHandle、worker 或模型，不能声称响应真实交付、HostClose 完成、十个 B 情境通过或完整语言健全性。
