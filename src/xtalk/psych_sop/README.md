# Psychology SOP Agent

## 项目是什么

xtalk.psych_sop 是一个用于研究结构化心理访谈流程的工程原型。它将问题、流程、安全规则、回答生成、轻量记忆和会话记录拆开实现，以便在命令行和 X-Talk 语音链路中验证流程。

“SOP”是 Standard Operating Procedure 的缩写，中文可理解为标准操作流程。它表示系统按照预先定义的节点和规则推进，而不是让模型完全自由发挥。一次从开始到结束的会话记录称为一个 episode。

本项目不是医疗产品，也不是诊断系统。它用于工程演示、流程研究、模型协作验证和日志分析，不能替代医生、精神科医生、心理咨询师或当地紧急服务。

## 当前能力与阅读路线

项目目前有两条相关但不同的能力线：

1. 量表 SOP 演示模式：以确定性流程执行 GAD-7、PHQ-9 等常见筛查量表，适合理解基本模块、命令行运行方式和数据记录。
2. SCID 结构化语音访谈模式：SCID 是结构化临床访谈的简称；本项目将其用于非诊断性的流程研究，面向实时语音和长会话。详细说明见 [SCID README](scid/README.md)。

建议首次阅读按以下顺序进行：

~~~text
项目边界
  → 运行文本演示
  → 了解一次量表会话如何推进
  → 了解核心模块
  → 了解语音和 SCID 模式
  → 查看数据、测试和后续规划
~~~

## 非诊断与安全边界

项目必须遵守以下边界：

- 不进行临床诊断。
- 不提供药物建议。
- 不声称替代心理咨询、心理治疗或精神科就诊。
- 不承诺治疗效果。
- 量表结果只能描述为“量表结果”“筛查结果”或“可能提示”，不能直接称为诊断。
- 用户表达自伤、自杀、伤害他人或立即危险时，系统应停止普通流程并转入危机回应。
- 危机回应应优先建议联系当地紧急服务、身边可信任的人、专业机构或所在地危机热线。
- 当前安全识别仍以规则为主，不能作为经过临床验证的安全分类器。
- 当前记忆默认是单用户演示模式，不适合作为真实多用户系统直接上线。

## 五分钟运行文本演示

从 xtalk 目录运行：

~~~bash
PYTHONPATH=src python examples/psych_sop_demo/demo_cli.py \
  --scale GAD-7 \
  --reset-memory
~~~

也可以使用模块方式：

~~~bash
PYTHONPATH=src python -m xtalk.psych_sop.demo_cli \
  --scale PHQ-9 \
  --reset-memory
~~~

常用参数：

~~~text
--scale SCALE          GAD-7 或 PHQ-9
--reset-memory         启动前清空演示记忆
--experiment-id ID     写入会话记录和记忆元数据的实验 ID
--no-mem0              强制使用本地 JSON 记忆
~~~

运行结果主要写入：

~~~text
data/psych_sop_demo/memory.json
data/psych_sop_demo/episodes/{episode_id}.json
~~~

## 量表 SOP 演示模式

量表 SOP 演示验证的是以下工程链路：

~~~text
用户文本
  → 安全检查
  → SOP 流程导航
  → 量表问题与答案记录
  → 分数计算
  → 非诊断性解释
  → 会话记录和记忆
~~~

当前版本是规则驱动的演示模式，不要求真实大语言模型，也不要求 Mem0 才能运行。

### 一次会话如何推进

以 GAD-7 为例：

1. 初始化 SOP 流程、量表引擎、安全规则、回复生成器、记忆后端和会话记录器。
2. 流程导航器从 START 节点开始，回复生成器输出边界说明。
3. 每次用户输入先经过安全检查。
4. 流程导航器根据当前节点和上下文条件选择下一步。
5. 用户选择量表；如直接回车，可以使用默认量表。
6. 量表引擎显示当前问题和选项，并解析用户答案。
7. 答案写入确定性状态；如果还有下一题，流程进入下一题。
8. 最后一题完成后，量表引擎计算总分，回复生成器输出非诊断性解释。
9. 系统输出结束语，保存 episode，并写入本次会话的工程总结。

量表题目阶段支持的典型输入：

~~~text
0 / 1 / 2 / 3
完全没有
有几天
超过一半天数
几乎每天
有一点
经常
跳过
解释
退出
~~~

系统会尽量将自然语言映射到选项；如果解析置信度太低，会要求用户确认，而不是擅自记录答案。

## 基本概念

| 名称 | 含义 |
|---|---|
| 量表 | 由固定题目和选项组成的结构化筛查工具。 |
| SOP | 规定访谈应如何开始、提问、澄清、结束或处理中断的流程。 |
| 节点 | SOP 中的一个明确步骤，例如“选择量表”或“计算分数”。 |
| 状态机 | 系统在当前节点上，根据输入和条件转移到允许的下一节点的机制。 |
| 运行时 | 协调输入、状态、规则、回复和记录的会话执行层。 |
| 记忆 | 供后续会话检索的摘要信息，不是量表的正式答案状态。 |
| Episode | 一次从开始到结束的完整会话记录。 |
| 备用定义 | 原始资源不可用时，为保持演示可运行而使用的内置后备数据。 |
| ASR | 自动语音识别，将用户语音转换为文本。 |
| TTS | 文字转语音，将系统文本播放给用户。 |

## 核心模块

### 量表：scale_schema.py、scale_loader.py 与 scale_engine.py

scale_schema.py 定义量表数据：

- ScaleOption：选项编号、说明和分数；
- ScaleSpec：量表名称、介绍、题目、选项和分数解释；
- ScaleSessionState：当前题号、答案、跳过题目、状态、时间和中止原因。

scale_loader.py 会依次从仓库外部的 psydata/pysc 和包内的 src/xtalk/psych_sop/data/pysc 读取量表 JSON。JSON 是一种结构化文本数据格式。目前支持：

~~~text
GAD-7
PHQ-9
SCL-90
~~~

外部目录中的 JSON 可能是 Git LFS 指针。Git LFS 是把大文件存放在外部、仓库中只保存引用的 Git 扩展。如果两个目录都没有可用 JSON，或文件格式不合法，加载器会使用内置备用定义。备用定义仅用于工程演示，不代表最终专业量表版本。

scale_engine.py 负责题目顺序、答案解析、答案记录和分数计算。常用接口包括：

~~~python
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
~~~

当前题号、答案、跳过题目和最终分数属于确定性状态：模型可以帮助理解用户表达，但不能替代引擎计算分数或改变题目顺序。

### 流程：sop_schema.py、sop_template.yaml 与 sop_navigator.py

sop_schema.py 定义流程数据：

- SOPNode：节点编号、目标、允许动作、转移条件和提示；
- SOPSpec：完整流程、全局规则、危机回应和全部节点；
- NextAction：下一节点、下一动作、原因、是否结束和附加数据。

sop_template.yaml 是可编辑流程配置。YAML 是一种适合人工编辑的结构化配置文本。当前包含：

~~~text
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
~~~

每个节点包含 id、goal、allowed_actions、transitions 和 prompt_hints。全局规则和危机回应也在该 YAML 中，便于在专业审核后编辑。

sop_navigator.py 是规则版流程导航器。核心接口：

~~~python
class SOPNavigator:
    def __init__(self, sop_spec): ...
    @classmethod
    def from_yaml(cls, path=None): ...
    def current_node(self): ...
    def allowed_actions(self): ...
    def set_node(self, node_id): ...
    def step(self, user_input, context): ...
~~~

第一版读取的上下文标记包括：

~~~text
safety_interrupt
high_risk
selected_scale
needs_clarification
user_skips
answer_recorded
has_next_question
score_computed
~~~

未来可以让模型辅助识别用户意图，但最终流程转移仍应经过规则和状态校验。

### 安全：safety_rules.yaml 与 safety_guard.py

safety_rules.yaml 保存可编辑的安全关键词和风险类别，例如：

~~~yaml
high:
  self_harm:
  harm_to_others:

moderate:
  panic:
  unknown:
~~~

SafetyGuard.classify(text) 返回：

- risk_level：none、low、moderate 或 high；
- risk_type：自伤、伤害他人、虐待、惊恐或未知；
- matched_signals：命中的信号；
- should_interrupt_sop：是否应立即中断普通流程。

当前高风险表达会中断 SOP 并进入 CRISIS_RESPONSE。它是演示级规则，不是经过临床验证的安全识别器。

### 回复：prompts.yaml 与 counseling_agent.py

prompts.yaml 保存回复风格、边界说明和选项提示。当前 CounselingAgent 是规则版回复生成器，因此 YAML 是可编辑行为配置，不等同于真实模型的系统提示词。

它会结合当前 SOP 节点、量表题目、选项、进度、分数、结果解释、危机回应和记忆上下文生成中文回复，并遵守：

- 温和、简短、不评判；
- 一次只问一个问题；
- 不暗示用户应选择什么答案；
- 尊重跳过和退出；
- 不改变量表题意；
- 解释结果时强调非诊断性。

未来可以扩展为模型生成，但必须保留确定性状态、安全中断和输出检查。

### 运行时：runtime.py

PsychSOPRuntime 是量表 SOP 的会话协调层。CLI、测试和语音示例应通过它推进会话，而不是各自复制状态机逻辑。

~~~python
runtime = PsychSOPRuntime(scale_id="GAD-7")
assistant_text = runtime.start()
assistant_text = runtime.accept_text("同意")
snapshot = runtime.snapshot()
episode_path = runtime.finish(status="aborted")
~~~

它负责初始化各组件、接收一轮文本、推进流程、输出回复，并在完成、危机或中止时保存会话记录。它不读取标准输入、不直接打印，因此可被命令行程序（CLI）、X-Talk 管理器和测试复用。

### 记忆：memory_backend.py

记忆后端保存后续可检索的摘要，不保存量表的正式答案状态。

~~~python
class PsychMemoryBackend:
    def search(query, scope=None, top_k=5): ...
    def add_dialogue_turn(user_text, assistant_text, metadata=None): ...
    def add_note(content, scope, metadata=None): ...
    def reset(): ...
    def export(path): ...
~~~

当前演示用户标识：

~~~python
DEBUG_USER_ID = "xtalk_psych_demo_user"
~~~

记忆范围：

- dialogue_memory：对话偏好和个性化上下文；
- scale_state_summary：最近一次量表结果摘要；
- evolution_memory：完成、退出、失败和改进建议总结。

可用实现：

- LocalJsonMemoryBackend：默认本地后备实现，写入 data/psych_sop_demo/memory.json；
- Mem0MemoryBackend：可选的外部记忆服务适配器，仅在安装 Mem0 且设置 MEM0_API_KEY 时尝试使用；初始化失败会回退到本地 JSON。

当前记忆为单用户调试模式。正式使用前必须明确用户标识、会话标识、保留期限、删除策略和敏感信息处理方式。

### 会话记录：episode_logger.py 与 evolution_summarizer.py

episode_logger.py 将一次完整会话保存到：

~~~text
data/psych_sop_demo/episodes/
~~~

记录包含会话标识、量表、流程版本、输入输出回合、答案、分数、结果解释、安全事件、跳过题目、退出状态和失败信息。它是工程演示记录，不等同于医疗病历。

evolution_summarizer.py 是规则版会话总结器：

- 完成时记录完成状态、跳过题数和澄清次数；
- 危机时记录安全中断，并提示检查危机识别和资源文案；
- 中止时记录退出，并提示优化继续或保存进度策略；
- 失败时提示检查 episode 日志。

总结写入 evolution_memory，用于工程改进，不是对用户的临床判断。

## SCID 结构化语音访谈模式

SCID 模式位于 [scid/](scid/)。它与量表模式共享非诊断和安全边界，但使用面向实时语音和长会话的独立运行时。

典型路径：

~~~text
用户语音
  → ASR 得到部分和最终文本
  → SCIDDualLMManager
  → SCIDDualLMRuntime
       ├─ Talker：前台自然承接
       ├─ Observer：增量理解和候选澄清
       ├─ Assessor：正式评估和字段推进
       ├─ Blackboard：保存候选状态
       ├─ Broker：选择当前可播放动作
       └─ Ledger：保存正式结构化状态
  → TTS
  → 用户听到回复
~~~

其中：

- Talker 只负责如何表达，不评分、不推进正式字段；
- Observer 提供对话理解、候选证据和澄清方向，不能直接提交评分；
- Assessor 负责正式评估决策，但结果必须通过当前轮次、字段和状态版本校验；
- Blackboard 保存临时和候选信息，不是正式结论来源；
- Broker 从多个候选动作中选择一个当前可播放的动作；
- Ledger 是正式结构化状态的唯一来源。

SCID Runtime 还包括：

- SessionActor：每个会话一个串行状态写入者；
- TurnSupervisor：管理单轮的并发任务、截止时间和取消；
- ModelGateway：管理模型并发、优先级、超时、重试和熔断；
- SpeculationSaga：管理可选“提前问下一步”及其失败后修复的状态机；
- EpisodeEventStore：保存脱敏事件、快照和可选原文。

这使前台可以快速回应，后台可以谨慎评估；旧轮、迟到或过期的模型结果不会覆盖当前正式状态。详细设计、事件状态和长会话约束请阅读 [SCID README](scid/README.md)。

## X-Talk 语音接入

### 量表 SOP 语音示例

[examples/psych_sop_voice_demo/server.py](../../../examples/psych_sop_voice_demo/server.py) 是独立的 X-Talk 语音示例服务。它不会修改默认服务，而是在示例中注册 PsychSOPManager。

主要路径：

~~~text
ASRResultFinal
  → PsychSOPManager
  → PsychSOPRuntime.accept_text()
  → 规则回复或模型回复
  → ConsumeLLMAgentGenerationRequested
  → X-Talk 语音消费层
  → TTS 播放
~~~

ASRResultFinal 表示一次最终识别结果；ConsumeLLMAgentGenerationRequested 是 X-Talk 内部请求消费一段生成内容的事件。

运行示例：

~~~bash
PYTHONPATH=src python examples/psych_sop_voice_demo/server.py \
  --config ../ali_config.json \
  --scale GAD-7 \
  --reset-memory
~~~

示例服务会禁用默认模型管理器对相同输入事件的处理，避免默认 Agent 和 PsychSOP 同时回复。

### SCID 语音示例

~~~bash
cd xtalk
DEEPSEEK_API_KEY=<your_key> PYTHONPATH=src \
python examples/psych_sop_voice_demo/server.py \
  --config ../ali_config.json \
  --mode scid \
  --backend-model deepseek-v4-pro
~~~

安全要求：

- DeepSeek 密钥只从 DEEPSEEK_API_KEY 环境变量读取，不写入配置或日志；
- 前台模型只负责口语化表达，不负责评分或诊断；
- 后台模型输出必须经过 JSON 解析和 AssessmentLedger 校验后才能写入正式状态；
- 当前 SCID 覆盖扫描模块和 F/G/K 重点模块入口，不是完整的 346 页 SCID 手册实现。

## 文件结构

~~~text
src/xtalk/psych_sop/
├── README.md
├── scale_schema.py          量表数据结构
├── scale_loader.py          量表加载
├── scale_engine.py          量表执行和计分
├── sop_schema.py            SOP 数据结构
├── sop_template.yaml        SOP 节点和转移配置
├── sop_navigator.py         SOP 流程导航
├── safety_rules.yaml        安全规则配置
├── safety_guard.py          安全检查
├── prompts.yaml             回复风格配置
├── counseling_agent.py      回复生成
├── runtime.py               量表 SOP 运行时
├── memory_backend.py        记忆后端
├── episode_logger.py        会话记录
├── evolution_summarizer.py  会话总结
└── scid/                    SCID 实时语音模式

examples/
├── psych_sop_demo/demo_cli.py
└── psych_sop_voice_demo/server.py
~~~

## 数据、日志与隐私

量表演示默认写入：

~~~text
data/psych_sop_demo/memory.json
data/psych_sop_demo/episodes/
~~~

SCID Runtime 还会保存事件日志、进行中的快照、终态快照，以及在显式开启原文记录时保存独立的原文文件。

当前记录可以帮助复盘：

- 会话是完成、危机中断还是中止；
- 哪些题目被回答或跳过；
- 分数和结构化状态；
- 哪些安全事件发生过；
- SCID 中哪些状态迁移、动作选择和错误发生过。

默认记录不保证保存完整逐字对话和每次模型调用的原始输入输出。要建设逐轮模型延迟、结论、依据、前台片段和状态差异报告，应按 [审计优化方案](../../../data/psych_sop_demo/审计优化方案.md) 实施。

## 测试

从 xtalk 目录运行量表演示回归：

~~~bash
PYTHONPATH=src python -m pytest \
  tests/test_psych_scale_engine.py \
  tests/test_psych_safety_guard.py \
  tests/test_psych_sop_navigator.py \
  tests/test_psych_memory_backend.py \
  tests/test_psych_episode_logger.py
~~~

当前测试覆盖：

- GAD-7 七道题全部回答 1 时总分为 7；
- PHQ-9 九道题全部回答 2 时总分为 18；
- 输入“我想自杀”时返回高风险并中断 SOP；
- 输入“退出”时进入 ABORTED；
- 本地 JSON 记忆可以新增、搜索、重置和导出；
- EpisodeLogger 可以保存会话 JSON。

SCID 的测试覆盖状态图、评估事务、观察结果、动作仲裁、超时后的等待承接、提前推进与修复状态机、事件存储和长会话约束。具体命令请参考 [SCID README](scid/README.md)。

## 当前限制

- 本项目是研究和工程演示原型，不是医疗产品。
- 量表引导不是心理诊断；结果解释需要心理学专业成员审核。
- 当前危机识别主要是关键词规则，不可独立作为生产安全系统。
- 当前记忆为单用户调试模式，不适合直接用于真实多用户。
- 当前默认配置没有完整的隐私合规、访问控制和数据生命周期管理。
- SCID 通过示例服务接入 X-Talk；它不是完整 SCID 手册实现。
- 当前没有依据完整 PsychologySOP 手册生成完整 SOP。
- 事件日志主要用于进程内追踪和事后复盘，不保证跨进程崩溃后的自动续接。

## 后续工作

### 量表和流程专业化

- 用经过审核的真实量表 JSON 替换备用定义；
- 与心理学成员共同审核 SOP 节点、转移条件、结果解释和危机文案；
- 细化停止、跳过、沉默、重复解释和题意澄清等边界行为；
- 将 PsychologySOP-Template 转换为经过审核的流程配置。

### 安全、记忆和数据治理

- 区分立即危险与历史性自伤想法，并补充本地化危机资源；
- 明确用户和会话标识、记忆范围、删除策略和保留期限；
- 为原文、模型输出和事件日志设计访问控制和脱敏策略；
- 建立真实数据的人类审核流程。

### 实时语音和模型协作

- 完善量表 SOP 的 ASR/TTS 接入和会话隔离；
- 在确定性状态与输出安全检查之外，接入可替换的模型回复生成器；
- 完善 SCID Talker、Observer、Assessor、Ledger、Broker 和事件审计；
- 记录每次模型调用的延迟、结构化结论、采用/拒绝原因和用户可见输出；
- 在真实数据和人工审核基础上评估小型控制路由模型，以及从语音中提取停顿和打断线索的 Captioner。

### 访谈话语规划

当前基础流程主要是“输入 → 路由 → 观察/评估 → 动作”。长会话还需要独立的访谈规划层，决定当前应承接、解释、澄清、追问、等待还是结束，并避免跨轮重复表达。

规划方向包括：

- Captioner：从语音中提取停顿、犹豫、打断和未说完等线索的组件；
- Observer contribution：Observer 提供给前台的自然对话建议，不直接提交诊断状态；
- Interview Planner / Discourse Planner：访谈话语规划器，根据当前字段目标、用户话轮、Observer 信息和 Assessor 指令生成本轮话语计划；
- Floor Controller：话轮控制器，决定当前由用户继续表达，还是由系统接话；
- Question Semantic Contract：问题语义约定，保存当前问题要确认的内容、已覆盖信息和禁止重复内容；
- Speech Queue：语音片段队列，管理多段前台输出、取消、替换和播放节奏。

规划层不能取代 Assessor。Assessor 仍是正式评估和 Ledger 推进的唯一来源；Planner 只负责组织访谈语言和话轮节奏。

### 审计优化

审计建设应按以下顺序推进：

1. 从已有事件和快照生成只读的会话与逐轮 Markdown 报告；
2. 增加模型调用记录，记录 Router、Observer、Assessor、Talker 的请求身份、排队时间、推理时间、结果和错误；
3. 统一记录初始承接、等待 bridge、Observer probe、Assessor action 和 repair 等前台片段；
4. 记录 Ledger 状态前后差异和过期结果的拒绝原因；
5. 增加 Markdown、静态 HTML、CSV 和命令行查询；
6. 在 500 轮长会话和并发会话中验证审计数据不造成状态写入阻塞、内存失控或原文泄露。

完整计划见 [审计优化方案](../../../data/psych_sop_demo/审计优化方案.md)。

## 代码中的后续工作标记

代码中仍有以下 TODO：

~~~text
TODO(psychology): refine SOP with PsychologySOP-Template
TODO(safety): replace keyword guard with validated classifier
TODO(memory): switch from single-user debug memory to proper user/session anchor
TODO(voice): connect CLI flow to X-Talk ASR/TTS pipeline
TODO(evolution): implement human-in-the-loop SOP patch proposal
~~~

其中部分语音和 SCID 接入已有示例实现；TODO 表示它们尚未成为完整、统一、生产级能力，不能据此宣称工作已经完成。

## 推荐开发顺序

1. 以 PsychSOPRuntime 作为 CLI 和量表语音示例的共同状态入口。
2. 用经过审核的真实量表定义替换备用量表定义。
3. 与心理学成员共同审核 SOP 节点、结果解释和危机文案。
4. 补充危机回应本地化配置和安全测试。
5. 明确用户和会话记忆边界，再接入 Mem0 或其他记忆服务。
6. 完善 X-Talk ASR/TTS 语音链路和会话隔离。
7. 在输出安全检查外接入可替换的模型回复生成。
8. 完善 SCID Runtime 的事件审计和逐轮报告。
9. 以审计数据为基础实现访谈话语规划层。
10. 建立人工审查、数据安全和长期回归测试流程。
