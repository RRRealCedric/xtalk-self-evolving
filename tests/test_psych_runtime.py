from pathlib import Path

from xtalk.psych_sop.memory_backend import LocalJsonMemoryBackend
from xtalk.psych_sop.runtime import PsychSOPRuntime


def _runtime(tmp_path: Path) -> PsychSOPRuntime:
    return PsychSOPRuntime(
        scale_id="GAD-7",
        prefer_mem0=False,
        memory_backend=LocalJsonMemoryBackend(path=tmp_path / "memory.json"),
        episode_dir=tmp_path / "episodes",
    )


def test_runtime_start_returns_greeting(tmp_path: Path):
    runtime = _runtime(tmp_path)

    assert "demo" in runtime.start()
    assert runtime.snapshot()["current_node"] == "START"


def test_runtime_gad7_full_flow_scores_seven(tmp_path: Path):
    runtime = _runtime(tmp_path)
    runtime.start()

    reply = ""
    for user_text in ["", "", "同意", "测试流程", "", "没有", *["1"] * 7]:
        reply = runtime.accept_text(user_text)

    assert "总分是 7" in reply
    assert runtime.snapshot()["score"] == 7
    assert runtime.is_finished is False

    close = runtime.accept_text("")
    assert "谢谢" in close
    assert runtime.is_finished is True
    assert runtime.episode_path is not None
    assert runtime.episode_path.exists()


def test_runtime_crisis_finishes_episode(tmp_path: Path):
    runtime = _runtime(tmp_path)
    runtime.start()

    reply = runtime.accept_text("我想自杀")

    assert "紧急服务" in reply
    assert runtime.is_finished is True
    assert runtime.snapshot()["status"] == "crisis"
    assert runtime.episode_path is not None
    assert runtime.episode_path.exists()


def test_runtime_abort_finishes_episode(tmp_path: Path):
    runtime = _runtime(tmp_path)
    runtime.start()

    reply = runtime.accept_text("退出")

    assert "停在这里" in reply
    assert runtime.is_finished is True
    assert runtime.snapshot()["status"] == "aborted"
