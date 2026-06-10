"""Episode logging for the psychology SOP demo."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4


DEFAULT_EPISODE_DIR = Path("data/psych_sop_demo/episodes")


class EpisodeLogger:
    """Collect and save one demo episode."""

    def __init__(
        self,
        *,
        user_id: str,
        task: str,
        scale_id: str,
        sop_version: str,
        prompt_version: str,
        experiment_id: str = "psych_sop_demo",
        episode_dir: str | Path = DEFAULT_EPISODE_DIR,
    ) -> None:
        now = datetime.utcnow().isoformat()
        self.episode_dir = Path(episode_dir)
        self.episode: dict[str, Any] = {
            "episode_id": str(uuid4()),
            "user_id": user_id,
            "task": task,
            "scale_id": scale_id,
            "sop_version": sop_version,
            "prompt_version": prompt_version,
            "experiment_id": experiment_id,
            "started_at": now,
            "ended_at": None,
            "status": "in_progress",
            "turns": [],
            "answers": {},
            "score": None,
            "interpretation": None,
            "safety_events": [],
            "skipped_questions": [],
            "clarification_count": 0,
            "dropout": False,
            "failure_type": None,
            "notes": [],
        }

    def add_turn(
        self,
        *,
        node_id: str,
        action: str,
        assistant_text: str,
        user_text: str = "",
    ) -> None:
        self.episode["turns"].append(
            {
                "node_id": node_id,
                "action": action,
                "assistant_text": assistant_text,
                "user_text": user_text,
                "timestamp": datetime.utcnow().isoformat(),
            }
        )

    def add_safety_event(self, event: dict[str, Any]) -> None:
        self.episode["safety_events"].append(event)

    def set_result(
        self,
        *,
        status: str,
        answers: dict[int, int] | None = None,
        skipped_questions: list[int] | None = None,
        score: int | None = None,
        interpretation: dict[str, Any] | None = None,
        failure_type: str | None = None,
        dropout: bool = False,
    ) -> None:
        self.episode["status"] = status
        self.episode["answers"] = {str(k): v for k, v in (answers or {}).items()}
        self.episode["skipped_questions"] = skipped_questions or []
        self.episode["score"] = score
        self.episode["interpretation"] = interpretation
        self.episode["failure_type"] = failure_type
        self.episode["dropout"] = dropout

    def increment_clarification(self) -> None:
        self.episode["clarification_count"] += 1

    def add_note(self, note: str) -> None:
        self.episode["notes"].append(note)

    def save(self) -> Path:
        self.episode["ended_at"] = datetime.utcnow().isoformat()
        self.episode_dir.mkdir(parents=True, exist_ok=True)
        path = self.episode_dir / f"{self.episode['episode_id']}.json"
        path.write_text(
            json.dumps(self.episode, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return path
