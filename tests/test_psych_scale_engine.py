import pytest

from xtalk.psych_sop.scale_engine import ScaleEngine


@pytest.mark.parametrize(
    "text,expected",
    [
        ("0", 0),
        ("3", 3),
        ("零", 0),
        ("一", 1),
        ("两", 2),
        ("三", 3),
        ("１", 1),  # fullwidth digit
        ("选三", 3),
        ("第1个", 1),
        ("2。", 2),
    ],
)
def test_parse_answer_accepts_asr_number_variants(text, expected):
    engine = ScaleEngine()
    engine.start_scale("GAD-7")  # options 0..3

    option_id, confidence, _ = engine.parse_answer(text)

    assert option_id == expected
    assert confidence >= 0.6


@pytest.mark.parametrize(
    "text,expected",
    [("一点", 1), ("一半", 2), ("完全没有", 0), ("几乎每天", 3)],
)
def test_parse_answer_keyword_fallback_survives_numeral_parsing(text, expected):
    engine = ScaleEngine()
    engine.start_scale("GAD-7")

    assert engine.parse_answer(text)[0] == expected


@pytest.mark.parametrize("text", ["五", "不知道", "今天天气不错"])
def test_parse_answer_rejects_out_of_range_and_garbage(text):
    engine = ScaleEngine()
    engine.start_scale("GAD-7")

    option_id, confidence, _ = engine.parse_answer(text)

    assert option_id is None
    assert confidence == 0.0


def test_gad7_all_one_scores_seven():
    engine = ScaleEngine()
    engine.start_scale("GAD-7")
    for index in range(7):
        engine.record_answer(index, 1, "1")
        if engine.has_next_question():
            engine.next_question()

    assert engine.compute_score() == 7


def test_phq9_all_two_scores_eighteen():
    engine = ScaleEngine()
    engine.start_scale("PHQ-9")
    for index in range(9):
        engine.record_answer(index, 2, "2")
        if engine.has_next_question():
            engine.next_question()

    assert engine.compute_score() == 18
