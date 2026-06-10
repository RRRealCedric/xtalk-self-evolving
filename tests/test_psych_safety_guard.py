from xtalk.psych_sop.safety_guard import SafetyGuard


def test_self_harm_text_interrupts_sop():
    result = SafetyGuard().classify("我想自杀")

    assert result.risk_level == "high"
    assert result.risk_type == "self_harm"
    assert result.should_interrupt_sop is True
