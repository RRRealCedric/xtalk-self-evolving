# Psychology SOP-Agent Demo

这是一个独立于 X-Talk 主语音链路的 text-only 心理量表 SOP demo。它的目标不是做临床级心理咨询系统，而是先把“结构化心理量表引导 + SOP 流程控制 + 轻量单用户记忆 + episode-level 自进化日志”这条工程链路跑通，并为后续接入 X-Talk ASR/LLM/TTS pipeline、Mem0、真实 PsychologySOP 手册和真实访谈数据保留清晰接口。

## 一句话概览

`xtalk.psych_sop` 是一个最小可运行的心理 SOP-Agent 原型：

- 用 `ScaleEngine` 确定性执行 PHQ-9 / GAD-7 量表。
- 用 `SOPNavigator` 控制访谈节点，不让流程散掉。
- 用 `SafetyGuard` 在明显危机表达时中断量表并进入危机回应。
- 用 `CounselingAgent` 生成温和、简短、非诊断的中文回复。
- 用 `LocalJsonMemoryBackend` 或可选 `Mem0MemoryBackend` 写入轻量单用户记忆。
- 用 `EpisodeLogger` 保存每次 demo 的完整 episode JSON。
- 用 `EvolutionSummarizer` 生成一次 episode-level 的自进化总结。

当前版本是 rule-based demo，不强制依赖真实 LLM，也不强制依赖 Mem0。

## 设计原则

这个模块有几条非常明确的边界：

- 不做临床诊断。
- 不提供药物建议。
- 不声称替代心理咨询师、心理医生或精神科医生。
- 不承诺治疗效果。
- 量表结果只能表达为“筛查结果”“量表结果”“可能提示”。
- 用户表达自伤、自杀、伤害他人或立即危险时，停止量表流程，进入危机回应。
- 第一版不消化 PDF，不接真实访谈数据，不接主 voice pipeline。
- 量表状态、题号、答案、分数必须是确定性状态，不只存入 memory。
- 所有 SOP、安全规则、prompt 文案都应尽量可编辑。

## 目录结构

```text
src/xtalk/psych_sop/
├── README.md
├── __init__.py
├── scale_schema.py
├── scale_loader.py
├── scale_engine.py
├── sop_schema.py
├── sop_template.yaml
├── sop_navigator.py
├── safety_rules.yaml
├── safety_guard.py
├── prompts.yaml
├── counseling_agent.py
├── runtime.py
├── memory_backend.py
├── episode_logger.py
├── evolution_summarizer.py
└── demo_cli.py

examples/psych_sop_demo/
└── demo_cli.py

examples/psych_sop_voice_demo/
└── server.py

tests/
├── test_psych_scale_engine.py
├── test_psych_safety_guard.py
├── test_psych_sop_navigator.py
├── test_psych_memory_backend.py
└── test_psych_episode_logger.py
```

## 模块职责

### `scale_schema.py`

定义量表数据结构。

核心类型：

- `ScaleOption`
  - `option_id`
  - `description`
  - `score`

- `ScaleSpec`
  - `scale_id`
  - `title`
  - `description`
  - `introductions`
  - `score_interpretation`
  - `questions`
  - `options`
  - `additional_questions`
  - `source_path`

- `ScaleSessionState`
  - `scale_id`
  - `current_index`
  - `answers`
  - `raw_answers`
  - `skipped`
  - `status`
  - `started_at`
  - `updated_at`
  - `abort_reason`

这里的 state 是量表运行过程的确定性状态。也就是说，当前题号、答案、跳过题目、最终分数不依赖 LLM，也不依赖 memory。

### `scale_loader.py`

负责加载 `psydata/pysc` 里的量表 JSON，并规范化为 `ScaleSpec`。

当前路径默认指向：

```text
psydata/pysc/
```

支持：

- `GAD-7`
- `PHQ-9`
- `SCL-90`

注意：当前仓库中的 `psydata/pysc/*.json` 可能是 Git LFS pointer，不一定是真实 JSON 内容。因此 loader 会先尝试读取真实 JSON；如果发现是 LFS pointer、文件缺失或 JSON 不合法，就使用内置 fallback spec。

内置 fallback 的目的只是保证 demo 可运行，不代表最终专业版本。

### `scale_engine.py`

结构化量表执行器，负责确定性动作。

主要方法：

```python
load_scale(scale_id)
start_scale(scale_id)
get_current_question()
get_options()
record_answer(question_index, option_id, raw_user_text, confidence=1.0)
skip_question(question_index)
has_next_question()
next_question()
compute_score()
get_score_interpretation(score)
get_progress()
abort_scale(reason)
parse_answer(user_text)
snapshot()
```

输入解析逻辑：

- 用户输入 `0` / `1` / `2` / `3` 时，直接映射到选项编号。
- 用户输入选项文字时，尝试匹配选项描述。
- 用户输入“有一点”“经常”“几乎每天”等模糊自然语言时，使用关键词做低复杂度映射。
- 置信度太低时，CLI 会要求用户重新确认。

### `sop_schema.py`

定义 SOP 数据结构。

核心类型：

- `SOPNode`
  - `id`
  - `goal`
  - `allowed_actions`
  - `transitions`
  - `prompt_hints`

- `SOPSpec`
  - `sop_id`
  - `global_rules`
  - `crisis_response`
  - `nodes`

- `NextAction`
  - `node_id`
  - `action`
  - `reason`
  - `should_end`
  - `data`

### `sop_template.yaml`

可编辑 SOP 配置。

当前 SOP 节点：

```text
START
EXPLAIN_BOUNDARY
GET_CONSENT
GOAL_INQUIRY
SCALE_SELECTION
RISK_CHECK
SCALE_LOOP
CLARIFY_ITEM
RECORD_ANSWER
COMPUTE_SCORE
EXPLAIN_RESULT
SUPPORTIVE_CLOSE
CRISIS_RESPONSE
ABORTED
```

每个节点包含：

- `id`
- `goal`
- `allowed_actions`
- `transitions`
- `prompt_hints`

`global_rules` 和 `crisis_response` 也写在这个 YAML 中，方便后续由心理学成员或产品成员直接编辑。

### `sop_navigator.py`

规则版 SOP 状态机。

核心接口：

```python
class SOPNavigator:
    def __init__(self, sop_spec): ...
    @classmethod
    def from_yaml(cls, path=None): ...
    def current_node(self): ...
    def allowed_actions(self): ...
    def set_node(self, node_id): ...
    def step(self, user_input, context): ...
```

第一版 `step()` 使用简单规则和 context flags：

- `safety_interrupt`
- `high_risk`
- `selected_scale`
- `needs_clarification`
- `user_skips`
- `answer_recorded`
- `has_next_question`
- `score_computed`

后续可以把 `step()` 的 transition 判断扩展成 LLM-assisted classifier，但不建议把确定性量表状态交给 LLM。

### `safety_rules.yaml`

可编辑安全关键词配置。

当前分为：

- `high`
  - `self_harm`
  - `harm_to_others`

- `moderate`
  - `panic`
  - `unknown`

当前 `interrupt_levels` 包含 `high`，也就是说高风险命中会立即中断 SOP。

### `safety_guard.py`

关键词版危机识别器。

核心接口：

```python
class SafetyGuard:
    def classify(self, text: str) -> SafetyResult: ...
```

`SafetyResult` 包含：

- `risk_level`
  - `none`
  - `low`
  - `moderate`
  - `high`

- `risk_type`
  - `none`
  - `self_harm`
  - `harm_to_others`
  - `abuse`
  - `panic`
  - `unknown`

- `matched_signals`
- `should_interrupt_sop`

这是 demo 级规则，不是经过验证的安全分类器。

### `prompts.yaml`

可编辑回复风格和边界文案。

包含：

- `prompt_version`
- `style_rules`
- `boundary_text`
- `option_hint`

当前 `CounselingAgent` 是 rule-based，所以这里不是传统 LLM prompt，但它承担同样的“可编辑行为配置”作用。

### `counseling_agent.py`

规则版回复生成器。

输入：

- 当前 SOP node
- action
- scale title
- 当前题目
- options
- progress
- score
- interpretation
- crisis response
- memory context
- selected scale

输出：

- 中文 assistant 文本

当前原则：

- 中文为主。
- 温和、简短、不评判。
- 一次只问一个问题。
- 不暗示答案。
- 尊重跳过和退出。
- 量表题目尽量自然表达，但不改变题意。
- 解释结果时强调非诊断性。

未来如果接 LLM，可以把 `CounselingAgent.render()` 替换或扩展成 LLM call，同时保留当前 deterministic context。

### `runtime.py`

可复用的 PsychSOP 会话运行时。CLI 和 X-Talk voice demo 都调用同一个 runtime，避免把状态机逻辑复制到多个入口。

核心接口：

```python
runtime = PsychSOPRuntime(scale_id="GAD-7")
assistant_text = runtime.start()
assistant_text = runtime.accept_text("同意")
snapshot = runtime.snapshot()
episode_path = runtime.finish(status="aborted")
```

职责：

- 初始化 SOP、量表引擎、安全规则、回复 agent、memory 和 episode logger。
- 渲染首轮 greeting。
- 接收一轮用户文本并推进 SOP。
- 在 completed、crisis、aborted 时保存 episode log。
- 写入 `scale_state_summary` 和 `evolution_memory`。

`PsychSOPRuntime` 不读 stdin，不 print，不依赖 WebSocket，因此可以被 CLI、测试、X-Talk manager 或未来的 LLM-backed agent adapter 复用。

### `memory_backend.py`

心理 demo 的轻量 memory 接口。

统一接口：

```python
class PsychMemoryBackend:
    def search(query, scope=None, top_k=5): ...
    def add_dialogue_turn(user_text, assistant_text, metadata=None): ...
    def add_note(content, scope, metadata=None): ...
    def reset(): ...
    def export(path): ...
```

当前固定 debug user：

```python
DEBUG_USER_ID = "xtalk_psych_demo_user"
```

Memory scope：

- `dialogue_memory`
  - 普通对话偏好和个性化上下文。

- `scale_state_summary`
  - 最近一次量表结果摘要。

- `evolution_memory`
  - episode-level 自进化总结，例如失败原因、退出点、改进建议。

实现：

- `LocalJsonMemoryBackend`
  - 默认 fallback。
  - 写入 `data/psych_sop_demo/memory.json`。

- `Mem0MemoryBackend`
  - 可选。
  - 仅当安装 mem0 且存在 `MEM0_API_KEY` 时尝试使用。
  - 初始化失败会 fallback 到 local JSON。

### `episode_logger.py`

保存每次 demo episode。

默认目录：

```text
data/psych_sop_demo/episodes/
```

每个 episode JSON 包含：

- `episode_id`
- `user_id`
- `task`
- `scale_id`
- `sop_version`
- `prompt_version`
- `experiment_id`
- `started_at`
- `ended_at`
- `status`
- `turns`
- `answers`
- `score`
- `interpretation`
- `safety_events`
- `skipped_questions`
- `clarification_count`
- `dropout`
- `failure_type`
- `notes`

### `evolution_summarizer.py`

生成 episode-level 的自进化总结。

当前是 rule-based：

- 完成时，记录完成状态、跳过题数、澄清次数。
- crisis 时，记录安全中断并建议细化危机识别和资源文案。
- aborted 时，记录退出并建议优化继续/保存进度策略。
- failed 时，建议人工检查 episode log。

生成结果会写入 memory scope：

```text
evolution_memory
```

### `examples/psych_sop_demo/demo_cli.py`

真正的可运行 CLI。

它连接：

- `SOPNavigator`
- `SafetyGuard`
- `CounselingAgent`
- `ScaleEngine`
- `PsychMemoryBackend`
- `EpisodeLogger`
- `EvolutionSummarizer`

### `examples/psych_sop_voice_demo/server.py`

独立的 X-Talk voice demo server。它不会修改默认 `DefaultService` 行为，而是在示例服务里注册 `PsychSOPManager`。

运行方式示例：

```bash
PYTHONPATH=src python examples/psych_sop_voice_demo/server.py \
  --config ../ali_config.json \
  --scale GAD-7 \
  --reset-memory
```

内部接入方式：

- `LLMAgentLoop` 触发 `PsychSOPRuntime.start()`，生成开场提示。
- `ASRResultFinal` 触发 `PsychSOPRuntime.accept_text()`。
- `PsychSOPManager` 把规则回复包装成 `ConsumeLLMAgentGenerationRequested` stream。
- 后续复用现有 `LLMAgentConsumptionManager`、`TTSManager`、`TTSPlaybackManager`、`OutputGateway`。
- 示例服务禁用默认 `LLMAgentContextManager` 对 `ASRResultFinal` 和 `LLMAgentLoop` 的处理，避免默认 LLM agent 与 PsychSOP 双响应。

## CLI 运行方式

从 `xtalk/` 目录运行：

```bash
PYTHONPATH=src python examples/psych_sop_demo/demo_cli.py --scale GAD-7 --reset-memory
```

或：

```bash
PYTHONPATH=src python examples/psych_sop_demo/demo_cli.py --scale PHQ-9 --reset-memory
```

也可以使用模块方式：

```bash
PYTHONPATH=src python -m xtalk.psych_sop.demo_cli --scale GAD-7 --reset-memory
```

参数：

```text
--scale SCALE          GAD-7 或 PHQ-9
--reset-memory         启动前清空 demo memory
--experiment-id ID     写入 episode 和 memory metadata 的实验 ID
--no-mem0              强制使用 LocalJsonMemoryBackend
```

## 一次完整流程内部发生什么

以 GAD-7 为例：

1. CLI 初始化 SOP、safety guard、agent、memory、scale engine、episode logger。
2. `SOPNavigator` 从 `START` 开始。
3. `CounselingAgent` 生成 greeting。
4. 用户输入后，`SafetyGuard.classify()` 检查风险。
5. `SOPNavigator.step()` 根据当前节点和 context 进入下一节点。
6. 到 `SCALE_SELECTION` 时，用户选择 GAD-7 / PHQ-9，或回车使用默认 scale。
7. 到 `RISK_CHECK` 时，若出现高风险表达，进入 `CRISIS_RESPONSE`。
8. 到 `SCALE_LOOP` 时，`ScaleEngine` 输出当前题目和选项。
9. 用户输入 `0` / `1` / `2` / `3`，`ScaleEngine.parse_answer()` 映射答案。
10. `ScaleEngine.record_answer()` 写入确定性状态。
11. `SOPNavigator` 进入 `RECORD_ANSWER`。
12. 如果还有下一题，`ScaleEngine.next_question()`，回到 `SCALE_LOOP`。
13. 如果没有下一题，进入 `COMPUTE_SCORE`。
14. `ScaleEngine.compute_score()` 计算总分。
15. `ScaleEngine.get_score_interpretation()` 获取解释。
16. `CounselingAgent` 在 `EXPLAIN_RESULT` 输出非诊断性解释。
17. `SUPPORTIVE_CLOSE` 输出支持性结束语。
18. `EpisodeLogger.save()` 保存 episode JSON。
19. `EvolutionSummarizer.summarize()` 生成自进化总结。
20. Memory 写入 `evolution_memory`。

## 支持的用户输入

在量表题目阶段：

```text
0
1
2
3
完全没有
有几天
超过一半天数
几乎每天
有一点
经常
跳过
解释
退出
```

行为：

- `0` / `1` / `2` / `3`：记录答案。
- 选项文字：尝试映射答案。
- 模糊频率词：尝试映射答案。
- `跳过`：记录 skipped。
- `解释`：进入 `CLARIFY_ITEM`。
- `退出`：进入 `ABORTED`。

## Safety 行为

如果用户输入明显高风险表达，例如：

```text
我想自杀
我不想活了
我想伤害自己
我想杀了别人
```

则：

1. `SafetyGuard` 返回 `risk_level="high"`。
2. `should_interrupt_sop=True`。
3. CLI 记录 safety event。
4. `SOPNavigator` 进入 `CRISIS_RESPONSE`。
5. 不再继续量表。
6. episode status 写为 `crisis`。
7. evolution summary 写入 `evolution_memory`。

危机回应文案位于：

```text
sop_template.yaml
```

当前文案不写死某个国家热线，而是建议联系当地紧急服务、可信任的人、专业机构或所在地危机热线。

## Episode Log

运行 demo 后会生成：

```text
data/psych_sop_demo/episodes/{episode_id}.json
```

示例字段：

```json
{
  "episode_id": "...",
  "user_id": "xtalk_psych_demo_user",
  "task": "psych_sop_scale_demo",
  "scale_id": "GAD-7",
  "sop_version": "psych_scale_interview_v0.1",
  "prompt_version": "psych_sop_demo_v0.1",
  "status": "completed",
  "turns": [],
  "answers": {
    "0": 1,
    "1": 1
  },
  "score": 7,
  "interpretation": {
    "label": "轻度",
    "description": "结果可能提示轻度焦虑相关困扰。"
  },
  "safety_events": [],
  "skipped_questions": [],
  "clarification_count": 0,
  "dropout": false,
  "failure_type": null
}
```

## Memory 文件

默认 local memory 文件：

```text
data/psych_sop_demo/memory.json
```

示例记录：

```json
{
  "id": "...",
  "user_id": "xtalk_psych_demo_user",
  "content": "本次 GAD-7 引导已完成...",
  "scope": "evolution_memory",
  "metadata": {
    "scope": "evolution_memory",
    "task": "psych_sop_scale_demo",
    "scale_id": "GAD-7",
    "experiment_id": "psych_sop_demo",
    "sop_version": "psych_scale_interview_v0.1",
    "prompt_version": "psych_sop_demo_v0.1",
    "result": "completed",
    "failure_type": null
  },
  "timestamp": "..."
}
```

当前 memory 是单用户调试模式，不适合作为正式用户系统。

## 测试

从 `xtalk/` 目录运行：

```bash
PYTHONPATH=src python -m pytest tests/test_psych_scale_engine.py tests/test_psych_safety_guard.py tests/test_psych_sop_navigator.py tests/test_psych_memory_backend.py tests/test_psych_episode_logger.py
```

当前测试覆盖：

- GAD-7 七道题全部回答 `1`，总分为 `7`。
- PHQ-9 九道题全部回答 `2`，总分为 `18`。
- 用户输入“我想自杀”时，SafetyGuard 返回 high risk 且中断 SOP。
- 用户输入“退出”时，SOP 进入 `ABORTED`。
- LocalJsonMemoryBackend 可以 add/search/reset/export。
- EpisodeLogger 可以保存 episode JSON。

## 与 X-Talk 主系统的关系

当前模块没有接入主 serving manager，也没有修改主 voice pipeline。

它现在是：

```text
text input
  -> SafetyGuard
  -> SOPNavigator
  -> ScaleEngine
  -> CounselingAgent
  -> text output
  -> EpisodeLogger
  -> EvolutionSummarizer
  -> MemoryBackend
```

未来接入 X-Talk voice pipeline 时，可以改成：

```text
ASRResultFinal
  -> PsychSOPManager
  -> SafetyGuard
  -> SOPNavigator
  -> ScaleEngine
  -> CounselingAgent or LLM-backed agent
  -> ResponseUpdate / ResponseFinish
  -> TTSPlaybackManager
```

建议下一步不要直接把 CLI 逻辑塞进主 manager，而是先抽象一个 `PsychSOPSession` 或 `PsychSOPRuntime`：

```python
class PsychSOPRuntime:
    def start(scale_id: str) -> str: ...
    def accept_text(user_text: str) -> str: ...
    def snapshot() -> dict: ...
    def finish() -> Path: ...
```

这样 CLI 和 voice pipeline 可以复用同一套状态机。

## 后续扩展路线

### 一、接入真实量表 JSON

当前 loader 已经支持读取：

```text
psydata/pysc/GAD-7.json
psydata/pysc/PHQ-9.json
psydata/pysc/SCL-90.json
```

但当前仓库里这些文件可能还是 Git LFS pointer。真实 JSON 到位后，需要检查：

- 字段名是否与 `ScaleLoader._normalize_spec()` 兼容。
- options 是否包含 id/description/score。
- score_interpretation 是否能被 `get_score_interpretation()` 正确解析。
- additional_questions 是否需要进入 scale loop。

### 二、细化 SOP

把 `psydata/realdata/PsychologySOP-Template` 里的流程转成 `sop_template.yaml`。

需要人工细化：

- 节点定义。
- 节点目标。
- 允许动作。
- 转移条件。
- prompt hints。
- crisis response。
- 停止、跳过、沉默、反复解释等边界行为。

### 三、升级 SafetyGuard

当前是关键词规则。

后续可以：

- 引入 LLM classifier。
- 加入 confidence。
- 区分 immediate danger 和 historical ideation。
- 引入本地化危机资源。
- 增加 human-in-the-loop 审核。

注意：即使使用 LLM classifier，也建议保留关键词硬规则作为兜底。

### 四、引入 Mem0

当前 `create_memory_backend()` 只有在存在 `MEM0_API_KEY` 时尝试使用 Mem0。

后续要明确：

- user anchor。
- session anchor。
- memory retention policy。
- memory deletion policy。
- sensitive data handling。
- 哪些内容可写入 memory。
- 哪些内容只能写入 episode log。
- 哪些内容必须不保存。

### 五、LLM-backed CounselingAgent

当前 `CounselingAgent` 是 rule-based。

后续可扩展为：

```text
deterministic state
  -> prompt builder
  -> LLM
  -> output guard
  -> final assistant text
```

但必须保证：

- LLM 不负责计算分数。
- LLM 不负责决定已记录答案。
- LLM 不绕过 safety interrupt。
- LLM 输出必须通过非诊断、安全边界检查。

### 六、接入 X-Talk 语音链路

建议步骤：

1. 抽象 `PsychSOPRuntime`。
2. CLI 改用 runtime。
3. 新增 `PsychSOPManager`，监听 `ASRResultFinal`。
4. 将 runtime 输出转成 `ResponseUpdate` / `ResponseFinish`。
5. 复用现有 TTS playback。
6. 为每个 session 存储独立 runtime。
7. 将 speaker/user id 与 memory user id 对齐。

## 当前 TODO 标注

代码中已有这些 TODO：

- `TODO(psychology): refine SOP with PsychologySOP-Template`
- `TODO(safety): replace keyword guard with validated classifier`
- `TODO(memory): switch from single-user debug memory to proper user/session anchor`
- `TODO(voice): connect CLI flow to X-Talk ASR/TTS pipeline`
- `TODO(evolution): implement human-in-the-loop SOP patch proposal`

这些 TODO 对应后续真正把 demo 推向研究原型的关键工作。

## 当前实现边界

请项目组成员特别注意：

- 这是 demo，不是医疗产品。
- 这是筛查量表引导，不是心理诊断。
- 当前结果解释只用于工程演示，需要心理学专业成员审阅。
- 当前危机识别只做关键词，不可作为安全系统上线。
- 当前 memory 是单用户调试模式，不适合多用户真实环境。
- 当前没有隐私合规、数据脱敏、访问控制、审计策略。
- 当前没有接入真实 X-Talk voice pipeline。
- 当前没有使用真实 PsychologySOP 手册生成完整 SOP。

## 推荐开发顺序

后续如果继续迭代，建议顺序是：

1. 抽象 `PsychSOPRuntime`，让 CLI 和 voice pipeline 共享状态机。
2. 用真实 JSON 替换 fallback scale spec。
3. 和心理学成员一起修订 `sop_template.yaml`。
4. 和心理学成员一起修订 score interpretation 文案。
5. 补充 crisis response 本地化配置。
6. 接入 Mem0，但先保持 local fallback。
7. 接入 X-Talk ASR/TTS。
8. 加入 LLM-backed response generation。
9. 加入 episode evaluator 和 SOP patch proposal。
10. 建立人工审查和数据安全策略。
