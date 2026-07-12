from pathlib import Path

import pytest

from xtalk.psych_sop.memory_backend import LocalJsonMemoryBackend
from xtalk.psych_sop.runtime import (
    PsychSOPRuntime,
    normalize_scale_id,
    select_scale,
)


@pytest.mark.parametrize(
    "raw,expected",
    [("gad7", "GAD-7"), ("GAD-7", "GAD-7"), ("ＰＨＱ９", "PHQ-9"), ("scl90", "SCL-90")],
)
def test_normalize_scale_id_canonicalizes_asr_forms(raw, expected):
    assert normalize_scale_id(raw) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("GAD-7", "GAD-7"),
        ("gad7", "GAD-7"),
        ("GAD 7", "GAD-7"),
        ("ＧＡＤ７", "GAD-7"),
        ("gad7。", "GAD-7"),
        ("我想做gad7", "GAD-7"),
        ("焦虑", "GAD-7"),
        ("第一个", "GAD-7"),
        ("", "GAD-7"),  # empty -> default
        ("默认", "GAD-7"),  # accept default
        ("phq9", "PHQ-9"),
        ("PHQ-9", "PHQ-9"),
        ("情绪低落", "PHQ-9"),
        ("第二个", "PHQ-9"),
        ("我最近很抑郁", "PHQ-9"),
    ],
)
def test_select_scale_handles_asr_variants(text, expected):
    assert select_scale(text, "GAD-7") == expected


@pytest.mark.parametrize("text", ["xyz", "今天天气怎么样", "gad和phq"])
def test_select_scale_returns_none_when_ambiguous(text):
    assert select_scale(text, "GAD-7") is None


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


def test_runtime_advances_past_scale_selection_with_asr_text(tmp_path: Path):
    runtime = _runtime(tmp_path)
    runtime.start()
    for user_text in ["", "", "同意", "测试流程"]:
        runtime.accept_text(user_text)

    assert runtime.snapshot()["current_node"] == "SCALE_SELECTION"

    runtime.accept_text("gad7")  # ASR'd scale name, previously looped

    assert runtime.snapshot()["current_node"] != "SCALE_SELECTION"


def test_runtime_full_flow_with_spoken_numeral_answers(tmp_path: Path):
    runtime = _runtime(tmp_path)
    runtime.start()

    reply = ""
    for user_text in ["", "", "同意", "测试流程", "焦虑", "没有", *["一"] * 7]:
        reply = runtime.accept_text(user_text)

    assert "总分是 7" in reply
    assert runtime.snapshot()["score"] == 7


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
