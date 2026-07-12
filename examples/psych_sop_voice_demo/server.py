"""X-Talk voice demo server for the rule-based Psychology SOP runtime."""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
from pathlib import Path

os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from fastapi.templating import Jinja2Templates  # noqa: E402

from xtalk import DefaultPipeline, DefaultService, Xtalk  # noqa: E402
from xtalk.serving.events import ASRResultFinal, LLMAgentLoop  # noqa: E402
from xtalk.serving.module_types import (  # noqa: E402
    LLMAgentContextManager,
    PsychSOPManager,
    SCIDDualLMManager,
)


mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("application/javascript", ".mjs")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="X-Talk PsychSOP Voice Demo Server")
    parser.add_argument("--config", required=True, type=str, help="X-Talk config path")
    parser.add_argument("--port", type=int, help="Port number")
    parser.add_argument("--scale", default=None, help="GAD-7 or PHQ-9")
    parser.add_argument(
        "--mode",
        choices=("psych", "scid", "chat"),
        default="psych",
        help="Run the legacy scale demo, the SCID dual-LM runtime, or plain chat.",
    )
    parser.add_argument(
        "--backend-model",
        default="deepseek-v4-pro",
        help="Backend diagnostic model used in --mode scid.",
    )
    parser.add_argument("--reset-memory", action="store_true")
    return parser.parse_args()


args = parse_args()
config = Xtalk._get_config_dict(args.config)
pipeline = Xtalk.create_pipeline_from_config(
    pipeline_cls=DefaultPipeline,
    config_path_or_dict=config,
    additional_model_registry={},
)
service_config = dict(Xtalk._load_service_config(config))
if args.scale:
    service_config["psych_sop_scale"] = args.scale
if args.reset_memory:
    service_config["psych_sop_reset_memory"] = True
service_config.setdefault("psych_sop_experiment_id", "psych_sop_voice_demo")
service_config.setdefault("scid_experiment_id", "scid_voice_demo")
service_config["scid_backend_model"] = args.backend_model

service = DefaultService(pipeline=pipeline, service_config=service_config)
if args.mode == "scid":
    service.register_manager(SCIDDualLMManager)
elif args.mode == "psych":
    service.register_manager(PsychSOPManager)
if args.mode in {"psych", "scid"}:
    service.unsubscribe_event(
        event_listener_cls=LLMAgentContextManager,
        event_type=ASRResultFinal,
    )
    service.unsubscribe_event(
        event_listener_cls=LLMAgentContextManager,
        event_type=LLMAgentLoop,
    )
xtalk_instance = Xtalk(
    service_prototype=service,
    max_sessions=Xtalk._max_sessions(config),
)

app = FastAPI(title="X-Talk PsychSOP Voice Demo")
xtalk_instance.mount_routes(app)

example_server_path = Path(__file__).resolve().parents[1] / "sample_app"
templates = Jinja2Templates(directory=str(example_server_path / "templates"))
app.mount(
    "/static", StaticFiles(directory=str(example_server_path / "static")), name="static"
)
try:
    app.mount(
        "/xtalk",
        StaticFiles(
            directory=str(Path(__file__).resolve().parents[2] / "frontend" / "dist")
        ),
        name="xtalk",
    )
except Exception:
    print("No local X-Talk frontend library found.")


@app.get("/api/voices")
async def get_reference_audios():
    with open(args.config, "r", encoding="utf-8") as f:
        loaded_config = json.load(f)
        try:
            voices = loaded_config["tts"]["params"]["voices"]
        except KeyError:
            voices = []
    return JSONResponse(content={"audios": voices})


@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=args.port or 11995)
