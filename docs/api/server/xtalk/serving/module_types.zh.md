# xtalk.serving.module_types

## OutputGateway

_定义于 `xtalk.serving.modules.output_gateway`。_

```python
class OutputGateway(EventListenerMixin)
```

将后端事件转发到前端 WebSocket。

### Parameters

- `event_bus` (`EventBus`)
  用于订阅会话事件的事件总线。
- `session_id` (`str`)
  返回给前端的会话标识符。
- `websocket` (`WebSocket`)
  用于发送出站消息的实时 WebSocket 连接。
- `config` (`dict[str, Any] | None, optional`)
  与输出行为相关的服务配置。

## ASRManager

_定义于 `xtalk.serving.modules.asr_manager`。_

```python
class ASRManager(Manager)
```

会话级 ASR 管理器。

## DirectAudioManager

_定义于 `xtalk.serving.modules.direct_audio_manager`。_

```python
class DirectAudioManager(Manager)
```

将 `direct_audio` 工具调用转发到外发音频流。

## EmbeddingsManager

_定义于 `xtalk.serving.modules.embeddings_manager`。_

```python
class EmbeddingsManager(Manager)
```

会话级嵌入写入与检索存储管理器。

## EnhancerManager

_定义于 `xtalk.serving.modules.enhancer_manager`。_

```python
class EnhancerManager(Manager)
```

后端语音增强管理器。

## LatencyManager

_定义于 `xtalk.serving.modules.latency_manager`。_

```python
class LatencyManager(EventListenerMixin)
```

监听 VAD、ASR、LLM、TTS 事件的会话级延迟跟踪器。

## LLMAgentContextManager

_定义于 `xtalk.serving.modules.llm_agent_context_manager`。_

```python
class LLMAgentContextManager(Manager)
```

将会话上下文事件转发给配置好的 LLM Agent。

## LLMAgentConsumptionManager

_定义于 `xtalk.serving.modules.llm_agent_consumption_manager`。_

```python
class LLMAgentConsumptionManager(Manager)
```

消费 Agent 产生的流式输出，并将其转成后续事件。

## SpeakerManager

_定义于 `xtalk.serving.modules.speaker_manager`。_

```python
class SpeakerManager(Manager)
```

会话作用域的说话人识别管理器。

## TTSPlaybackManager

_定义于 `xtalk.serving.modules.tts_playback_manager`。_

```python
class TTSPlaybackManager(Manager)
```

跟踪前端播放进度并协调 TTS 播放状态。

## TTSManager

_定义于 `xtalk.serving.modules.tts_manager`。_

```python
class TTSManager(Manager)
```

处理流式合成和控制的事件驱动 TTS 管理器。

## TurnTakingManager

_定义于 `xtalk.serving.modules.turn_taking_manager`。_

```python
class TurnTakingManager(Manager)
```

协调回合切换与打断策略。

## VADManager

_定义于 `xtalk.serving.modules.vad_manager`。_

```python
class VADManager(Manager)
```

语音活动检测管理器。
