# SCID-Informed Realtime Voice Support Runtime

本目录是 X-Talk 中一个受 SCID 结构化访谈方法启发的实时语音支持原型。它将语音交互、后台理解、受约束的结构化记录、流程控制和可回放日志组合起来，目标是在**不作诊断**的前提下，帮助用户梳理体验，并为研究和工程迭代留下证据。

本文档按 2026-08-18 的工作区代码与本地配置核对。运行代码、测试和实际配置才是行为事实来源；本文说明当前设计和已知边界，不能替代代码审查或临床验证。

> **范围与安全定位**
>
> 这是工程研究和交互原型，不是临床诊断、筛查、分诊或治疗系统，不能替代精神科医生、心理治疗师或受训访谈员。系统不应将普通烦躁、压力、悲伤或短暂情绪波动自动病理化，也不提供用药、住院等医疗处置建议。

## 从这里开始

- 想先理解系统：阅读[系统解决的问题](#系统解决的问题)、[术语表](#术语表)和[系统架构](#系统架构)。
- 想运行或联调：阅读[运行与配置](#运行与配置)。
- 想定位前端或后台异常：阅读[数据、审计与排查](#数据审计与排查)。
- 想了解代码边界：阅读[代码组织与测试](#代码组织与测试)。
- 想跟进研发状态：阅读[风险、证据与后续 Gate](#风险证据与后续-gate)。

完整的旧版说明已原样保存在 [readme.bak.md](readme.bak.md)。该备份用于追溯，不应与本 README 同时维护。

## 系统解决的问题

结构化心理访谈需要保留字段、时间范围、证据和流程跳转；但真实用户会停顿、修正、插话、解释题意，或在一次回答中同时给出症状、背景和问题。单个模型若同时负责自然对话、临床式推理、字段推进、并发取消和安全控制，容易在延迟、可靠性和责任边界上相互冲突。

本系统将这些职责分开：

```text
用户语音 / ASR final
        │
        ▼
安全预检查 → 控制路由 → Session Actor（唯一正式状态写入者）
                              │
              ┌───────────────┼────────────────┐
              ▼               ▼                ▼
         Talker          Observer          Assessor
       前台表达        增量候选理解       正式评估建议
              └───────────────┼────────────────┘
                              ▼
                    Broker / Ledger
              选择可说的话；提交正式字段
                              ▼
                        TTS 与事件日志
```

其中，前台体验可以先得到一句自然回应；较慢的后台任务继续理解回答。可是，任何正式字段分数或流程推进都只能由确定性状态核心提交，而不能由模型直接写入。

### 当前覆盖与不覆盖的内容

当前模板标识为 `scid_phase1_scan_fgk_v1`，包含：

- 30 个顺序执行的扫描字段；
- 当 F、G、K 相关扫描结果触发时，最多 21 个模块入口字段；
- 扫描阶段与上述入口字段的工程闭环。

它**不等于**完整 346 页 SCID，也没有完整的 criterion（诊断标准条目）级访谈图、临床 rubric（评分细则）或临床效果验证。H/I/J 等扫描题仍会执行，但当前不会展开相应完整模块。

系统同样不做以下事情：

- 不让 Talker 自行判分、宣布字段完成或做诊断；
- 不让 Observer 直接修改正式记录；
- 不将语气、性格印象或宽泛闲聊直接转为 SCID 分数；
- 不承诺后台模型给出的下一步一定临床正确；
- 不支持跨进程崩溃后的自动续接；
- 尚未实现长期个性化记忆、在线学习或 RL。

## 术语表

下表先定义本项目中反复出现的专有概念。代码标识保留英文，便于定位实现；其余章节首次使用时仍会结合上下文说明。

| 术语 | 本项目中的含义 |
| --- | --- |
| **Runtime** | 会话运行时：接收一轮用户输入，调度模型、校验结果、发布语音文本并保存审计记录的编排层。主入口是 `SCIDDualLMRuntime`。 |
| **Talker** | 前台对话模型（`Foreground Dialogue LM`）。它负责把安全的话语指令说得自然、简短并适合 TTS；不评分、不改状态。 |
| **Observer** | 增量观察模型（`IncrementalObserver`）。它理解 partial 与 final 输入，提出候选证据、背景信息或同字段追问；其结论不是正式记录。 |
| **Assessor** | 深度评估模型（`BackgroundAssessor`）。它根据冻结的上下文返回结构化 `AssessmentDecision`，建议填写当前字段、澄清或下一步；它不能直接写状态。 |
| **Ledger** | 正式评估账本（`AssessmentLedger`）。唯一可信的字段、分数、已提交证据和流程位置来源。 |
| **Blackboard** | 候选黑板（`ClinicalBlackboard`）。保存尚未确认的证据、上下文记忆、partial 计划、探针问题和推测/返工信息；它不是诊断结论。 |
| **Broker** | 前台动作仲裁器（`ForegroundActionBroker`）。收集 Assessor、Observer 和快速策略提出的候选话语，检查因果身份、排序、去重；每轮至多选择一个正式后续动作。 |
| **Actor** | 每个 session 的串行状态写入者（`SessionActor`）。所有正式状态变化按单一顺序处理，避免多个异步任务同时改 Ledger。 |
| **mutation** | 状态变更操作，例如提交一个字段、变更 delivery 状态或关闭 Saga。只有 Actor 可以执行正式 mutation。 |
| **worker** | Actor 外部执行耗时工作的异步任务，例如一次模型调用、候选生成或事件写入。worker 只能返回不可变结果，不能自行改正式状态。 |
| **CausalEnvelope** | 后台结果随附的因果身份证：interaction、turn、field、状态版本、generation、请求/关联 ID 与 deadline。Actor 据此拒绝迟到或错轮结果。 |
| **generation** | 用户输入替换代号。新的 ASR final 会递增它，使旧 partial 或旧模型结果即使迟到也失去提交资格。 |
| **state_version** | Ledger 每次正式提交后的单调版本号，用来防止结果在旧状态上提交。 |
| **TurnSupervisor** | 单轮任务监督器。它使用 AnyIO 管理该轮 Assessor、Observer、候选动作和 stream，并统一取消和截止时间。 |
| **mailbox** | Actor 的消息队列。系统将不可丢失的控制消息和可合并的后台工作分开排队，避免慢任务阻塞危机、停止或 final 输入。 |
| **deadline** | 一轮共享的绝对单调时钟截止时间。超过它的后台结果不会被当作当前轮有效结果。 |
| **bridge** | 后台正式动作尚未到达时的非终止性等待承接语。它不是提问动作，不会写 `ActionSelected`、提交 Ledger 或取消后台评估。 |
| **speculation** | 在上一字段尚待最终确认时，保守地预先准备或播出一步候选问题的机制；默认关闭。 |
| **Saga** | 对 speculation 的可补偿事务。若之前的预先动作后来被否决，系统只允许一次受控 repair（修复），避免重复提交或重复追问。 |
| **snapshot / event projection** | snapshot 是当前热状态的可读投影；event projection 是从按序事件生成的审计视图。两者默认不保存自由文本。 |
| **shadow / active** | Observer 的运行权限。`shadow` 只记录预测、不影响用户；`active` 才可向 Broker 提供合格的前台候选。 |
| **floor control** | 对话发言权控制：决定何时保持安静、何时短承接、何时追问、何时停止。当前实现有限；Gate 1.5 会引入专门的规划器。 |

## 系统架构

### 三个模型角色与确定性核心

当前逻辑上有三个模型角色：

| 角色 | 输入 | 输出 | 明确不能做什么 |
| --- | --- | --- | --- |
| Talker | `DialogueDirective`（安全话语指令）与有限对话上下文 | 面向用户的自然短文本 | 不评分、诊断、推进字段或泄露内部 JSON/字段名 |
| Observer | 当前字段、有限上下文、ASR partial/final | `TurnInterpretation`：候选理解、证据、背景、同字段探针或候选动作 | 不写 Ledger、不单独决定字段完成 |
| Assessor | 冻结的 `AssessmentRequest`：本轮文本、字段、版本和有限证据 | `AssessmentDecision`：提交、澄清或下一步建议 | 不直接改 Blackboard/Ledger，不读持续变化的状态 |
| 确定性核心 | 模型 proposal、控制事件、delivery receipt | 经过校验的动作、状态变化与事件 | 不把模型输出当作自动真相 |

Observer 与 Assessor 当前可使用同一 DeepSeek endpoint 与凭据，但它们有不同 prompt、JSON schema 和权限。`realtime + active` 配置要求两个模型名不同，避免误把两个角色配置成同一模型。ASR 与 TTS 属于语音链路，不计入这里的 LM 角色。

### 状态边界

```text
用户原始 interaction history
              │
              ▼
ClinicalBlackboard（候选、可撤回）
              │  仅 Actor 接收合法 AssessmentDecision 后
              ▼
AssessmentLedger（正式、可审计）
```

Router 只解决“用户正在控制什么”：暂停、恢复、结束、危机、流程问题、题意澄清或 partial。它不是临床证据过滤器；除噪声外的正常对话会继续交给 Observer/Assessor 理解。`SafetyGuard` 位于 Router 前方，先做同步危机预检查。

Ledger 能保证字段、版本、证据格式和流程推进的一致性；它不能证明 Assessor 的临床判断本身正确。临床正确性需要金标准案例、人工审核和后续评测来建立证据。

### 一轮实时会话如何协作

1. Manager 将 ASR partial/final 转为 Runtime 调用；final 得到递增的 `interaction_seq` 与 `generation`。
2. `SafetyGuard` 处理危机；Router 处理暂停、继续、结束、题意等控制路径。危机/停止会抢占当前轮。
3. Actor 接受输入、记录 `InputAccepted` 与 `RouteChosen`，创建该轮唯一的 `TurnSupervisor`。
4. Talker 先根据当前指令给出一段自然的初始回应；Observer 与 Assessor 并发运行。所有结果都携带 `CausalEnvelope`。
5. Broker 只接受字段、版本、interaction、generation 都匹配的候选。Assessor 的正式提交优先于 Observer probe；明确否定的快路径可在严格规则下更快推进。
6. 在初始段语义边界后，Broker 先等待约 `0.35` 秒。若尚无正式动作，Runtime 输出 bridge，但继续等待后台任务。
7. 当有效的 `AssessmentDecision` 被 Actor 提交，或有效 Observer probe 被 Broker 选中时，Talker 在同一 action stream 输出真正的后续追问/说明。只有此时才写 `ActionSelected` 和对应 delivery 状态。
8. Manager 将文本分句交给 TTS，并把发布结果回传为 delivery receipt；Actor 写入 `DeliveryCompleted`、更新 snapshot，并在终止时完成落盘。

这意味着 bridge 仅用于降低短暂等待感：它不代表当前轮已完成，也不代表用户已经听到一条正式访谈动作。若 deadline 前始终没有合法动作，stream 结束而不伪造临床追问。

### Runtime v3：一致性、取消与资源边界

`SCIDDualLMRuntime` 是对旧调用方保持兼容的外观；内部采用每会话一个 Actor 和显式状态图。Actor 按单调 `event_seq` 串行处理状态变化，worker 只能提交带 `CausalEnvelope` 的 proposal。字段、轮次、版本或 generation 不匹配的结果会被拒绝，因而远端请求即使无法真正中止，迟到结果也不会污染当前会话。

每个普通评分轮只允许一个 `TurnSupervisor`，它用 AnyIO task group 管理前台 stream、final Observer、Assessor 和候选任务，并共享绝对单调 deadline。新的 final、危机、停止、delivery 失败和 shutdown 都会取消对应 scope。已经选择的一步 speculation 会转入唯一的 Saga scope，不会和新的普通评分事务并行。

两级 mailbox 提供背压：

| 队列 | 默认容量 | 放入内容 | 满载行为 |
| --- | ---: | --- | --- |
| control lane | 64 | final、危机、停止、Assessor/Ledger 命令、delivery receipt | 不静默丢弃；接近满载时暂停普通输入，危机/停止仍可进入 |
| work lane | 128 | partial、Observer、候选预生成、非关键 telemetry | 相同 key 保留最新结果；满载时优先取消或丢弃旧工作 |

`ModelGateway` 统一处理远端模型的每会话/全局并发上限、优先级、deadline、有限重试和 circuit breaker（连续失败后的临时熔断）。优先级依次为危机/前台首响应、Assessor、final Observer、partial Observer、候选预生成。Python task 被取消不等于供应商已停止计算，所以 causal gate 仍是最终保护。

### 有界状态与 500 轮目标

为避免长会话把全部 transcript 和 trace 都留在内存，Runtime 使用版本化 `RetentionPolicy`：

| 热状态 | 默认上限 |
| --- | ---: |
| recent interaction turns / runtime turns | 各 32 |
| latency traces / foreground actions / Observer updates | 各 64 |
| partial plan history | 32 |
| candidate evidence / contextual memories | 各 128，按内容指纹去重 |
| candidate utterance cache | 128 项 LRU |
| committed Ledger turns | 16 |

字段模板数量有限，`field_states` 全量保留。较早的有效结构化信息压缩进入 `SessionMemoryProjection`；模型上下文使用有界窗口，而不无限回填原始逐字稿。当前工程认证目标是单会话 500 轮及 3 个并发 session × 500 轮的进程内 soak；超过该范围继续使用有界状态，但不作同等级稳定性声明。

### 一步预先推进（默认关闭）

当 `scid_allow_one_step_speculation=true` 时，系统最多允许一个未关闭的预先动作。它由 `action_id + transition_version` 保证幂等，状态如下：

```text
Proposed → Selected → Spoken → PendingCommit
                                ├─ Confirmed → Closed
                                ├─ Compensating → Closed
                                └─ Cancelled → Closed
```

若正式评估确认前一个字段，Saga `Confirmed`；若否决或用户中断，Actor 在同一受控状态变化中执行补偿与 repair。重复 receipt、迟到结果、危机和中断不得造成第二次提交或第二次 repair。

## 运行与配置

以下示例假设当前目录为仓库内的 `xtalk/`。使用本地私有配置或环境变量提供密钥，绝不要把真实 key 写入 README、日志或提交记录。

```bash
DEEPSEEK_API_KEY=<your_key> PYTHONPATH=src \
python examples/psych_sop_voice_demo/server.py \
  --config ../ali_config.json \
  --mode scid \
  --backend-model deepseek-v4-pro
```

启动日志会打印实际加载的 `xtalk` 包路径。它应指向当前仓库的 `xtalk/src/xtalk/__init__.py`；若指向 `site-packages`、其他 clone 或旧虚拟环境，前端重启后仍可能运行旧逻辑。

### 配置项

配置来自 X-Talk 的 `service_config`。优先级为：

```text
显式 CLI 参数 > service_config 中的 scid_* 字段 > 代码默认值
```

| 配置项 | 默认值 | 用途 |
| --- | --- | --- |
| `data_dir` | `data` | episode 写入 `<data_dir>/psych_sop_demo/episodes/` |
| `scid_experiment_id` | `scid_voice_demo` | 审计元数据中的实验标识 |
| `scid_backend_model` | `deepseek-v4-pro` | Assessor 模型名 |
| `scid_observer_model` | `deepseek-v4-flash` | Observer 模型名；active 时不得与 Assessor 相同 |
| `scid_prefer_deepseek` | `true` | `false` 时强制规则 fallback，适合离线测试 |
| `scid_deepseek_api_key` / `scid_deepseek.api_key` | 无 | API key；没有时再读取 `DEEPSEEK_API_KEY` |
| `scid_deepseek_base_url` / `scid_deepseek.base_url` | `https://api.deepseek.com` | OpenAI-compatible endpoint |
| `scid_observer_mode` | `shadow` | `off`、`shadow` 或 `active` |
| `scid_allow_one_step_speculation` | `false` | 是否允许最多一步预先推进 |
| `scid_persist_raw_transcript` | `false` | 是否将自由文本写入独立、受限权限的 artifact stream |
| `scid_observer_confidence_threshold` | `0.9` | Observer 候选动作/证据槽探针的最低置信度 |
| `scid_partial_plan_max_age_seconds` | `3.0` | stable partial 可提升到 final 仲裁的最长时间 |
| `scid_post_initial_action_wait_seconds` | `0.35` | 初始段语义边界后无正式动作时，发出 bridge 前的最长等待 |
| `scid_partial_observer_debounce_seconds` | `0.5` | stable partial 触发 Observer 前的防抖时间 |

当前本地保守交互配置为：

```json
{
  "scid_observer_mode": "active",
  "scid_allow_one_step_speculation": false,
  "scid_persist_raw_transcript": false,
  "scid_observer_model": "deepseek-v4-flash",
  "scid_backend_model": "deepseek-v4-pro",
  "scid_partial_plan_max_age_seconds": 3.0,
  "scid_post_initial_action_wait_seconds": 0.35
}
```

推荐的模式选择：

| 场景 | 推荐配置 | 含义 |
| --- | --- | --- |
| 本地无网络/单测 | `scid_prefer_deepseek=false`，Observer `off` | 规则 fallback；不能代表真实模型质量 |
| 观察 Observer | Observer `shadow`，speculation `false` | 记录一致率与延迟，不影响对话 |
| 当前保守交互 | Observer `active`，speculation `false` | Observer 只能提供受校验的同字段帮助 |
| 预先推进研究 | Observer `active`，speculation `true` | 仅在冻结 replay 达标后启用 |

## 数据、审计与排查

### 文件位置、命名与隐私

默认 episode 目录为：

```text
data/psych_sop_demo/episodes/
```

每次会话使用便于排序且不含用户标识的名称：

```text
scid_<UTC timestamp>_<8-char random suffix>
```

例如 `scid_20260723T181022Z_398ff0c5`。同一次会话可能生成：

| 文件 | 用途 |
| --- | --- |
| `<episode>.events.jsonl` | 追加式、按 `event_seq` 排序的领域事件；用于因果审计和分页读取 |
| `<episode>.partial.json` | latest-wins 的当前热状态投影；用于在线调试，不是崩溃恢复 checkpoint |
| `<episode>.json` | session 结束后的最终 snapshot |
| `<episode>.artifacts.jsonl` | 仅在 `scid_persist_raw_transcript=true` 时存在；保存原文 artifact |

默认事件与 snapshot 会递归脱敏自由文本、模型原始 payload 和密钥。原文 opt-in 后也只写入单独的 artifact stream，事件与 snapshot 保存引用；目录权限为 `0700`、文件权限为 `0600`。这属于存储最小化，不等于加密、TTL、删除 API 或供应商侧数据治理。

事件使用 schema v4 与 `runtime_profile=realtime_v3`。单条事件最多 64 KiB，具有 `event_seq`、schema version、causation/correlation 信息。`read_events(after_seq=0, limit=100)` 支持异步分页，`limit` 范围为 1–500。终止、危机、停止和 shutdown 会 drain writer，并对 event log、存在的 artifact 与 final snapshot 执行 flush/`fsync`；当前不从事件日志自动恢复进程崩溃后的会话。

Gate 1 最小审计骨架已可用：在仓库根目录执行 `PYTHONPATH=src python -m xtalk.psych_sop.scid.audit <episode_id>`，会在 episode 目录生成 `<episode_id>.audit/`，其中包含脱敏的 `session.md`、逐轮报告和 `index.json`。报告只读取事件与 snapshot，不修改 Runtime 或 Ledger；旧 episode 缺少新审计事件时会标记为不可用。完整边界见 [审计优化方案.md](../../../../data/psych_sop_demo/审计优化方案.md)。

### 从一轮输入定位问题

先找目标 `interaction_seq`，再沿以下链路检查：

```text
InputAccepted
  → RouteChosen
  → initial Talker segment
  → Observer / Assessor
  → Broker ActionSelected
  → TTS / DeliveryCompleted
  → AssessmentCommitted
```

`AssessmentCommitted` 和 `ActionSelected` 的先后可随动作类型不同，但只有合法的 Actor 状态变化才会产生它们。不要仅凭用户听到 initial segment 就认定该轮已经完成。

常用命令：

```bash
# 当前目录为 xtalk/
tail -f logs/xtalk_YYYYMMDD_HHMMSS.log

rg -n "SCID|DeepSeek|deepseek|backend|Event handler raised|AsyncCompletions|asr.result_final" \
  logs/xtalk_*.log

ls -lt data/psych_sop_demo/episodes/*.partial.json | head
```

| 现象 | 先检查 | 常见原因 |
| --- | --- | --- |
| 回答后完全没有前台声音 | `frontend_first_token_at`、Manager/TTS 日志 | 前台 stream 无输出、事件未消费、TTS 异常，或新 turn 立即取消旧 stream |
| 只听到 bridge | `ForegroundBridgeRequested` 后是否有 `AssessmentCommitted`、`ObserverUpdated`、`ActionSelected` | 后台未产生合法动作、deadline 到期、stream 被新 final/stop/crisis 取消，或 action worker 异常 |
| action 已选中但未播放 | `foreground_actions` 的 delivery 状态、`followup_segment_published_at` | 首个 chunk 前取消、发布失败或 TTS 未消费；`selected` 不等于用户已听到 |
| “继续/暂停”被误判 | `route_decision.route`、`interaction_mode` | 控制表达变体未被规则覆盖；应修 control detector，不应误归因于 Talker |
| 重复当前题 | Ledger `current_field_id`、`state_version`、stale 原因 | Assessor 只给 reask、结果过期、delivery 取消或动作策略选择不当 |
| 字段错误推进 | `ledger.turns[-1].decision`、`field_states` | Assessor 结论或候选动作质量问题；Ledger 只能保障一致性，不能保障临床正确性 |
| `Decision turn_id is stale or unknown` | envelope、Actor event、turn phase | 旧任务返回、重复应用或 supervisor 已取消；Actor 应拒绝结果而非提交 |

正常的慢后台路径是：

```text
ForegroundBridgeRequested
  → AssessmentCommitted 或 ObserverUpdated
  → ActionSelected
```

不应出现 `ForegroundBridgeRequested → ActionSelected(kind=hold, source=timeout_fallback)`。若只有 bridge，优先检查 turn deadline、generation gate、action stream cancellation 和 Manager 是否持续消费同一 iterator。

### 审计的已知边界

当前 Runtime 会记录脱敏的模型调用终态与延迟、用户可见 segment、delivery 结果、Ledger before/decision/after diff，以及现有的路由、动作选择和 Saga 事件；审计投影会按 `interaction_seq` 生成逐轮 Markdown。默认仍不包含用户原话、助手原话、模型 raw payload 或 reasoning；受控原文回放、HTML 时间线、跨进程恢复与生产级访问治理仍是后续工作。

## 代码组织与测试

### 目录职责

```text
scid/
├── core/            字段 schema、模板、产品约束与输入校验
├── state/           Ledger、Blackboard、telemetry
├── policy/          控制 Router、Observer、延迟策略
├── assessment/      Assessor 请求/响应与模型后端
├── dialogue/        Talker 指令、前台渲染、缓存和 repair 话术
├── orchestration/   Runtime v3：Actor、状态图、Supervisor、Gateway、Saga、事件存储
├── knowledge/       SCID 资料抽取、候选生成与人工复核工具；不在实时主链路
└── *.py             兼容 shim，维持旧导入路径
```

`orchestration/` 的重点模块：

| 文件 | 责任 |
| --- | --- |
| `runtime.py` | `SCIDDualLMRuntime` 外观与每轮编排入口 |
| `actor.py` | `SessionActor`、双 mailbox、正式状态 mutation 与事件排序 |
| `state_graph.py` | session / turn / assessment / delivery 等枚举状态与合法迁移 |
| `supervisor.py` | AnyIO 单轮任务生命周期、deadline 与取消传播 |
| `action_policy.py` | Broker 对前台候选动作的因果校验、优先级与去重 |
| `model_gateway.py` | 模型准入、并发、重试、超时和熔断 |
| `saga.py` | 一步 speculation 的幂等状态机与补偿 |
| `event_store.py` | append-only 事件、snapshot 写入和终止 flush |
| `memory_projection.py` | 长会话的有界结构化记忆投影 |

X-Talk 接入由 `SCIDDualLMManager` 处理：它把 ASR/VAD/事件总线转为 Runtime 输入，将 Runtime 的 initial/action stream 分句交给既有 TTS，并写 delivery receipt。它不拥有 sequence、stale 判定、Ledger 或领域状态。

### 运行前后的最低验证

```bash
PYTHONPATH=src python -m pytest tests/test_psych_scid.py -q

PYTHONPATH=src python -m pytest \
  tests/test_psych_scale_engine.py \
  tests/test_psych_safety_guard.py \
  tests/test_psych_sop_navigator.py \
  tests/test_psych_memory_backend.py \
  tests/test_psych_episode_logger.py -q

python -m py_compile src/xtalk/psych_sop/scid/orchestration/runtime.py
git diff --check
```

高风险改动还应覆盖：重复 final、迟到模型结果、重复 delivery receipt、stream cancellation、模型/磁盘短暂失败、mailbox 满载、危机/停止抢占、500 轮 soak 和 3 个并发 session 隔离。验收要关注 stale/duplicate Ledger commit、跨 interaction delivery、speculation depth、orphan task、热状态上限、事件序列连续性及终止时任务归零。

## 风险、证据与后续 Gate

### 当前风险登记

| ID | 当前风险 | 状态与下一步 |
| --- | --- | --- |
| GOV-01 | Gate 0 基线完成；若扩大产品或临床范围，仍需要新的证据与审查 | 保持治理制品、范围控制与变更审查 |
| DOM-01 | 仅覆盖 scan 与 F/G/K 入口，不是完整 SCID 流程 | Gate 2 版本化领域模型 |
| VAL-01 | Ledger 保证因果/结构一致，不证明临床判断正确 | Gate 3 金标准案例与人工审核 |
| ORCH-01 | 当前每轮至多“初始段 + 后续动作”，尚非连续自然访谈 | Gate 1.5 话语规划与 floor control |
| CTRL-01 | 规则 Router 对自然表达可能漏判 | 小模型 Router 的 shadow/replay 研究 |
| OBS-01 | Observer 为实验组件 | 先 shadow 评估，再扩大权限 |
| POL-01 | Broker 只仲裁本轮动作，不是跨轮对话策略器 | 引入 Planner，但不得越过 Ledger 边界 |
| REL-01 | 远端慢、失败和取消不可控 | Gateway 指标、soak 与故障注入 |
| SAFE-01 | 尚不具备临床部署级安全与治理 | Gate 0/5/6 的证据链 |
| DATA-01 | 无崩溃恢复；隐私治理尚不完整 | Gate 1/3/6 的审计与数据治理 |
| EVAL-01 | 缺少 clinical gold benchmark | Gate 3 系统化评测 |
| SEC-01 | 供应链与安全工程证据不足 | Gate 6 发布准备 |
| REL-02 | 运营发布治理未闭环 | Gate 5/6 监督试点与运行手册 |

### Gate 路线图

Gate 是分阶段证据门槛，不是“功能清单完成即上线”的承诺。每一阶段应先明确可验证产物、失败边界和人工审查要求。

| Gate | 目标 | 主要产物/通过条件 |
| --- | --- | --- |
| Gate 0 | 范围、风险和治理基线 | D0 已完成；扩范围必须补充风险评估、制品和审查 |
| Gate 1 | 可复现工程基线、运行时可靠性、逐轮审计 | 调用链追踪、可读报告、500 轮稳定性证据、隐私边界 |
| Gate 1.5 | 自然话语编排原型 | Captioner、Observer contribution、Talker、Planner 与 floor control 的 shadow 验证 |
| Gate 2 | 版本化 SCID 领域模型 | 补全字段、依据、流程 fidelity 与来源版本 |
| Gate 3 | 系统化评测、研究复现、数据治理 | golden replay、标注/人工审核、指标和访问治理 |
| Gate 4 | 更自然的访谈与广泛理解 | 经评测的自然对话、安全个性化与复杂表达处理 |
| Gate 5 | shadow 验证与人工监督试点 | 监督流程、升级路径、运行治理和人类复核 |
| Gate 6 | 生产、临床与监管准备 | 安全、隐私、供应链、运营和临床/监管证据 |

`OPT`、`LEARN`、`RL` 只属于条件性研究轨道，不代表当前产品承诺；任何在线学习或强化学习都不得绕过上述数据、审批和安全门槛。

### Gate 1：审计与稳定性

Gate 1 的目标不是收集越多原文越好，而是让每个重要决定能够被复现和质询。每轮报告应在受控权限下关联：用户输入与 Talker 输出、所有模型请求/返回的时间线、每个模型的结构化结论和依据、Router/Broker/Actor 的决定、Ledger 前后版本、delivery 结果、错误与 causation/request ID。默认日志仍应脱敏；完整原文和模型 payload 只能在显式授权、隔离 artifact 与访问审计下保存。

详细计划见 [审计优化方案.md](../../../../data/psych_sop_demo/审计优化方案.md)。

### Gate 1.5：Captioner、Observer、Talker 与全局话语规划

当前 bridge 的问题不只是文案重复，而是系统还没有跨轮的 `InterviewPlanner` / `DiscoursePlanner`（全局访谈/话语规划器）。单靠一个 timeout fallback 很容易让“嗯，我在听”或“可以慢慢来”在连续超时时机械重复，也无法决定何时应解释题意、何时应更具体地追问、何时应该安静等待。

Gate 1.5 的拟议结构是：

```text
ASR / Captioner
       │  低延迟、分段且可修订的语义/副语言线索
       ▼
Observer ── ConversationContribution ──┐
Assessor ── 正式评估建议 ────────────────┼──► InterviewPlanner
Ledger / Blackboard / 近期对话投影 ──────┘          │
                                                    ▼
                                         TurnSpeechQueue / FloorController
                                                    ▼
                                                  Talker
```

- **Captioner** 是额外的音频理解来源，拟接入 Qwen3-Omni 一类能力，给 Observer/Assessor 提供时间对齐的语义、停顿、犹豫、情绪/副语言候选；它不作诊断，不能直接写 Ledger。
- **ConversationContribution** 是 Observer 交给规划器的结构化“可说内容”，例如承接点、对用户疑惑的解释、可选澄清角度、建议频率、禁止重复主题；它不是强制播报文本，更不是正式评分。
- **InterviewPlanner / DiscoursePlanner** 只做话语计划：结合最近已说内容、未回答问题、用户表达、等待预算和字段目标，选择沉默、承接、解释、反映、探问或过渡。它不改 Ledger、不评分、不直接播音。
- **TurnSpeechQueue** 保存有限、可取消、去重的候选说话段；**FloorController** 根据用户是否仍在说、TTS 是否播放、是否已有有效正式动作、重复风险和安全事件决定何时让 Talker 说话。
- **QuestionSemanticContract** 将每个字段拆成不能丢失的关键临床信息、允许口语化的表达、与上次问法的差异要求及升级追问路径，避免机械复述扫描题。
- **Assessor 仍是正式 Ledger 推进的唯一来源**。Observer/Captioner/Planner 的作用是让 Talker 在等待和追问时更贴合用户，而不是降低正式评估门槛。

“持续输出”在这里指**持续具备有价值内容的供给能力**，由 Planner 和 floor control 决定是否、何时播出；它绝不是模型无限说话。实施必须先在 Gate 1 的审计数据上运行 shadow/replay，验证重复率、打断率、无效 bridge、话题偏移、行动延迟和用户可见一致性，再允许影响真实 Talker。

## 贡献与变更原则

对本目录的修改应遵守以下边界：

1. 先明确变更影响 Talker、Observer、Assessor、Actor、Ledger、事件还是 X-Talk 接入中的哪一层。
2. 模型输出始终视为 proposal；正式 mutation 必须经过 Actor、causal gate 与 Ledger 校验。
3. 新增状态、事件或配置时，同时更新 schema、snapshot/event projection、脱敏策略、测试和本文档。
4. 不要把自然语言体验问题误修成 Ledger 放宽；不要把临床判断不准误归因于并发一致性。
5. 改动 Runtime、Saga、delivery、Router 或模型 prompt 前，使用冻结 replay 和故障注入检查重复、过期、取消和隐私边界。

如需追溯已删减的历史说明、字段级实现细节或旧计划，请查阅 [readme.bak.md](readme.bak.md)，再以当前代码和测试验证其是否仍适用。
