from xtalk.psych_sop.scale_engine import ScaleEngine


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
