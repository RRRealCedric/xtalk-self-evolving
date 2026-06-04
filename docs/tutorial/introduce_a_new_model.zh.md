*实验中的API*


> **Note**
> 详情请参阅 `examples/sample_app/custom_model.py` 和 `examples/sample_app/echo_agent.py`。

> **Note**
> 若要添加已有类型的新模型，请参阅 [Recipe](#recipe)。

您可能希望为已有类型引入一个新模型（例如文本转语音），或者添加一种全新的模型类型（例如处理 backchannel 的模型）。这可以通过在从配置创建 `xtalk_instance` 之前调用 `register_model_search_spec` 来实现：

```python
from xtalk import Xtalk
Xtalk.register_model_search_spec(
    slot="llm_agent",
    spec=Path(__file__).parent / "echo_agent.py",
)
xtalk_instance = Xtalk.from_config(args.config)
```

这里的 `slot` 对应 `Pipeline` 中相应初始化参数的名称。您可以查看 `Xtalk.MODEL_REGISTRY` 了解已有的 slot，也可以使用一个新的 slot 表示新的模型类型（参见 `examples\sample_app\custom_service.py`，其中的 `llm_output_refactor_model` 就可以是新 slot）。

`spec` 是模型实现文件的路径，`echo_agent.py` 中的一个示例实现如下：

```python
from xtalk.model_types import Agent

class EchoAgent(Agent):
    """一个简单地回显用户输入的 agent。"""

    def generate(self, input) -> str:
        if isinstance(input, dict):
            return input["content"]
        return input

    def clone(self) -> "EchoAgent":
        return EchoAgent()

```

之后，您就可以在配置文件中使用这个自定义模型：
```python
{
    "asr": {
        "type": "Qwen3ASRFlashRealtime",
        "params": {
            "api_key": "<API_KEY>"
        }
    },
    "llm_agent": "EchoAgent",
    "tts": {
        "type": "CosyVoice",
        "params": {
            "api_key": "<API_KEY>"
        }
    }
}
```

#### Recipe

下面列出了主要模型定制场景的配方。您也可以阅读其他模型类型对应接口的源码。我们会不定期更新这些接口。

> **Note**
> 所有可用模型类型请参阅 `src/xtalk/model_types.py`。

> [!IMPORTANT]
> X-Talk 为同步版本提供了异步默认实现，通常会使用 `run_in_executor`，例如相对于 ASR 的 `recognize`，会提供 `async_recognize`。但为了在生产环境获得最佳并发性能，我们建议您自行实现这些异步版本。

##### 新的 ASR（自动语音识别）模型

您的 ASR 类必须继承 `xtalk.speech.interfaces.ASR`，并实现以下方法：

* **`recognize(audio: bytes) -> str`**
    * 单次完成音频识别。
* **`reset() -> None`**
    * 重置内部识别状态。
* **`clone() -> ASR`**
    * 返回一个新的实例，用于新的会话或并发会话。
    * 可以共享权重或连接（例如 `_shared_model`），但不能共享状态。

以下方法是可选的：
* **`recognize_stream(audio: bytes, *, is_final: bool = False) -> str`**
    * 用于流式增量识别的接口。
    * 返回“截至当前时刻的累计识别结果”。
* **`async_recognize(audio: bytes)`**
* **`async def async_recognize_stream(
        self, audio: bytes, *, is_final: bool = False
    )`**


> [!IMPORTANT]
> `recognize` 和 `recognize_stream` 的输入是 PCM 16-bit、单声道、16 kHz 的原始字节流。您可能需要自行完成格式转换。

> **Note**
> X-Talk 已为 `recognize_stream` 提供了基于 `MockStreamRecognizer` 的默认实现。因此，即使您的 ASR 模型不支持流式识别也无需担心。

> **Note**
> 构建自己的 ASR 类时，您可以参考现有实现（例如 `src/xtalk/speech/asr/zipformer_local.py`）。我们建议将 ASR 部署为独立服务，并在 ASR 类中通过 API 调用它，可参考 `src/xtalk/speech/asr/sherpa_onnx_asr.py` 的实现方式。

##### 新的 TTS（文本转语音）模型

您的新 TTS 类必须继承 `xtalk.speech.interfaces.TTS`，并实现以下方法：


- **`synthesize(self, text: str) -> bytes`**

  - 输入：要合成的文本。
  - 输出：PCM 16-bit、单声道、48000 Hz 的原始音频字节。

- **`clone(self) -> TTS`**

  - 返回一个新的 TTS 实例：
    - 它应当拥有隔离的运行时状态，避免跨会话相互影响；如果后端支持，也可以共享只读资源。

> **Note**
> 新接入的 TTS 实现请遵循以下约定：
> - 非流式 TTS：实现 `synthesize`；如需提升异步效率，可选重写 `async_synthesize`。
> - 流式 TTS：仍然必须实现 `synthesize`，并额外重写 `synthesize_stream`；如需提升异步效率，也可以重写 `async_synthesize` 与 `async_synthesize_stream`。
> - 非流式后端不要为了适配接口而重写 `synthesize_stream`。基类默认已经会把 `synthesize` 包装成单个 chunk 作为兼容行为，这种继承得到的包装不应被视为原生流式能力。

**可选方法**

- **`synthesize_stream(self, text: str, **kwargs) -> Iterable[bytes]`**
  - 仅当您的后端支持真正的流式合成时，才应重写此方法。
- **`set_voice(self, voice_names: list[str])`**

  - 此方法配合 `TTSManager` 中的 `TTSVoiceChange` 事件使用，用于通过语言模型工具调用切换音色。
  - 通常 `voice_names` 只有一个元素，这也是当前工具调用结果的行为。不过，一些 TTS 模型可能支持混合多个参考音色，因此这里使用 `list` 类型。

- **`set_emotion(self, emotion: str | list[float])`**

  - 此方法配合 `TTSManager` 中的 `TTSEmotionChange` 事件使用，用于通过语言模型工具调用切换情绪。
  - 当前工具调用结果中的 `emotion` 仅为 `str`。不过，未来您也可能希望支持以 `list[float]` 形式传入情绪向量。
 
- **`async def async_synthesize(self, text: str, **kwargs: Any)`**
  - 适用于流式与非流式后端的可选异步优化。
- **`async def async_synthesize_stream(
        self, text: str, **kwargs: Any
    )`**
  - 面向流式后端的可选异步优化；如果不重写，基类会异步迭代 `synthesize_stream`。
