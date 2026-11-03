# Response Lab

在 Apple Silicon 上研究本地 LLM 请求的取消、设备完成、资源转交与响应性。Swift 是主要语言与接口实验载体；tiny-llm 提供真实模型负载。完整路线见 [ROADMAP.md](ROADMAP.md)，当前证据见 [STATUS.md](STATUS.md)。

最新执行增量（2026-11-02，完整P01首次回放已启动）：v8自身完整P00已正式验收，完整P01装配及[修订v2具体派生独审](experiments/source-response-parent-host-close-v1/review/t01-graph-P01-concrete-derivation-independent-v2/REVIEW-v1.json)保持。新增[root同实物读回](results/source-response-parent-host-close-src1b/native-tool-T01-graph-P01-first-root-concrete-observation-v1.json)首次9394bf/session55889→5ecd27/0：1126小文件137459089B全hash、六个明确大件继承实际三向内容证明并核当前身份；19完整目录块、325基线物理与328原始路径/alias、完整45SHA报告条件保持。容量首实际d5c41e/0已完成：完整正例报告上界27557812B，低于原32MiB5996620B；55X与735完整运行输入不变。[v3最终输入准备](results/source-response-parent-host-close-src1b/native-tool-T01-graph-P01-final-input-preparation-second-actual-tool-v1.json)已e504f2/0，四个文件累计750130B、实际读取145552577B，原14/7/23/10接口及九项调用差异保持；[最终运行输入](results/source-response-parent-host-close-src1b/native-tool-T01-graph-P01-final-inputs-root-launch-v1.json)不是语义通过。首次v2准备e1c1ce/1在Python.framework5452528B被普通other每件4MiB门误拒时停止，outputs_created为空，原失败保留；v3仅对同P00三份固定解释器镜像按确切pin/size接线，other18MiB/额外64MiB/总256MiB与其他4MiB门不变。完整启动前b953cc/0再核1143元数据、325/328身份、54staged文件与19块。完整P01候选现已[首次启动](results/source-response-parent-host-close-src1b/native-tool-T01-graph-P01-first-actual-tool-inflight-v1.json)：0b2c77/session52534，最近3987f5仍为同一活动会话，尚无终态结果。保持原work1800/total1840与32/16MiB报告限制，不减少原9345审计、455检查或45允许SHA条件；完整actual observer源码在准备，实际P01正例尚未验收。容量wrapper首次发布e648f2因并行准备时合同pin暂变而拒绝，原合同恢复后同stdout原call b7cc05/0发布，计算未重跑；所有旧失败保持。下一步完成同一会话及完整独审，再贯通真实token→父程序→工具后继与宿主关闭责任。累计12/64指定负例（前11为v6，F06-02为v7）、后13工具案、29部分创建案、10父子组合、P3/P8、GPU/AMX/device-async与统一PL全目标仍未完成。本批无新模型/GPU，离线核验与回放耗时不作agent性能结论。

[SRC1 A 源响应合同](results/source-response-contract-src1/REPORT.md)已完成[有限阶段验收](results/source-response-contract-src1/phase-a-stage-acceptance-v1.json)：源程序显式登记真实B，主结果可为Unit，交付等待原main实际join及完整源安全边界，原宿主继续关闭。[B父程序组合](results/source-response-parent-host-close-src1b/PROGRESS.md)已完成前端／parser／decoder、S3报告修复构建、六类归档探针、修复T01输入准备，以及基础reader27方法、探针11方法／12情境和监督器26方法的实际验收。第二轮大原件现已[实际独立验收](results/source-response-parent-host-close-src1b/native-tool-T01-large-reader-second-root-actual-acceptance-v1.json)：三原件585080023字节、13阶段及四组完整原值逐字节比较完成，外层／子进程均exit0，41输入保持、6输出和进程／句柄收尾核准；内层1050.678630秒、峰值RSS40927232字节。首次外层EPERM失败仍保留，不倒写成功。[S6语义oracle源码已接受](results/source-response-parent-host-close-src1b/native-tool-T01-oracle-s6-root-source-acceptance-v1.json)，补齐S5遗漏的 returned→issued_B 顺序；原307方法保持，新增2个定向反例，共309。[309方法首次启动失败](results/source-response-parent-host-close-src1b/native-tool-T01-oracle309-first-failure-root-acceptance-v1.json)：macOS Python的bin启动路径与实际orig_argv中的Resources image不同，bootstrap命令校验在候选模块导入前拒绝，实际执行0方法；58输入及7输出完整，原失败和收尾已独审。[image绑定修订与第二轮启动](results/source-response-parent-host-close-src1b/native-tool-T01-oracle309-second-root-launch-v1.json)已接受；[第二轮实际回执](results/source-response-parent-host-close-src1b/native-tool-T01-oracle309-second-actual-tool-v1.json)为exit1，309方法全执行，262通过／47error，未超时。[root原件观察](results/source-response-parent-host-close-src1b/native-tool-T01-oracle309-second-root-direct-observation-v1.json)核准63输入保持、2500输出及进程收尾；Owner38在完整fixture超过2MiB时提前报错，资源9因循环变量覆盖资源字典而TypeError。整轮不通过，[完整失败证据已获独审与root接受](results/source-response-parent-host-close-src1b/native-tool-T01-oracle309-second-failure-root-acceptance-v1.json)；[S7两处修复源码](results/source-response-parent-host-close-src1b/native-tool-T01-oracle-s7-root-source-acceptance-v1.json)已接受，全部309方法保持。V4身份期限末端缺口另版修复后，[V5完整源码／合同](results/source-response-parent-host-close-src1b/native-tool-T01-oracle-v5-runner-root-source-acceptance-v1.json)与[24项期限测试实际验收](results/source-response-parent-host-close-src1b/native-tool-T01-deadline24-root-actual-acceptance-v1.json)均已完成：真实outer／driver exit0，24方法全通过，14次精确迟到拒绝、3次whole-main函数返回1、7正例及4真实tinychild收束分别核准；26输入保持，231输出含2symlink。模拟327.25秒不作真实时延。第三轮S7完整309已[实际独立审查并获root验收](results/source-response-parent-host-close-src1b/native-tool-T01-oracle309-third-root-actual-acceptance-v1.json)：309/309通过，真实outer／driver exit0；此前受阻的Owner38和资源9均已到达预期判断。64输入、2688物理输出及进程／句柄收尾核准，五处早或复合gate覆盖限定保留；旧失败不倒写。

[C2控制器82项测试已实际独立审查并获root验收](results/source-response-parent-host-close-src1b/native-tool-T01-reader82-first-root-actual-acceptance-v1.json)：82/82通过，47次拒绝、16对完整变异、23个真实tinychild收束及6个启动前拒绝核准；38输入与12309输出保持。错误保留容量及部分内存断言的证据限制随验收保留。

[P1完整reader源码／控制冻结已验收](results/source-response-parent-host-close-src1b/native-tool-T01-reader-P1-root-actual-acceptance-v1.json)，7份副本逐字节一致。233项启动前输入及单次T01启动条件已获[实际复核与root接受](results/source-response-parent-host-close-src1b/native-tool-T01-repair-first-root-launch-v1.json)。[修复T01已首次实际正常终止](results/source-response-parent-host-close-src1b/native-tool-T01-repair-first-actual-tool-v1.json)：真实c399ab／exit0，Swift与wrapper均0，无timeout；work46.991805秒、保存total47.504249秒，原2／50／55／60秒预算保持。[原控制／EOF／回收窄门已独审及root接受](results/source-response-parent-host-close-src1b/native-tool-T01-wrapper-root-actual-P4-launch-acceptance-v1.json)，44个文件共592424480字节、最大494768905字节；完整图语义尚待读取。[P4已实际保存233项运行后输入](results/source-response-parent-host-close-src1b/native-tool-T01-reader-P4-actual-tool-v1.json)，真实0a5db6／exit0，完整233项before／after／当前身份一致。[P4与P5实际均已独审并获root接受](results/source-response-parent-host-close-src1b/native-tool-T01-reader-P5-root-actual-acceptance-v1.json)：19字段ReaderInputs、19字段invocation、237管理输入与44原件全量流式哈希核准，原5源码／8角色／3先决保持。[完整reader预检发现原empty-stderr规则与正常生产诊断冲突](results/source-response-parent-host-close-src1b/native-tool-T01-reader-preflight-stderr-conflict-v1.json)：原8902字节是正常BRS1服务记录。原S7 P6未执行，不能记作运行失败；单独S8语义修订与C3运行后来源绑定正在实现，旧P1/P2/P4/P5和同一T01原件全部保留。修订后仍按1840秒总预算验证完整语义；完整图mutation的[显式I/O参数方向](results/source-response-parent-host-close-src1b/native-tool-T01-full-graph-mutation-root-design-disposition-v1.json)已获root接受，适配S2已通过源码独审，尚无实际验证；须另版重绑未来接受的S8，原完整baseline与实际图变异执行未完成。后13工具案、29部分创建案、十个完整父子组合与真实模型token到工具参数的数据流仍未完成；本批新增一次CPU stub worker工具单案，无新模型/GPU运行，完整SRC1／P3／P8与总体目标继续进行。

[JRD1 安全结果交付与后端收尾](results/joined-result-delivery-jrd1/REPORT.md)已完成[有限阶段验收](results/joined-result-delivery-jrd1/jrd1-stage-acceptance-v1.json)：CPU正常／晚到故障／watchdog路径与reference→C1→J1→J2→C2五次真实模型会话全部完成。原main实际join、源义务全部Safe后交付同一请求的B token，原宿主随后仍完成关闭。两组原策略入队→客户端为1106.116250／1101.833542 ms，候选为65.205125／65.276833 ms；候选交付后仍有1257.478250／1041.449833 ms才关闭。十次prefill均72／271、280层ACK及原任务／进程IO收束通过；四格56端点、72区间、36配对差独立一致，正式报告通过、root重核435保存文件。C2模型启动前的路径错误及各只读辅助首错保留，没有模型重跑。每模式n=2，仅支持本批交付与责任分离，不建立稳定性能／一般TTFT／响应界；源级契约、组合证明、完整P3／P8与整体目标继续进行。

[EBD1 外部请求与结果交付](results/external-demand-ebd1/REPORT.md)已完成[有限阶段验收](results/external-demand-ebd1/ebd1-stage-acceptance-v1.json)：六项 CPU 门槛与五次真实模型会话均闭合，四个完整独立 B8 经受检源、真实 MLX 与同一客户端交付271；共十次prefill、280层ACK，原任务与资源归还义务保持。主源 Stable 结果在到达后约65–77ms就绪，客户端却在约1.11–1.16秒收到；同域 backend close 区间约1.05–1.09秒。K8提优格归还和token更晚、客户端反而更早，n=1不支持稳定收益。reference首审计预算表示错误与同raw修订完整保留，模型没有重跑。下一步独立研究已join的Stable结果交付与最终后端关闭的分离，保留原收尾责任；P3、完整P8及整体目标仍在进行。

核心情境：用户取消正在占用 KV 资源的请求 A，立即启动 B。分别观察取消受理、资源安全归还和 B 首 token，解释它们之间的依赖。实验同时考察正确性、响应延迟、吞吐和取消后的无效工作。

持续输出／长输入结果见[八形状综合报告](results/batch5/decode-cost-eight-shape-synthesis-REPORT.md)：288次主样本与288次预热，比较A输出、B首token与完整工作取舍。

语言侧 [A3 一般Stable发布结果](results/core-language-runtime-stable-a3/REPORT.md)已完成：GPU计算返回纯值闭包或带上游身份的结果包，原资源释放后，受检源仍能实际CALL/UNPACK并使用完整结果。两正例各首次成功，共4次GPU提交、32次成功LOAD；三个静态负例准确拒绝LiveRead逃逸、缺κ与身份错配。

[A2 可取消等待](results/core-language-runtime-await-cancel-a2/REPORT.md)已验证等待者退出后原任务仍承担join/释放责任；此前[有限静态G1](experiments/core-language-static/verification/closure-generative-stage-acceptance-v1/REPORT.md)、[Swift CPU R1](results/core-language-runtime-r1/REPORT.md)、[真实Metal M1](results/core-language-runtime-metal-m1/REPORT.md)与[A1 terminal AWAIT](results/core-language-runtime-await-a1/REPORT.md)也已各自验收。[WRP1局部等待证明](results/wait-responsibility-proof-wrp1/REPORT.md)已完成验收：Lean核验任意有限前缀的一次仲裁/恢复与资源非干扰，条件进展和Swift对应单列假设。[SMWO1](results/swift-mlx-wait-owner-smwo1/REPORT.md)已完成真实tiny-llm/MLX接入的有限验证：保存首CPU失败及短读修订后13项CPU通过，首次GPU六次prefill通过，N/D等待均实际取消返回，原Task join和全28层ACK之后才准入B；仍是Swift库级adapter，未接入受检PL。完整语言、成本与响应上界仍待完成；历史证据与阶段缺口见[STATUS](STATUS.md)。

[CM1](results/core-language-runtime-mlx-cm1/REPORT.md)已把这条责任链接到受检PL与真实MLX：静态/CPU门之后，独立reference→checked各首次完成，总四次prefill，A/B均72/271；受检源以实际A结果构造B，并返回历史A，最终 `(72,(271,72))`。四个原计算Task、112层归还与完整收束通过独审；首编失败及窄修保留。独立源等待取消随后由CM2普通FORK接入，整个P8未完成。

[CM2](results/core-language-runtime-mlx-fork-cm2/REPORT.md)已完成有限阶段验收：普通源任务W可取消等待并结束自己的作用域，A仍须真实join与完整KV归还，B才取得下一代Owner。静态、修订构建、五个decoder负例／八个CPU情境和首次reference→N→D六次prefill均通过独审；丢ACK情境确认W已结束、A已join也不能跳过BackendReturn。原失败和窄修完整保留。[BRP1](results/backend-return-proof-brp1/REPORT.md)现已完成有限阶段验收：22类转移、100条含辅助引理的Lean声明证明局部任意有限前缀的完整归还证据、原ownjoin必要条件、历史保持、至多一次与Unknown隔离，三个错误规则反例通过。原失败/超时与窄修保留，无新GPU或响应界结论。后续转向归还责任链的实际服务边界，P8与整体研究仍在进行。

[BRS1 服务边界](results/backend-return-service-brs1/REPORT.md)已完成[有限验收](results/backend-return-service-brs1/brs1-stage-acceptance-v1.json)：在 CM2 原任务划分上，仅一个真实 N、两次 prefill，记录 74 条 Swift／8 条 Python 观测并独立复算 44 个区间。实际归还由原 main Task 经 Dispatch 和 worker 推进，producer 结束不足以准入 B。A 的归还调用至 Owner 释放为 12.726250 ms，其中原 continuation 恢复前沿至等待返回为 0.040750 ms；不能把整段归因于调度。W 本次为早取消，模型未重跑；未建立在途取消覆盖、TTFT 或响应界。

[BRC1 主机成本诊断](results/backend-return-cost-brc1/REPORT.md)已完成[有限验收](results/backend-return-cost-brc1/brc1-stage-acceptance-v1.json)：保存的完整真实 A/B 数据经七类原 Swift 函数处理，首次CPU的126次计时调用、36条实际日志及三个拒绝控制均获独立核验。A 的 receipt 转换约2.046 ms、ACK解析约2.591 ms、完整日志调用约1.26 ms（各八样本中位数）。这些是历史对象上的孤立调用成本，不能从BRS1减出调度延迟。后续Core→Wire对照已在下述CWC1完成有限验证；本批无新GPU。

[CWC1 转换对照](results/core-wire-conversion-cwc1/REPORT.md)已完成[有限验收](results/core-wire-conversion-cwc1/cwc1-stage-acceptance-v1.json)：74边界＋4完整历史对象保持原结果与错误，独立审计2,178项通过。同进程各八样本中位数，A由2.381降至1.341 ms、B由2.367降至1.032 ms；A仍有一次4.053 ms慢样本，全部保留。CWC1本身原生产路径未替换、无新GPU；后续真实归还链对照见下述HRC1，不能把局部结果当作TTFT或响应界。

[HRC1 真实归还链对照](results/host-return-conversion-hrc1/REPORT.md)已完成[有限验收](results/host-return-conversion-hrc1/hrc1-stage-acceptance-v1.json)：两种模式完整CPU回归及四会话／八次真实prefill首次通过，完整ACK、原Task与Owner转交保持。四个配对的转换区间均短约0.80–1.20 ms，最终Owner归还区间却仅三对更短，C2-A反而慢0.171542 ms；局部收益没有逐毫秒传到资源可复用端点。每mode仅两会话，原始慢值与失败诊断全部保留；无TTFT、稳定改善或响应上界结论；必要归还获得CPU服务的问题已由下述RSP1完成首次有限对照。

[RSP1 必要归还服务提优](results/return-service-priority-rsp1/REPORT.md)已完成[有限验收](results/return-service-priority-rsp1/rsp1-stage-acceptance-v1.json)：保持原转换与完整归还义务，对实际持有责任的原main定向提优；两CPU回归、四GPU会话／八prefill及224层ACK均经独立核验。干预前沿至A原Owner释放，无新增竞争时12.014→11.904 ms，八CPU任务竞争时26.072→20.785 ms；实际采到优先级21→25。每格只有一次会话，尚不支持稳定收益、TTFT或响应界。同binary平衡重复见下述RSP2；外部请求需求沿归还依赖传递仍待独立研究，P3／完整P8未完成。

[RSP2 平衡重复结果](results/return-service-priority-rsp2/REPORT.md)已完成[有限验收](results/return-service-priority-rsp2/rsp2-stage-acceptance-v1.json)：同binary、同转换与工作量的四区组16新会话／32prefill首次全部通过，原任务和完整归还证据保持。提优8格都实际采到21→25，但八任务竞争下的四组N−P差值为−2.260、+3.915、−0.552、−2.511 ms，中位−1.406 ms，即本批3/4提优更慢。RSP1单次正结果未稳定重现；n=4也不证明普遍伤害。独立复算320新区间、704服务区间和160配对一致；下一步验证真实外部B需求到归还责任的准确绑定，再单独测量响应收益。

## 目录

| 路径 | 内容 |
| --- | --- |
| [ROADMAP.md](ROADMAP.md) | 完整研究路线、各阶段假设、对照与完成门槛 |
| [docs/EXPERIMENT_CONTRACT.md](docs/EXPERIMENT_CONTRACT.md) | 事件、指标、资源协议和证据边界 |
| `experiments/tiny-cancel/` | 真实 tiny-llm paged KV 取消与重启基准 |
| `experiments/metal-handoff/` | Swift＋Metal 物理 buffer 转交与在途窗口探针 |
| `experiments/tiny-interleave/` | 完整模型工作量下的块间调度与容量反事实 |
| `experiments/tiny-eval-crossover/` | 同进程交替执行的求值开销校准 |
| `experiments/tiny-fault-recovery/` | 主机异常保留、真实清理与新 cache 恢复 |
| `experiments/swift-cleanup-dependency/` | 真实 buffer 必要 CPU 读取、Swift 等待依赖与计算竞争 |
| `scripts/analyze.py` | 独立事件／指标校验与聚合 |
| `scripts/capture_environment.py` | 运行环境及只读依赖来源记录 |
| `results/` | 原始运行数据、日志、环境快照和分析报告 |

## 工作方式

从小规模真实 GPU pilot 开始，先确认事件、数值和资源状态，再扩大重复次数。逻辑测试只验证协议，延迟由真实计算产生。模型 TTFT 和数值探针首个结果分别命名。

既有 `../swift`、`../tiny-llm`、`../PriML` 是只读参考。这里保存新代码和结果。tiny-llm 使用已有虚拟环境与本地模型；不隐式同步依赖或下载权重。GPU 实验串行运行。VS Code Insiders 可直接打开本目录，Swift 构建和运行使用命令行，无需 Xcode 编辑区。

```sh
cd /Users/fbsh/response-lab
python3 scripts/capture_environment.py --output results/environment-reproduction.json
python3 -m unittest discover -s tests -v
```

首批执行已完成：18 次真实模型 trial 和 50 次 Metal trial，全部通过独立日志检查。结果见 [首批实验报告](results/PILOT_REPORT.md)，完整原始数据和命令保存在 `results/`。每配置只有 3 或 5 次观察，报告用于检验机制，不提供稳定 p95 或最坏响应界。

[第二批报告](results/batch2/REPORT.md)新增 189 次主实验：完整工作量、真实模型交错、同进程求值校准、异常恢复与 Swift 清理依赖。各阶段的未完成门槛仍保留在 roadmap；当前结果不等于已经证明统一 PL 或最坏响应保证。

## 复现首批实验

先构建与测试，再串行运行两个实验；输出目录必须是新的，以保留已有结果。

```sh
xcrun swift test --package-path experiments/metal-handoff -c release
xcrun swift build --package-path experiments/metal-handoff -c release
python3 -m unittest discover -s tests -v
python3 -m unittest discover -s experiments/tiny-cancel -v
python3 scripts/run_tiny_pilot.py --output-dir results/tiny-reproduction
python3 scripts/run_metal_pilot.py --output-dir results/metal-reproduction
python3 scripts/run_metal_running.py --output-dir results/running-reproduction
python3 scripts/analyze.py results/tiny-reproduction/*/stdout.json results/metal-reproduction/*/stdout.json results/running-reproduction/*/stdout.json --json --output results/reproduction-analysis.json
```

真实运行中取消的附加相位使用 `--cancel-trigger scheduled --cancel-delay-us 500 --iterations 2048`，准确命令在 `results/metal-running-final/*/run.json`。定时等待只控制用户取消到达的时刻，计算和设备收尾均来自真实 GPU；每次是否确在执行中以保存的设备时间判断。

实验细节见 [tiny-cancel](experiments/tiny-cancel/README.md) 和 [metal-handoff](experiments/metal-handoff/README.md)。

当前 Metal 可执行文件也保存在 `results/artifacts/metal-handoff`，用于核对保存的二进制哈希。归档的校验清单在 `results/manifest.json`；完整来源与报告一起保留。

## 第二批复现入口

各脚本仅接受新的结果子目录，GPU 阶段必须串行运行；先完成编译再固定二进制。第二批参数保存在 `configs/batch2.json` 和 `configs/followup.json`。固定已归档数据的检查与配对重算：

```sh
python3 scripts/archive_batch2.py --check
python3 scripts/pair_eval_crossover.py results/batch2/eval-crossover/stdout.json
python3 scripts/pair_p3_trials.py results/batch2/p3
```

新运行使用 `scripts/run_batch2.py --stage tiny|metal --output-dir 新目录`、`scripts/run_followup.py --stage fault|p3 --output-dir 新目录`，每个 stage 独立调用，不把 `|` 原样作为 shell 参数。P3 runner 默认复制第二批已验证的依赖实验二进制；源码与构建来源由原始 provenance 和快照保留。修改策略或源码后的新实验应使用新的归档与来源记录。
