# xtalk.model_types

## Embeddings

_定义于 `langchain_core.embeddings`。_

```python
from langchain_core.embeddings import Embeddings
```

该模块重新导出的外部依赖。

## BaseChatModel

_定义于 `langchain_core.language_models.chat_models`。_

```python
from langchain_core.language_models.chat_models import BaseChatModel
```

该模块重新导出的外部依赖。

## Agent

_定义于 `xtalk.llm_agent.interfaces`。_

```python
class Agent(ABC)
```

Xtalk 所使用的会话式 Agent 抽象接口。

### Methods

#### content_to_text

_定义于 `xtalk.llm_agent.interfaces`。_

```python
def content_to_text(content: Any) -> str
```

将模型内容块规范化为纯文本。

##### Parameters

- `content`
  LangChain 模型消息或分块产生的内容。

##### Returns

- `str`
  从输入中提取出的纯文本内容。

#### accept

_定义于 `xtalk.llm_agent.interfaces`。_

```python
def accept(self, context: AgentContext) -> Iterable[AgentOutput]
```

接收一次增量上下文更新。

##### Parameters

- `context` (`AgentContext`)
  由服务层事件转发而来的上下文负载。

##### Yields

- `AgentStreamItem`
  由该次更新触发的零个或多个流式输出项。

#### async_accept

_定义于 `xtalk.llm_agent.interfaces`。_

```python
async def async_accept(self, context: AgentContext) -> AsyncIterator[AgentOutput]
```

异步接收一次增量上下文更新。

##### Parameters

- `context` (`AgentContext`)
  由服务层事件转发而来的上下文负载。

##### Yields

- `AgentStreamItem`
  由该次更新触发的流式输出项。

#### sync_iter_from_async

_定义于 `xtalk.llm_agent.interfaces`。_

```python
def sync_iter_from_async(self, async_iter: AsyncIterator[T]) -> Iterable[T]
```

将异步迭代器转换为同步生成器。

##### Parameters

- `async_iter` (`AsyncIterator[T]`)
  待桥接的异步迭代器。

##### Yields

- `T`
  `async_iter` 产生的各项内容。

#### clone

_定义于 `xtalk.llm_agent.interfaces`。_

```python
def clone(self) -> 'Agent'
```

为新会话克隆 Agent。

##### Returns

- `Agent`
  适用于新会话的 Agent 实例。

#### restore_history

_定义于 `xtalk.llm_agent.interfaces`。_

```python
def restore_history(self, messages: list[dict[str, Any]]) -> None
```

将持久化的对话消息恢复到 Agent 状态中。

#### get_chat_history

_定义于 `xtalk.llm_agent.interfaces`。_

```python
def get_chat_history(self, with_system: bool = False) -> str | None
```

当可用时，返回序列化后的对话历史。

##### Parameters

- `with_system` (`bool, optional`)
  在具体实现支持时，是否包含系统提示消息。

##### Returns

- `str | None`
  对话历史；如果没有则为 `None`。

#### add_tools

_定义于 `xtalk.llm_agent.interfaces`。_

```python
def add_tools(self, tools: list[BaseTool | Callable[[], BaseTool]]) -> None
```

向 Agent 附加工具。

##### Parameters

- `tools` (`list[BaseTool | Callable[[], BaseTool]]`)
  工具实例，或返回工具实例的工厂函数。

## Rewriter

_定义于 `xtalk.rewriter.interfaces`。_

```python
class Rewriter(ABC)
```

文本重写辅助组件的抽象接口。

### Methods

#### rewrite

_定义于 `xtalk.rewriter.interfaces`。_

```python
def rewrite(self, input: str) -> str
```

重写输入文本。

#### async_rewrite

_定义于 `xtalk.rewriter.interfaces`。_

```python
async def async_rewrite(self, input: str) -> str
```

异步重写输入文本。

## ASR

_定义于 `xtalk.speech.interfaces`。_

```python
class ASR(ABC)
```

自动语音识别的抽象接口。

### Methods

#### recognize

_定义于 `xtalk.speech.interfaces`。_

```python
def recognize(self, audio: bytes) -> str
```

识别完整音频缓冲区。

#### recognize_stream

_定义于 `xtalk.speech.interfaces`。_

```python
def recognize_stream(self, audio: bytes, *, is_final: bool = False, chat_history: str | None = None) -> str
```

以流式模式增量识别音频。

##### Parameters

- `audio` (`bytes`)
  增量 PCM 16-bit 单声道音频字节。
- `is_final` (`bool, optional`)
  调用方是否希望把当前点视为一个临时边界，并在需要时刷出原本会保留在缓冲中的尾部音频。这只是解码提示，并不意味着必须重置流式状态；之前已经识别出的文本应继续保留，以便后续音频在累计结果上继续识别。
- `chat_history` (`str | None, optional`)
  当前会话的序列化聊天历史；在不可用时不包含进行中的轮次。

##### Returns

- `str`
  当前识别结果。

#### stream_chunk_bytes_hint

_定义于 `xtalk.speech.interfaces`。_

```python
def stream_chunk_bytes_hint(self) -> int | None
```

返回建议的流式分块大小。

##### Returns

- `int | None`
  建议每次传给 `recognize_stream` 的分块字节数；如果没有偏好则为 `None`。

#### reset

_定义于 `xtalk.speech.interfaces`。_

```python
def reset(self) -> None
```

重置内部识别状态。

#### clone

_定义于 `xtalk.speech.interfaces`。_

```python
def clone(self) -> 'ASR'
```

为新会话克隆 ASR 实例。

#### async_recognize

_定义于 `xtalk.speech.interfaces`。_

```python
async def async_recognize(self, audio: bytes) -> str
```

异步识别完整音频缓冲区。

#### async_recognize_stream

_定义于 `xtalk.speech.interfaces`。_

```python
async def async_recognize_stream(self, audio: bytes, *, is_final: bool = False, chat_history: str | None = None) -> str
```

异步识别增量音频输入。

##### Parameters

- `audio` (`bytes`)
  增量 PCM 16-bit 单声道音频字节。
- `is_final` (`bool, optional`)
  调用方是否希望把当前点视为一个临时边界，并在需要时刷出原本会保留在缓冲中的尾部音频。这只是解码提示，并不意味着必须重置流式状态；之前已经识别出的文本应继续保留，以便后续音频在累计结果上继续识别。
- `chat_history` (`str | None, optional`)
  当前会话的序列化聊天历史；在不可用时不包含进行中的轮次。

##### Returns

- `str`
  当前识别结果。

## TTS

_定义于 `xtalk.speech.interfaces`。_

```python
class TTS(ABC)
```

文本转语音引擎的抽象基类。

## Captioner

_定义于 `xtalk.speech.interfaces`。_

```python
class Captioner(ABC)
```

音频描述模型的抽象基类。

## PuntRestorer

_定义于 `xtalk.speech.interfaces`。_

```python
class PuntRestorer(ABC)
```

标点恢复模型的抽象基类。

## VAD

_定义于 `xtalk.speech.interfaces`。_

```python
class VAD(ABC)
```

语音活动检测引擎的抽象基类。

## SpeechEnhancer

_定义于 `xtalk.speech.interfaces`。_

```python
class SpeechEnhancer(ABC)
```

语音增强引擎的抽象基类。

### Notes

输入与输出均使用 16 kHz 的 PCM 16-bit 单声道音频字节。

## SpeakerEncoder

_定义于 `xtalk.speech.interfaces`。_

```python
class SpeakerEncoder(ABC)
```

说话人嵌入模型的抽象基类。

## SpeechSpeedController

_定义于 `xtalk.speech.interfaces`。_

```python
class SpeechSpeedController(ABC)
```

TTS 语速控制器接口。

## TurnDetector

_定义于 `xtalk.speech.interfaces`。_

```python
class TurnDetector(ABC)
```

轮次检测器的抽象接口。

### Methods

#### detect

_定义于 `xtalk.speech.interfaces`。_

```python
def detect(self, audio: Optional[bytes] = None, text: Optional[str] = None, speech_start: bool = False, speech_pause: Optional[bool] = None) -> TurnDetectionResult
```

根据音频和文本检测当前回合状态。

##### Parameters

- `audio` (`bytes | None, optional`)
  当前 16 kHz PCM 16-bit 单声道音频帧。
- `text` (`str | None, optional`)
  当前回合的 ASR 文本。
- `speech_start` (`bool, optional`)
  VAD 是否刚刚检测到语音开始。该参数可以在没有 `audio` 或 `text` 的情况下单独提供。
- `speech_pause` (`bool | None, optional`)
  用户是否看起来暂停了说话。

#### async_detect

_定义于 `xtalk.speech.interfaces`。_

```python
async def async_detect(self, audio: Optional[bytes] = None, text: Optional[str] = None, speech_start: bool = False, speech_pause: Optional[bool] = None) -> TurnDetectionResult
```

异步检测当前回合状态。

##### Parameters

- `audio` (`bytes | None, optional`)
  当前 16 kHz PCM 16-bit 单声道音频帧。
- `text` (`str | None, optional`)
  当前回合的 ASR 文本。
- `speech_start` (`bool, optional`)
  VAD 是否刚刚检测到语音开始。该参数可以在没有 `audio` 或 `text` 的情况下单独提供。
- `speech_pause` (`bool | None, optional`)
  用户是否看起来暂停了说话。
