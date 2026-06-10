import json
from pathlib import Path

from xtalk.psych_sop.episode_logger import EpisodeLogger


def test_episode_logger_saves_json(tmp_path: Path):
    logger = EpisodeLogger(
        user_id="u",
        task="psych_sop_scale_demo",
        scale_id="GAD-7",
        sop_version="sop",
        prompt_version="prompt",
        episode_dir=tmp_path,
    )
    logger.add_turn(
        node_id="START",
        action="greet",
        assistant_text="你好",
        user_text="开始",
    )
    logger.set_result(status="completed", answers={0: 1}, score=1)

    path = logger.save()
    data = json.loads(path.read_text(encoding="utf-8"))

    assert path.exists()
    assert data["status"] == "completed"
    assert data["answers"] == {"0": 1}
