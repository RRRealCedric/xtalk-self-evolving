import json
import uuid
from pathlib import Path
from typing import Callable, Type, Any
from fastapi import (
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    WebSocket,
    status,
)
from langchain_core.tools import BaseTool

from .auth import JWTAuth, extract_bearer_token, resolve_auth_config
from .memory.schema import MemoryItem
from .memory.store import SQLiteMemoryStore
from .persistence import PersistenceStore
from .serving.service_manager import ServiceManager
from .pipelines import Pipeline
from .pipelines.default import DefaultPipeline
from .serving.session_limiter import SessionLimiter
from .serving.events import TextForEmbeddingReady
from .serving.service import Service, DefaultService
from .model_loader import (
    ImportSpec,
    MODEL_REGISTRY as SHARED_MODEL_REGISTRY,
    init_model,
    register_model_search_spec as register_model_search_spec_helper,
)


class Xtalk:
    """Create Xtalk pipelines, services, and session entrypoints.

    Notes
    -----
    ``Xtalk`` is the main integration surface used by the sample applications.
    It builds pipelines from configuration, stores a prototype service, and
    accepts WebSocket sessions on demand.
    """

    MODEL_REGISTRY: dict[str, list[ImportSpec]] = SHARED_MODEL_REGISTRY

    def __init__(self, *, service_prototype: Service, max_sessions: int | None = None):
        """Initialize an ``Xtalk`` application wrapper.

        Parameters
        ----------
        service_prototype : Service
            Prototype service used to clone per-session service instances.
        max_sessions : int | None, optional
            Maximum number of concurrent sessions. If omitted, no session limit
            is enforced.
        """
        service_config = dict(service_prototype.service_config)
        self._persistence_enabled = self._is_persistence_enabled(service_config)
        data_dir = service_config.get("data_dir") or "data"
        auth_secret, auth_ttl_seconds = resolve_auth_config(service_config)

        self._persistence = (
            PersistenceStore(
                Path(data_dir).expanduser().resolve() / "chat_history.sqlite3"
            )
            if self._persistence_enabled
            else None
        )
        self._memory_enabled = self._is_memory_enabled(service_config)
        self._memory_store = (
            SQLiteMemoryStore(self._resolve_memory_db_path(service_config, data_dir))
            if self._memory_enabled
            else None
        )
        self._auth = JWTAuth(secret=auth_secret, ttl_seconds=auth_ttl_seconds)
        self._service_manager = ServiceManager(
            service_prototype=service_prototype,
            persistence_store=self._persistence,
            memory_store=self._memory_store,
        )
        self._pipeline = service_prototype.pipeline
        self._session_limiter = (
            SessionLimiter(max_sessions) if max_sessions is not None else None
        )

    # -------------------------
    # Public registry APIs
    # -------------------------
    @classmethod
    def register_model_search_spec(
        cls,
        *,
        slot: str,
        spec: ImportSpec,
        prepend: bool = True,
    ) -> None:
        """Register an additional model lookup location for a slot.

        Parameters
        ----------
        slot : str
            Registry slot name such as ``"llm_agent"`` or ``"tts"``.
        spec : ImportSpec
            Import target to try when loading that slot. Supported forms include
            module paths, ``"module:attribute"`` references, and Python file
            paths.
        prepend : bool, default=True
            Whether to try the new spec before existing registered specs.

        Notes
        -----
        Common ``spec`` forms include ``"my_pkg.custom_tts"``,
        ``"my_pkg.custom_tts:registry"``, ``"/abs/path/custom_tts.py"``, and
        ``Path("./custom_tts.py")``.

        Examples
        --------
        >>> Xtalk.register_model_search_spec(
        ...     slot="llm_agent",
        ...     spec="./echo_agent.py",
        ... )
        """
        register_model_search_spec_helper(slot=slot, spec=spec, prepend=prepend)

    # -------------------------
    # Config / construction
    # -------------------------
    @staticmethod
    def _get_config_dict(path_or_dict: str | dict) -> dict:
        if isinstance(path_or_dict, str):
            with open(path_or_dict, "r") as f:
                config = json.load(f)
        else:
            config = path_or_dict
        return config

    @classmethod
    def from_config(cls, path_or_dict: str | dict) -> "Xtalk":
        """Build an ``Xtalk`` instance from configuration data.

        Parameters
        ----------
        path_or_dict : str | dict
            JSON file path or already loaded configuration dictionary.

        Returns
        -------
        Xtalk
            Configured application wrapper backed by a ``DefaultPipeline`` and
            ``DefaultService``.

        Examples
        --------
        >>> xtalk = Xtalk.from_config("server_config.json")
        """
        config = cls._get_config_dict(path_or_dict)
        pipeline = cls._load_pipeline(DefaultPipeline, config)
        service_prototype = DefaultService(
            pipeline=pipeline, service_config=cls._load_service_config(config)
        )
        max_sessions = cls._max_sessions(config)
        return cls(service_prototype=service_prototype, max_sessions=max_sessions)

    @classmethod
    def create_pipeline_from_config(
        cls,
        *,
        pipeline_cls: Type[Pipeline],
        config_path_or_dict: str | dict,
        additional_model_registry: dict[str, Any],
    ) -> Pipeline:
        """Instantiate a custom pipeline class from configuration.

        Parameters
        ----------
        pipeline_cls : Type[Pipeline]
            Concrete pipeline type to instantiate.
        config_path_or_dict : str | dict
            JSON file path or already loaded configuration dictionary.
        additional_model_registry : dict[str, Any]
            Extra slot-to-instance mapping merged on top of the default model
            registry before the pipeline is created.

        Returns
        -------
        Pipeline
            Pipeline instance created from the supplied configuration.

        Examples
        --------
        >>> pipeline = Xtalk.create_pipeline_from_config(
        ...     pipeline_cls=DefaultPipeline,
        ...     config_path_or_dict="server_config.json",
        ...     additional_model_registry={},
        ... )
        """
        config = cls._get_config_dict(config_path_or_dict)
        pipeline = cls._load_pipeline(pipeline_cls, config, additional_model_registry)
        return pipeline

    def set_session_limit(self, limit: int):
        """Set or replace the concurrent session limit.

        Parameters
        ----------
        limit : int
            Maximum number of active sessions allowed at the same time.
        """
        self._session_limiter = SessionLimiter(limit)

    async def embed_text(self, session_id: str, text: str, user_id: str | None = None):
        """Queue text for session-scoped embedding storage.

        Parameters
        ----------
        session_id : str
            Session identifier returned to the frontend.
        text : str
            Text content that should be embedded and persisted for retrieval.

        Raises
        ------
        ValueError
            Raised if the target session does not exist.
        """
        if (
            self._persistence is not None
            and user_id is not None
            and not self._persistence.user_owns_session(user_id, session_id)
        ):
            raise ValueError(f"Session {session_id} not found for user {user_id}.")
        service = self._service_manager.get_service(session_id)
        if service is None:
            raise ValueError(f"Session {session_id} not found.")
        await service.event_bus.publish(
            TextForEmbeddingReady(session_id=session_id, text=text)
        )

    def add_agent_tools(
        self, tools_or_factories: list[BaseTool | Callable[[], BaseTool]]
    ):
        """Attach tools to the prototype agent before sessions are created.

        Parameters
        ----------
        tools_or_factories : list[BaseTool | Callable[[], BaseTool]]
            Tool instances or zero-argument factories that produce tool
            instances.

        Raises
        ------
        RuntimeError
            Raised if at least one service session has already been created.
        """
        if self._service_manager.get_service_count() > 0:
            raise RuntimeError("Cannot add tools after services have been created.")
        self._pipeline.get_agent().add_tools(tools_or_factories)

    def _login(self) -> dict[str, Any]:
        user_id = str(uuid.uuid4())
        user = (
            self._persistence.ensure_user(user_id)
            if self._persistence is not None
            else {"id": user_id}
        )
        return {
            "access_token": self._auth.issue_token(sub=user_id),
            "user": user,
        }

    def _verify_access_token(self, token: str) -> str:
        user_id = self._auth.verify_token(token)
        if self._persistence is not None:
            self._persistence.ensure_user(user_id)
        return user_id

    def _list_sessions(self, user_id: str) -> list[dict[str, Any]]:
        if self._persistence is None:
            return []
        self._persistence.ensure_user(user_id)
        return self._persistence.list_sessions(user_id)

    def _get_session_detail(self, user_id: str, session_id: str) -> dict[str, Any]:
        if self._persistence is None:
            raise KeyError(session_id)
        self._persistence.ensure_user(user_id)
        return self._persistence.get_session_detail(user_id, session_id)

    def mount_routes(
        self,
        app: Any,
        *,
        login_path: str = "/api/auth/login",
        sessions_path: str = "/api/sessions",
        session_detail_path: str = "/api/sessions/{session_id}",
        upload_path: str = "/api/upload",
        memories_path: str = "/api/memories",
        memory_search_path: str = "/api/memories/search",
        memory_detail_path: str = "/api/memories/{memory_id}",
        ws_path: str = "/ws",
    ) -> None:
        """Mount the built-in auth, session, memory, upload, and websocket routes."""

        def _require_http_user(request: Request) -> str:
            token = extract_bearer_token(request.headers.get("authorization"))
            if not token:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Missing bearer token",
                )
            try:
                return self._verify_access_token(token)
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail=str(exc),
                ) from exc

        def _require_ws_user(websocket: WebSocket) -> str:
            token = websocket.query_params.get("access_token")
            if token is None:
                token = extract_bearer_token(websocket.headers.get("authorization"))
            if not token:
                raise ValueError("Missing access token")
            return self._verify_access_token(token)

        @app.post(login_path)
        async def _login_route() -> dict[str, Any]:
            return self._login()

        @app.get(sessions_path)
        async def _list_sessions_route(request: Request) -> dict[str, Any]:
            user_id = _require_http_user(request)
            return {"sessions": self._list_sessions(user_id)}

        @app.get(session_detail_path)
        async def _session_detail_route(
            request: Request, session_id: str
        ) -> dict[str, Any]:
            user_id = _require_http_user(request)
            try:
                return self._get_session_detail(user_id, session_id)
            except KeyError as exc:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="Session not found"
                ) from exc

        @app.post(upload_path)
        async def _upload_route(
            request: Request,
            session_id: str = Form(...),
            file: UploadFile = File(...),
        ) -> dict[str, str]:
            user_id = _require_http_user(request)
            content_type = (file.content_type or "").lower()
            is_text = content_type.startswith("text/") if content_type else False
            if content_type and not is_text:
                raise HTTPException(
                    status_code=400, detail="Only text files are supported."
                )
            text = (await file.read()).decode("utf-8", errors="ignore")
            try:
                await self.embed_text(session_id=session_id, text=text, user_id=user_id)
            except ValueError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            return {"status": "ok"}

        @app.get(memories_path)
        async def _list_memories_route(
            request: Request,
            limit: int = 50,
            offset: int = 0,
        ) -> dict[str, Any]:
            user_id = _require_http_user(request)
            if self._memory_store is None:
                return {"memories": []}
            memories = await self._memory_store.list(
                user_id=user_id,
                limit=limit,
                offset=offset,
            )
            return {"memories": [self._memory_to_dict(item) for item in memories]}

        @app.post(memories_path)
        async def _create_memory_route(request: Request) -> dict[str, Any]:
            user_id = _require_http_user(request)
            if self._memory_store is None:
                raise HTTPException(status_code=404, detail="Memory is disabled")
            data = await request.json()
            content = str(data.get("content") or "").strip()
            if not content:
                raise HTTPException(status_code=400, detail="Missing memory content")
            memory_type = str(data.get("type") or "semantic")
            session_id = data.get("session_id")
            memory_id = await self._memory_store.add(
                MemoryItem(
                    id=str(uuid.uuid4()),
                    user_id=user_id,
                    session_id=str(session_id) if session_id else None,
                    type=memory_type,
                    content=content,
                    source=str(data.get("source") or "debug_api"),
                    confidence=float(data.get("confidence") or 1.0),
                    metadata=(
                        data.get("metadata")
                        if isinstance(data.get("metadata"), dict)
                        else {}
                    ),
                )
            )
            return {"memory_id": memory_id}

        @app.get(memory_search_path)
        async def _search_memories_route(
            request: Request,
            q: str,
            limit: int = 5,
        ) -> dict[str, Any]:
            user_id = _require_http_user(request)
            if self._memory_store is None:
                return {"memories": []}
            results = await self._memory_store.search(
                user_id=user_id,
                query=q,
                limit=limit,
            )
            return {
                "memories": [
                    {
                        **self._memory_to_dict(result.item),
                        "score": result.score,
                    }
                    for result in results
                ]
            }

        @app.delete(memory_detail_path)
        async def _delete_memory_route(
            request: Request,
            memory_id: str,
        ) -> dict[str, Any]:
            user_id = _require_http_user(request)
            if self._memory_store is None:
                raise HTTPException(status_code=404, detail="Memory is disabled")
            deleted = await self._memory_store.delete(
                user_id=user_id,
                memory_id=memory_id,
            )
            if not deleted:
                raise HTTPException(status_code=404, detail="Memory not found")
            return {"status": "ok"}

        @app.websocket(ws_path)
        async def _websocket_route(websocket: WebSocket) -> None:
            try:
                user_id = _require_ws_user(websocket)
            except ValueError:
                await websocket.accept()
                await websocket.close(code=1008, reason="Unauthorized")
                return
            await self.connect(websocket, user_id=user_id)

    async def connect(self, websocket: WebSocket, user_id: str | None = None):
        """Accept a WebSocket session and hand it to the service manager.

        Parameters
        ----------
        websocket : WebSocket
            FastAPI WebSocket connection from the client.
        user_id : str | None, optional
            Authenticated user identifier. When omitted, the connection falls
            back to the legacy connection-scoped session behavior.

        Notes
        -----
        If a session limit is configured, the socket is first admitted through
        the session limiter queue.
        """
        if self._session_limiter:
            await websocket.accept()
            waiter = await self._session_limiter.acquire(websocket)
            if waiter is None:
                try:
                    await websocket.close(code=1000)
                except Exception:
                    pass
                return
            await self._service_manager.connect(
                websocket=websocket, already_accepted=True, user_id=user_id
            )
            await self._session_limiter.release(waiter)
            return

        await self._service_manager.connect(websocket=websocket, user_id=user_id)

    @staticmethod
    def _max_sessions(config: dict):
        if "max_connections" in config:
            try:
                return int(config["max_connections"])
            except Exception:
                pass
        return None

    @staticmethod
    def _load_service_config(config: dict):
        return config.get("service_config", {})

    @staticmethod
    def _is_persistence_enabled(service_config: dict[str, Any]) -> bool:
        """Return whether session persistence is enabled for the service."""
        value = service_config.get("enable_persistence")
        if value is None:
            return True
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"0", "false", "no", "off"}:
                return False
            if normalized in {"1", "true", "yes", "on"}:
                return True
        return bool(value)

    @staticmethod
    def _is_memory_enabled(service_config: dict[str, Any]) -> bool:
        """Return whether long-term memory storage is enabled."""
        memory_config = service_config.get("memory")
        if not isinstance(memory_config, dict):
            return True
        value = memory_config.get("enabled", True)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"0", "false", "no", "off"}:
                return False
            if normalized in {"1", "true", "yes", "on"}:
                return True
        return bool(value)

    @staticmethod
    def _resolve_memory_db_path(
        service_config: dict[str, Any],
        data_dir: str,
    ) -> Path:
        memory_config = service_config.get("memory")
        if isinstance(memory_config, dict):
            sqlite_path = memory_config.get("sqlite_path")
            if sqlite_path:
                return Path(str(sqlite_path)).expanduser().resolve()
            memory_data_dir = memory_config.get("data_dir")
            if memory_data_dir:
                return (
                    Path(str(memory_data_dir)).expanduser().resolve() / "memory.sqlite3"
                )
        return Path(data_dir).expanduser().resolve() / "memory" / "memory.sqlite3"

    @staticmethod
    def _memory_to_dict(item: MemoryItem) -> dict[str, Any]:
        return {
            "id": item.id,
            "session_id": item.session_id,
            "type": item.type,
            "content": item.content,
            "source": item.source,
            "confidence": item.confidence,
            "metadata": item.metadata,
            "created_at": item.created_at,
            "updated_at": item.updated_at,
        }

    @classmethod
    def _load_pipeline(
        cls,
        pipeline_cls: Type[Pipeline],
        config: dict,
        additional_model_registry: dict | None = None,
    ):
        model_map: dict[str, Any] = {}
        for slot, specs in cls.MODEL_REGISTRY.items():
            model_map[slot] = init_model(
                model_config=config.get(slot, {}),
                import_specs=specs,
            )

        if additional_model_registry:
            model_map = model_map | additional_model_registry

        return pipeline_cls(**model_map)
