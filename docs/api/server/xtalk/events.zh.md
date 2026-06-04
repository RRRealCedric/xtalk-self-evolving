# xtalk.events

## BaseEvent

_定义于 `xtalk.serving.events`。_

```python
@dataclass
class BaseEvent
```

所有 Xtalk 事件的基础 dataclass。

### Parameters

- `session_id` (`str`)
  与该事件关联的会话标识符。

### Attributes

- `timestamp` (`float`)
  事件实例创建时记录的 Unix 时间戳。
- `session_id` (`str`)
  与该事件关联的会话标识符。
- `TYPE` (`str`)
  事件总线使用的稳定事件类型字符串。

## create_event_class

_定义于 `xtalk.serving.events`。_

```python
def create_event_class(*, name: str, fields: dict[str, Any] | None = None, type_name: str | None = None) -> Type[BaseEvent]
```

动态创建 `BaseEvent` 子类。

### Parameters

- `name` (`str`)
  生成事件类型的 dataclass 名称。
- `fields` (`dict[str, Any] | None, optional`)
  字段名到默认值的映射，值类型会从默认值推断。
- `type_name` (`str | None, optional`)
  事件总线类型字符串。省略时默认使用 `name.lower()`。

### Returns

- `Type[BaseEvent]`
  继承自 `BaseEvent` 的 dataclass 类型。

## WebSocketMessageReceived

收到的 WebSocket 消息。

## AudioFrameReceived

收到的音频帧。

## EnhancedAudioFrameReceived

供下游 ASR/VAD 使用的增强音频帧。

## VADSpeechStart

VAD 检测到语音开始。

## VADSpeechEnd

VAD 检测到语音结束。

## ASRResultPartial

ASR 增量识别结果。

## ASRResultFinal

ASR 最终识别结果。

## LLMFirstChunk

第一个 LLM 片段或工具调用事件，用于度量首 token 延迟。

## LLMFirstSentence

第一个可合成句子事件，用于度量句级延迟。

## TTSStarted

TTS 开始事件。

## TTSStopped

TTS 停止事件。

## TTSPaused

TTS 暂停事件。

## TTSResumed

TTS 恢复事件。

## TTSFinished

TTS 完成事件。

## LLMAgentResponseUpdate

Agent 响应增量更新事件。

## LLMAgentResponseFinish

某一轮中的 Agent 最终文本。

## ResponseUpdate

对外统一的响应增量事件。

## ResponseFinish

对外统一的响应完成事件。

## TTSTextSynthesized

一段文本已经完成 TTS 合成。

## TTSVoiceChange

TTS 参考音频切换事件。

## TTSEmotionChange

TTS 情绪切换事件。

## TTSSpeedChange

TTS 语速切换事件。

## TTSChunkReady

TTS 音频块已就绪。

## TTSChunkPlayed

前端确认某个 TTS 音频块已经播放完成。

## TTSPlaybackFinished

TTS 播放完成事件。

## FullAudioFrameReady

完整音频帧准备就绪事件。

## ErrorOccurred

错误事件。

## CaptionUpdated

Caption 更新事件。

## ToolCallOccurred

LLM 或 Agent 工具调用通知。

## RetrievalUpdated

检索结果更新事件。

## EmbeddingStatusUpdated

嵌入写入状态更新事件。

## LLMAgentLoop

驱动 Agent 循环处理的事件。

## TextForEmbeddingReady

待写入嵌入存储的文本事件。

## LatencyMetricsUpdated

细粒度后端延迟指标事件。

## TurnTTSStartRequested

请求启动 TTS。

## TurnTTSPauseRequested

请求暂停 TTS。

## TurnTTSResumeRequested

请求恢复 TTS。

## TurnTTSStopRequested

请求停止 TTS。

## TurnTTSFlushRequested

请求刷出 TTS 缓冲。

## ConsumeLLMAgentGenerationRequested

请求消费 Agent 已生成的内容。

## TurnLLMAgentResumeRequested

请求恢复 LLM Agent 生成。

## TurnLLMAgentPauseRequested

请求暂停 LLM Agent 生成。

## TurnLLMAgentStopRequested

请求停止 LLM Agent 生成。

## TurnASRStartRequested

请求启动 ASR。

## TurnASREndRequested

请求结束 ASR。

## TurnASRPauseRequested

请求暂停 ASR。

## TurnTTSTextAppendRequested

请求向当前 TTS 缓冲追加文本。

## SpeakerRecognized

说话人识别结果事件。

## TTSModelSwitchRequested

请求切换 TTS 模型。

## LLMModelSwitchRequested

请求切换 LLM 模型。

## ClockSyncReceived

收到时钟同步信息。

## SessionConfigReceived

收到会话配置。

## TurnDetectorStopSpeaking

轮次检测器判定 AI 应停止说话。

## TurnDetectorStartGeneration

轮次检测器判定 AI 应开始生成。
