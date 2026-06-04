# Configure the service

## Customize models

As mentioned before in [Start the Service](start_the_service.md), an X-Talk instance can be created from a JSON config, which is used to customize the models in use.

Inspect the supported model categories:

```python
from xtalk import Xtalk
print(Xtalk.MODEL_REGISTRY)
# Something like
# {
#     "asr": ["xtalk.speech.asr"],
#     "llm_agent": ["xtalk.llm_agent"],
#     "tts": ["xtalk.speech.tts"],
#     "embeddings": ["xtalk.embeddings"],
#     "speaker_encoder": ["xtalk.speech.speaker_encoder"],
#     "captioner": ["xtalk.speech.captioner"],
#     "caption_rewriter": ["xtalk.rewriter"],
#     "thought_rewriter": ["xtalk.rewriter"],
#     "vad": ["xtalk.speech.vad"],
#     "speech_enhancer": ["xtalk.speech.speech_enhancer"],
#     "speech_speed_controller": ["xtalk.speech.speech_speed_controller"],
#     "turn_detector": ["xtalk.speech.turn_detector"],
# }
```

For model configuration, the config item should match the model Python class name and its initialization arguments.

For example, `DefaultAgent` is defined in `src/xtalk/llm_agent/default.py`:
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

To match those initialization arguments, the config item should look like:
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

Optional keys such as `voice_names`, `emotions`, and `tools` can be omitted. `tools` is not supported in config yet.

See [Supported Models](../docs/supported_models.md) for the full list of model types, their optional dependencies, and where they are adapted in the source code.
> **Note**
> Most model implementations are client-side adapters. You may also need to start the model instance itself according to its corresponding instructions.


## Customize service behavior

You can also customize service behavior, such as whether to save session audio under `logs/` and whether to send full session audio back to the client:
```json
    "service_config": {
        "recording": true,
        "send_full_audio_to_client": true
    }
```

See [all service configuration](../docs/service_config.md) for the full list of service configuration options.


## Frontend configuration

The frontend accepts a `SessionConfig` through `createSession(wsUrl, config)`. It currently supports three groups of options:

- `inputConfig`
- `outputConfig`
- `serviceURLs`

For example:

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

`inputConfig` controls the frontend input audio session. In normal browser microphone mode, the most commonly used fields are:

- `sampleRate`
  Input audio sample rate. The default is `16000`.
- `enableVAD`
  Whether to enable frontend VAD. The default is `true`.
- `enableEnhancer`
  Whether to enable frontend speech enhancement. The default is `true`.
- `vadRedemptionMs`
  VAD redemption window in milliseconds.

Bridge mode with `mode: "web_bridge"` also supports:

- `mode`
  Either `"microphone"` or `"web_bridge"`.
- `participantId`
  Participant identifier for bridge-backed input.
- `bridge`
  Shared audio bridge instance.
- `autoEmitVad`
  Whether to auto-broadcast frontend VAD events in bridge mode.

### outputConfig

`outputConfig` currently mainly supports:

- `sampleRate`
  Output playback sample rate. The default is `48000`.

### serviceURLs

`serviceURLs` overrides auxiliary HTTP endpoints. It currently supports:

- `login`
- `sessions`
- `sessionDetail`
  This can be either a fixed URL or a function of the form `(sessionId) => URL`.
- `upload`

If omitted, the frontend derives default URLs automatically from `wsUrl`:

- `POST /api/auth/login`
- `GET /api/sessions`
- `GET /api/sessions/{session_id}`
- `POST /api/upload`
