# Recipe

以下几个例子说明如何通过直接修改该框架开发：

## 引入新的ASR模型

假设要引入Qwen3ASRFlashRealtime，目前实现已经在`src/xtalk/speech/asr/qwen3_asr_flash_realtime.py`。

1. 在`src/xtalk/speech/asr`下创建`qwen3_asr_flash_realtime.py`
2. 准备骨架，并实现对应方法（各类模型接口参考`src/xtalk/speech/interfaces.py`）
```python
from ..interfaces import ASR
class Qwen3ASRFlashRealtime(ASR):
    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        config: Optional[Qwen3ASRFlashConfig] = None,
    ) -> None:
        ...
    def recognize(self, audio: bytes) -> str:
        ...
    def recognize_stream(
        self,
        audio: bytes,
        *,
        is_final: bool = False,
        chat_history: str | None = None,
    ) -> str:
        ...
    def stream_chunk_bytes_hint(self) -> int | None:
        ...
    def reset(self) -> None:
        ...
    def clone(self) -> "ASR":
        ...
    async def async_recognize(self, audio: bytes) -> str:
        ...
    async def async_recognize_stream(
        self,
        audio: bytes,
        *,
        is_final: bool = False,
        chat_history: str | None = None,
    ) -> str:
        ...
```
3. 在`src/xtalk/speech/asr/__init__.py`注册新实现的模型
4. 配置中使用
```json
"asr": {
        "type": "Qwen3ASRFlashRealtime",
        "params": {
            "api_key": "your key"
        }
    }
```

## 引入新的Agent

参考`src/xtalk/llm_agent/experimental.py`；实现与配置方法类似`引入新的ASR模型`章节，注意在`src/xtalk/llm_agent/__init__.py`中注册模型。

### accept的逻辑

```python
async def async_accept(self, context: AgentContext) -> AsyncIterator[AgentOutput]:
    pass
```

`accept`方法订阅外部输入并启动相关处理逻辑；`AgentContext`来自`src/xtalk/serving/modules/llm_agent_context_manager.py`，目前较为稳定的类型有`asr_partial`、`asr_final`、`loop`。其中`loop`在连接建立时触发一次，可以用于处理任何主动触发逻辑，或者是启动输出循环。`src/xtalk/llm_agent/experimental.py`用于触发主动对话。

`AgentOutput`为字符串、工具调用或工具调用结果；工具调用返回后可用于`Manager`触发相关逻辑，例如`src/xtalk/serving/modules/llm_agent_context_manager.py`中`direct_audio`的工具调用触发下游`src/xtalk/serving/modules/direct_audio_manager.py`生成直接播放音频的事件。

`Agent`从设计哲学上期望成为整个系统的思考核心，负责整合其他块的信息输出。

## 引入新的Manager

上个章节的`Agent`要求引入一个新的`src/xtalk/serving/modules/direct_audio_manager.py`转发工具调用的输出到音频事件。所有`Manager`直接在`src/xtalk/serving/modules`下创建即可，之后要在`src/xtalk/serving/service.py`与`src/xtalk/serving/module_types.py`中注册。`Manager`采用观察者模式进行事件订阅与发布。所有事件在`src/xtalk/serving/events.py`。`src/xtalk/serving/modules/input_gateway.py`和`src/xtalk/serving/modules/output_gateway.py`较为特殊，负责接收前端的输入和向前端输出。

在`Manager`中调用模型可参考`src/xtalk/serving/modules/asr_manager.py`，使用`pipeline.get_asr_model`等方法即可。

注意事件发布`publish`方法中`wait_for_completion`代表`await`时是否等待该事件直接触发的监听函数处理完成。沿事件链条启用`wait_for_completion`可以保证该链条上所有事件处理完毕后才回到事件发送源头继续执行。

## 引入新的类型的模型

在`src/xtalk/speech/interfaces.py`中创建接口，在`src/xtalk/speech`下创建对应文件夹与模型文件即可，流程与`引入新的ASR模型`相近，之后要在`src/xtalk/model_loader.py`与`src/xtalk/model_types.py`中注册。
