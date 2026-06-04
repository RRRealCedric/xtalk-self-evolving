# xtalk

## Xtalk

_定义于 `xtalk.api`。_

```python
class Xtalk
```

创建 Xtalk 管道、服务以及会话入口。

### Notes

`Xtalk` 是示例应用使用的主要集成入口。它会根据配置构建管道，保存服务原型，并按需接收 WebSocket 会话。

### Class Fields

- `MODEL_REGISTRY: dict[str, list[ImportSpec]]` = `SHARED_MODEL_REGISTRY`

### Methods

#### __init__

```python
def __init__(self, *, service_prototype: Service, max_sessions: int | None = None)
```

初始化 `Xtalk` 应用封装。

#### register_model_search_spec

```python
def register_model_search_spec(cls, *, slot: str, spec: ImportSpec, prepend: bool = True) -> None
```

为某个槽位注册额外的模型查找位置。

#### from_config

```python
def from_config(cls, path_or_dict: str | dict) -> 'Xtalk'
```

根据配置数据构建 `Xtalk` 实例。

#### create_pipeline_from_config

```python
def create_pipeline_from_config(cls, *, pipeline_cls: Type[Pipeline], config_path_or_dict: str | dict, additional_model_registry: dict[str, Any]) -> Pipeline
```

根据配置实例化自定义管道类。

#### set_session_limit

```python
def set_session_limit(self, limit: int)
```

设置或替换并发会话限制。

#### embed_text

```python
async def embed_text(self, session_id: str, text: str, user_id: str | None = None)
```

将文本加入会话级嵌入存储队列。

#### add_agent_tools

```python
def add_agent_tools(self, tools_or_factories: list[BaseTool | Callable[[], BaseTool]])
```

在会话创建之前，将工具附加到原型 Agent 上。

#### mount_routes

```python
def mount_routes(self, app: Any, *, login_path: str = '/api/auth/login', sessions_path: str = '/api/sessions', session_detail_path: str = '/api/sessions/{session_id}', upload_path: str = '/api/upload', ws_path: str = '/ws') -> None
```

挂载内置的登录、会话、上传和 WebSocket 路由。

#### connect

```python
async def connect(self, websocket: WebSocket, user_id: str | None = None)
```

接收一个 WebSocket 会话并将其交给服务管理器。

## Pipeline

_定义于 `xtalk.pipelines.interfaces`。_

```python
class Pipeline(ABC)
```

定义 Xtalk 服务所需的模型访问器。

### Notes

具体管道会暴露管理器所消费的模型和辅助对象。示例应用通常会继承 `DefaultPipeline` 来添加额外组件，同时保留这一接口。

### Methods

#### clone

```python
def clone(self) -> 'Pipeline'
```

为新会话克隆管道。

## DefaultPipeline

_定义于 `xtalk.pipelines.default`。_

```python
@dataclass(init=False)
class DefaultPipeline(Pipeline)
```

保存会话使用的标准 Xtalk 模型集合。

### Notes

当前标准字段包括 ASR、Agent、TTS、Captioner、PuntRestorer、Caption Rewriter、VAD、Speech Enhancer、Speaker Encoder、Speech Speed Controller、Embeddings 和 Turn Detector；其中 `vad_model` 现在按 `clone=True` 进行会话级克隆。

## Service

_定义于 `xtalk.serving.service`。_

```python
class Service
```

编排会话作用域内的管道和管理器栈。

## DefaultService

_定义于 `xtalk.serving.service`。_

```python
class DefaultService(Service)
```

带有标准 Xtalk 管理器栈的便捷 `Service`。

### Class Fields

- `MANAGER_CLASSES`
  默认包含 `ASRManager`、`LLMAgentContextManager`、`LLMAgentConsumptionManager`、`DirectAudioManager`、`TTSManager`、`TTSPlaybackManager`、`CaptionerManager`、`RetrievalManager`、`TurnTakingManager`、`LatencyManager`、`VADManager`、`EnhancerManager`、`SpeakerManager`、`EmbeddingsManager`、`RecordingManager` 和 `TurnDetectorManager`。

## BaseEvent

_定义于 `xtalk.serving.events`。_

```python
@dataclass
class BaseEvent
```

所有 Xtalk 事件的基础 dataclass。

## create_event_class

_定义于 `xtalk.serving.events`。_

```python
def create_event_class(*, name: str, fields: dict[str, Any] | None = None, type_name: str | None = None) -> Type[BaseEvent]
```

动态创建 `BaseEvent` 子类。

## Manager

_定义于 `xtalk.serving.interfaces`。_

```python
class Manager(EventListenerMixin, ShutdownMixin)
```

Xtalk 管理器的基类。

## EventBus

_定义于 `xtalk.serving.event_bus`。_

```python
class EventBus
```

发布和订阅会话事件，并支持异步分发。
