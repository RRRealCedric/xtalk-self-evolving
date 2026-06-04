# 配置服务

## 自定义模型

如前文在 [启动服务](start_the_service.zh.md) 中所述，X-Talk 实例可以通过 JSON 配置创建，该配置用于自定义所使用的模型。

查看支持的模型：
```python
from xtalk import Xtalk
print(Xtalk.MODEL_REGISTRY)
# 类似这样
# {
#     "asr": ["xtalk.speech.asr"],
#     "llm_agent": ["xtalk.llm_agent"],
#     "tts": ["xtalk.speech.tts"],
#     "embeddings": ["xtalk.embeddings"],
#  "speaker_encoder": ["xtalk.speech.speaker_encoder"],
#     "captioner": ["xtalk.speech.captioner"],
#     "caption_rewriter": ["xtalk.rewriter"],
#     "thought_rewriter": ["xtalk.rewriter"],
#     "vad": ["xtalk.speech.vad"],
#     "speech_enhancer": ["xtalk.speech.speech_enhancer"],
#     "speech_speed_controller": ["xtalk.speech.speech_speed_controller"],
#     "turn_detector": ["xtalk.speech.turn_detector"],
# }
```

对于模型配置，配置内容应与模型 Python 类名及其初始化参数一致。

例如，`DefaultAgent` 的定义位于 `src/xtalk/llm_agent/default.py`：
```python
class DefaultAgent(Agent):
    def __init__(
            self,
            model: BaseChatModel | dict,
            system_prompt: str = _BASE_PROMPT,
            voice_names: Optional[List[str]] = None,
            emotions: Optional[List[str]] = None,
            tools: Optional[List[Union[BaseTool, Callable[[], BaseTool]]]] = None,
        ):
    ...
```

为了与初始化参数匹配，配置项应写成这样：
```
"llm_agent": {
    "type": "DefaultAgent",
    "params": {
      "model": {
        "api_key": "none",
        "base_url": "http://127.0.0.1:8000/v1",
        "model": "cpatonn/Qwen3-30B-A3B-Instruct-2507-AWQ-4bit"
      },
      "voice_names": [
        "Man",
        "Woman",
        "Child"
      ],
      "emotions": [
        "happy",
        "angry",
        "sad",
        "fear",
        "disgust",
        "depressed",
        "surprised",
        "calm",
        "normal"
      ]
    }
  },
```

像 `voice_names`、`emotions` 和 `tools`（目前尚不支持在配置中使用）这样的可选键可以省略。

完整的模型类型、对应的可选依赖以及其在源码中的适配位置，请参阅[支持的模型](../docs/supported_models.zh.md)。
> **Note**
> 大多数模型实现都是客户端适配器。您可能还需要按照相应说明启动模型实例本身。


## 自定义服务行为

此外，您还可以通过以下配置自定义服务行为，例如是否将会话音频保存到 `logs/` 下、是否将会话音频发送到客户端：
```json
    "service_config": {
        "recording": true,
        "send_full_audio_to_client": true
    }
```

完整的服务配置项列表，请参阅[所有服务配置项](../docs/service_config.zh.md)。


## 前端配置

前端通过 `createSession(wsUrl, config)` 接收一个 `SessionConfig`。当前支持三类配置：

- `inputConfig`
- `outputConfig`
- `serviceURLs`

例如：

```ts
const session = createSession(wsUrl, {
    inputConfig: {
        sampleRate: 16000,
        enableVAD: true,
        enableEnhancer: true,
        vadRedemptionMs: 500,
    },
    outputConfig: {
        sampleRate: 48000,
    },
    serviceURLs: {
        login: "/api/auth/login",
        sessions: "/api/sessions",
        sessionDetail: (sessionId) => `/api/sessions/${sessionId}`,
        upload: "/api/upload",
    },
});
```

### inputConfig

`inputConfig` 用于配置前端输入音频会话。普通浏览器麦克风模式下，当前最常用的是这些字段：

- `sampleRate`
  输入音频采样率。默认值为 `16000`。
- `enableVAD`
  是否启用前端 VAD。默认值为 `true`。
- `enableEnhancer`
  是否启用前端语音增强。默认值为 `true`。
- `vadRedemptionMs`
  VAD 的 redemption 窗口，单位为毫秒。

此外，桥接模式 `mode: "web_bridge"` 还支持以下字段：

- `mode`
  可选值为 `"microphone"` 或 `"web_bridge"`。
- `participantId`
  桥接模式下的参与者标识符。
- `bridge`
  共享的音频桥实例。
- `autoEmitVad`
  是否在桥接模式下自动广播前端 VAD 事件。

### outputConfig

`outputConfig` 当前主要支持：

- `sampleRate`
  输出播放采样率。默认值为 `48000`。

### serviceURLs

`serviceURLs` 用于覆盖辅助 HTTP 接口地址，当前支持：

- `login`
- `sessions`
- `sessionDetail`
  可以是固定 URL，也可以是 `(sessionId) => URL` 形式的函数。
- `upload`

如果不显式传入，前端会根据 `wsUrl` 自动推导默认地址：

- `POST /api/auth/login`
- `GET /api/sessions`
- `GET /api/sessions/{session_id}`
- `POST /api/upload`