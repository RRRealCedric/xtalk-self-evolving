# xtalk.llm_agent

## Agent

_定义于 `xtalk.llm_agent.interfaces`。_

```python
class Agent(ABC)
```

Xtalk 所使用的会话式 Agent 抽象接口。

### Methods

#### content_to_text

```python
def content_to_text(content: Any) -> str
```

将模型内容块规范化为纯文本。

#### accept

```python
def accept(self, context: AgentContext) -> Iterable[AgentOutput]
```

接收一次增量上下文更新，并产生零个或多个输出项。

#### async_accept

```python
async def async_accept(self, context: AgentContext) -> AsyncIterator[AgentOutput]
```

异步接收一次增量上下文更新。

#### sync_iter_from_async

```python
def sync_iter_from_async(self, async_iter: AsyncIterator[T]) -> Iterable[T]
```

将异步迭代器桥接为同步生成器。

#### clone

```python
def clone(self) -> 'Agent'
```

为新会话克隆 Agent。

#### restore_history

```python
def restore_history(self, messages: list[dict[str, Any]]) -> None
```

将持久化的对话消息恢复到 Agent 状态中。

#### get_chat_history

```python
def get_chat_history(self, with_system: bool = False) -> str | None
```

返回序列化后的对话历史；`with_system` 控制是否包含系统提示。

#### add_tools

```python
def add_tools(self, tools: list[BaseTool | Callable[[], BaseTool]]) -> None
```

向 Agent 附加工具或工具工厂。

## AgentContext

_定义于 `xtalk.llm_agent.interfaces`。_

```python
class AgentContext(TypedDict)
```

Agent 接收的增量上下文更新。`type` 标识上下文流类型，`data` 携带由事件派生出的负载。

## AgentOutput

_定义于 `xtalk.llm_agent.interfaces`。_

```python
AgentOutput
```

**Value:** `Union[str, ToolCall, ToolCallResult]`

## ChatHistory

_定义于 `xtalk.llm_agent.interfaces`。_

```python
class ChatHistory
```

管理聊天历史，并支持与播放状态相关的助手消息合并。

### Methods

#### __init__

```python
def __init__(self, system_prompt: str) -> None
```

使用一条系统消息初始化历史。

#### messages

```python
def messages(self) -> list[BaseMessage]
```

返回当前消息列表。

#### append_message

```python
def append_message(self, message: BaseMessage) -> None
```

原样追加一条消息。

#### append_or_update_ai_message

```python
def append_or_update_ai_message(self, full_text: str, *, final: bool) -> None
```

追加或合并一条受播放控制的助手消息。

## DummyAgent

_定义于 `xtalk.llm_agent.dummy`。_

```python
class DummyAgent(Agent)
```

用于测试的哑 Agent。该实现总是返回相同的预设文本。

## DefaultAgent

_定义于 `xtalk.llm_agent.default`。_

```python
class DefaultAgent(Agent)
```

默认的语音优先对话 Agent 实现。

### Class Fields

- `BASE_PROMPT`
  默认系统提示词，约束回复风格、工具使用和搜索策略。

## LTSAgent

_定义于 `xtalk.llm_agent.lts`。_

```python
class LTSAgent(Agent)
```

面向长对话状态管理的 Agent 实现。

### Methods

#### accept

```python
def accept(self, context: AgentContext) -> Iterable[AgentOutput]
```

接收增量上下文更新。

#### async_accept

```python
async def async_accept(self, context: AgentContext) -> AsyncIterator[AgentOutput]
```

异步接收增量上下文更新。

#### clone

```python
def clone(self) -> 'Agent'
```

为新会话克隆 Agent。

#### restore_history

```python
def restore_history(self, messages: list[dict[str, Any]]) -> None
```

恢复持久化消息。

#### get_chat_history

```python
def get_chat_history(self, with_system: bool = False) -> str | None
```

获取序列化后的历史记录。

#### add_tools

```python
def add_tools(self, tools: list[BaseTool | Callable[[], BaseTool]]) -> None
```

附加工具或工具工厂。

## ExperimentalAgent

_定义于 `xtalk.llm_agent.experimental`。_

```python
class ExperimentalAgent(Agent)
```

实验性的 Agent 实现，支持主动问候与附和策略。

### Class Fields

- `BASE_SYSTEM_PROMPT`
  基础系统提示词。
- `GREETING_GEN_PROMPT`
  主动问候生成提示词。
- `BACKCHANNEL_JUDGE_PROMPT`
  附和判断提示词。

## PlaybackAIMessageMeta

_定义于 `xtalk.llm_agent.interfaces`。_

```python
class PlaybackAIMessageMeta
```

跟踪一条受播放管理的助手消息的合并状态。

### Class Fields

- `final: bool` = `False`
- `prefix: str | None` = `None`

## ToolCallResultArgs

_定义于 `xtalk.llm_agent.tools.utils`。_

```python
class ToolCallResultArgs(TypedDict)
```

描述一次已完成工具调用的序列化结果；其中 `name` 保存工具名，`args` 保存原始参数，`content` 保存文本结果。

## ToolCallResult

_定义于 `xtalk.llm_agent.tools.utils`。_

```python
class ToolCallResult(ToolCall)
```

工具执行完成后发出的结构化工具调用结果事件。
