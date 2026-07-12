import asyncio
from pathlib import Path

import pytest

from xtalk.psych_sop.scid import (
    AssessmentDecision,
    AssessmentLedger,
    BackgroundAssessor,
    RuleBasedDialogueModel,
    SCIDDualLMRuntime,
    SCIDPDFWidgetExtractor,
    load_scid_template,
)
from xtalk.psych_sop.scid.decision import (
    DecisionParseError,
    parse_assessment_decision,
)
from xtalk.psych_sop.scid.frontend import frontend_tool_names
from xtalk.psych_sop.scid.ledger import LedgerValidationError
from xtalk.serving.event_bus import EventBus
from xtalk.serving.events import (
    ASRResultFinal,
    ConsumeLLMAgentGenerationRequested,
    LLMAgentLoop,
)
from xtalk.serving.modules.scid_dual_lm_manager import SCIDDualLMManager


class MockAssessor(BackgroundAssessor):
    def __init__(self, decisions):
        self.decisions = list(decisions)

    async def assess(self, *, ledger, user_text, turn_id):
        del ledger, user_text, turn_id
        return self.decisions.pop(0)


class FailingAssessor(BackgroundAssessor):
    async def assess(self, *, ledger, user_text, turn_id):
        del ledger, user_text, turn_id
        raise RuntimeError("backend unavailable")


class DelayedAssessor(BackgroundAssessor):
    def __init__(self, decision, delay=0.05):
        self.decision = decision
        self.delay = delay

    async def assess(self, *, ledger, user_text, turn_id):
        del ledger, user_text, turn_id
        await asyncio.sleep(self.delay)
        return self.decision


async def wait_for_condition(predicate, timeout=1.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition was not met before timeout")


def test_scid_template_loads_scan_fields_and_priority_entries():
    template = load_scid_template()

    assert len(template.scan_order) == 30
    assert template.scan_order[0] == "S1-F3"
    assert "F3" in template.fields
    assert "G3" in template.fields
    assert "K3" in template.fields
    assert template.get_field("S1-F3").target_field_id == "F3"


def test_scid_pdf_widget_extractor_reads_form_values_when_available():
    pytest.importorskip("fitz")
    path = Path("../psydata/realdata/PsychologySOP-Template/SCID-5-1506000400.pdf")
    if not path.exists():
        pytest.skip("SCID template PDF is unavailable")

    values = SCIDPDFWidgetExtractor.extract_nonempty(path)
    names = SCIDPDFWidgetExtractor.field_names_from_values(values)

    assert "S1-F3" in names
    assert any(item.value in {"1", "2", "3"} for item in values)


def test_ledger_applies_valid_decision_and_queues_priority_module():
    ledger = AssessmentLedger(template=load_scid_template())
    turn = ledger.begin_turn("有过，很明显")

    ledger.apply_decision(
        AssessmentDecision(
            field_id="S1-F3",
            score="3",
            confidence=0.82,
            evidence=["有过，很明显"],
            next_action="advance",
            clarification_question="",
            reasoning_summary="用户明确肯定。",
        ),
        turn_id=turn.turn_id,
        raw_user_text=turn.user_text,
    )

    assert ledger.field_states["S1-F3"].score == "3"
    assert "F3" in ledger.queued_module_fields
    assert ledger.current_field_id == "S2-F58"


def test_ledger_rejects_invalid_field_and_empty_evidence():
    ledger = AssessmentLedger(template=load_scid_template())
    turn = ledger.begin_turn("有")

    with pytest.raises(LedgerValidationError):
        ledger.apply_decision(
            AssessmentDecision(
                field_id="S2-F58",
                score="3",
                confidence=0.8,
                evidence=["有"],
                next_action="advance",
                clarification_question="",
                reasoning_summary="wrong field",
            ),
            turn_id=turn.turn_id,
            raw_user_text=turn.user_text,
        )

    with pytest.raises(LedgerValidationError):
        ledger.apply_decision(
            AssessmentDecision(
                field_id="S1-F3",
                score="3",
                confidence=0.8,
                evidence=[],
                next_action="advance",
                clarification_question="",
                reasoning_summary="no evidence",
            ),
            turn_id=turn.turn_id,
            raw_user_text=turn.user_text,
        )


def test_decision_parser_accepts_json_code_block_and_normalizes_zero_score():
    decision = parse_assessment_decision(
        """```json
        {
          "field_id": "S1-F3",
          "score": "0",
          "confidence": 0.5,
          "evidence": ["不确定"],
          "next_action": "advance",
          "clarification_question": "",
          "reasoning_summary": "资料不足"
        }
        ```"""
    )

    assert decision.score == "?"
    assert decision.field_id == "S1-F3"


def test_decision_parser_rejects_missing_keys():
    with pytest.raises(DecisionParseError):
        parse_assessment_decision('{"field_id": "S1-F3"}')


def test_frontend_tool_policy_excludes_scoring_tools():
    names = frontend_tool_names()

    assert "submit_patient_reply" in names
    assert not any("score" in name or "diagnose" in name for name in names)


def test_scid_runtime_advance_clarify_and_crisis(tmp_path):
    async def run():
        runtime = SCIDDualLMRuntime(
            episode_dir=tmp_path,
            assessor=MockAssessor(
                [
                    AssessmentDecision(
                        field_id="S1-F3",
                        score="3",
                        confidence=0.8,
                        evidence=["有"],
                        next_action="advance",
                        clarification_question="",
                        reasoning_summary="肯定回答。",
                    ),
                    AssessmentDecision(
                        field_id="S2-F58",
                        score=None,
                        confidence=0.2,
                        evidence=[],
                        next_action="clarify",
                        clarification_question="你能具体说说是什么场合吗？",
                        reasoning_summary="信息不足。",
                    ),
                    AssessmentDecision(
                        field_id="S2-F58",
                        score=None,
                        confidence=0.9,
                        evidence=["我想自杀"],
                        next_action="crisis",
                        clarification_question="",
                        reasoning_summary="安全风险。",
                    ),
                ]
            ),
            dialogue_model=RuleBasedDialogueModel(),
            prefer_deepseek=False,
        )

        start = await runtime.start()
        assert "惊恐发作" in start

        first = await runtime.accept_text("有")
        assert runtime.ledger.field_states["S1-F3"].score == "3"
        assert "担心或害怕" in first.final_text

        second = await runtime.accept_text("不知道")
        assert "具体说说" in second.final_text
        assert "S2-F58" not in runtime.ledger.field_states

        third = await runtime.accept_text("我想自杀")
        assert "紧急服务" in third.final_text
        assert runtime.is_finished is True
        assert runtime.episode_path is not None
        assert runtime.episode_path.exists()

    asyncio.run(run())


def test_scid_runtime_backend_failure_reasks_and_saves_partial(tmp_path):
    async def run():
        runtime = SCIDDualLMRuntime(
            episode_dir=tmp_path,
            assessor=FailingAssessor(),
            dialogue_model=RuleBasedDialogueModel(),
            prefer_deepseek=False,
        )

        await runtime.start()
        response = await runtime.accept_text("有这种感觉")

        assert "具体说说" in response.final_text
        assert runtime.ledger.turns[-1].decision["next_action"] == "reask"
        assert runtime.partial_episode_path.exists()
        assert "backend unavailable" in runtime.partial_episode_path.read_text(
            encoding="utf-8"
        )

    asyncio.run(run())


def test_scid_runtime_routes_partial_then_scores_combined_answer(tmp_path):
    async def run():
        runtime = SCIDDualLMRuntime(
            episode_dir=tmp_path,
            assessor=MockAssessor(
                [
                    AssessmentDecision(
                        field_id="S1-F3",
                        score="3",
                        confidence=0.8,
                        evidence=["合并回答"],
                        next_action="advance",
                        clarification_question="",
                        reasoning_summary="合并后可判定。",
                    )
                ]
            ),
            dialogue_model=RuleBasedDialogueModel(),
            prefer_deepseek=False,
        )

        await runtime.start()
        partial = await runtime.accept_text("有时候会这样，就比如说")
        assert "继续" in partial.final_text
        assert runtime.pending_user_buffer
        assert runtime.ledger.turns == []

        answered = await runtime.accept_text("如果情绪低落我会比较担心")
        assert "S1-F3" in runtime.ledger.field_states
        assert "有时候会这样，就比如说" in runtime.ledger.turns[-1].user_text
        assert "如果情绪低落" in runtime.ledger.turns[-1].user_text
        assert "担心或害怕" in answered.final_text

    asyncio.run(run())


def test_scid_runtime_meta_question_does_not_score_or_advance(tmp_path):
    async def run():
        runtime = SCIDDualLMRuntime(
            episode_dir=tmp_path,
            dialogue_model=RuleBasedDialogueModel(),
            prefer_deepseek=False,
        )

        await runtime.start()
        response = await runtime.accept_text("你需要问多少个问题啊")

        assert "30" in response.final_text
        assert runtime.ledger.current_field_id == "S1-F3"
        assert runtime.ledger.field_states == {}
        assert runtime.interaction_turns[-1].route_decision["route"] == "meta_question"

    asyncio.run(run())


def test_scid_runtime_off_sop_chat_hides_scid_state_and_can_resume(tmp_path):
    async def run():
        runtime = SCIDDualLMRuntime(
            episode_dir=tmp_path,
            dialogue_model=RuleBasedDialogueModel(),
            prefer_deepseek=False,
        )

        await runtime.start()
        chat = await runtime.accept_text("今天天气怎么样")
        assert runtime.interaction_mode == "off_sop_chat"
        assert "SCID" not in chat.final_text
        assert "扫描" not in chat.final_text
        assert runtime.ledger.current_field_id == "S1-F3"
        assert runtime.ledger.field_states == {}

        resumed = await runtime.accept_text("继续吧")
        assert runtime.interaction_mode == "scid"
        assert "惊恐发作" in resumed.final_text

    asyncio.run(run())


def test_scid_runtime_discards_stale_backend_result(tmp_path):
    async def run():
        runtime = SCIDDualLMRuntime(
            episode_dir=tmp_path,
            assessor=DelayedAssessor(
                AssessmentDecision(
                    field_id="S1-F3",
                    score="3",
                    confidence=0.8,
                    evidence=["有"],
                    next_action="advance",
                    clarification_question="",
                    reasoning_summary="肯定回答。",
                )
            ),
            dialogue_model=RuleBasedDialogueModel(),
            prefer_deepseek=False,
        )

        await runtime.start()
        old_task = asyncio.create_task(runtime.accept_text("有", interaction_seq=1))
        await asyncio.sleep(0.01)
        new_response = await runtime.accept_text(
            "你需要问多少个问题啊",
            interaction_seq=2,
        )
        old_response = await old_task

        assert old_response.stale is True
        assert "30" in new_response.final_text
        assert runtime.ledger.current_field_id == "S1-F3"
        assert runtime.ledger.field_states == {}
        assert runtime.ledger.turns == []

    asyncio.run(run())


def test_scid_runtime_ignores_obvious_asr_noise(tmp_path):
    async def run():
        runtime = SCIDDualLMRuntime(
            episode_dir=tmp_path,
            dialogue_model=RuleBasedDialogueModel(),
            prefer_deepseek=False,
        )

        await runtime.start()
        response = await runtime.accept_text("Ma")

        assert response.final_text == ""
        assert runtime.ledger.turns == []
        assert runtime.interaction_turns[-1].ignored_reason == "asr_noise"

    asyncio.run(run())


def test_scid_manager_publishes_start_and_asr_streams(tmp_path):
    async def run():
        bus = EventBus(enable_history=True, max_history=50)
        SCIDDualLMManager(
            event_bus=bus,
            session_id="s",
            pipeline=object(),
            config={
                "data_dir": str(tmp_path),
                "scid_prefer_deepseek": False,
                "scid_enable_wait_text": False,
            },
        )

        await bus.publish(LLMAgentLoop(session_id="s"), wait_for_completion=True)
        await bus.publish(
            ASRResultFinal(session_id="s", text="有"),
            wait_for_completion=True,
        )
        await wait_for_condition(
            lambda: len(
                [
                    event
                    for event in bus.get_history()
                    if isinstance(event, ConsumeLLMAgentGenerationRequested)
                ]
            )
            >= 2
        )

        consume_events = [
            event
            for event in bus.get_history()
            if isinstance(event, ConsumeLLMAgentGenerationRequested)
        ]
        assert len(consume_events) >= 2
        chunks = []
        async for chunk in consume_events[-1].stream:
            chunks.append(chunk)
        assert chunks

    asyncio.run(run())


def test_scid_manager_does_not_block_asr_final_frontend_display(tmp_path):
    async def run():
        bus = EventBus(enable_history=True, max_history=50)
        order = []

        async def fake_frontend_display(event):
            del event
            order.append("frontend_asr_final")

        async def fake_model_output(event):
            del event
            order.append("scid_model_output")

        bus.subscribe(ASRResultFinal, fake_frontend_display, priority=5)
        bus.subscribe(
            ConsumeLLMAgentGenerationRequested, fake_model_output, priority=99
        )
        SCIDDualLMManager(
            event_bus=bus,
            session_id="s",
            pipeline=object(),
            config={
                "data_dir": str(tmp_path),
                "scid_prefer_deepseek": False,
                "scid_enable_wait_text": False,
            },
        )

        await bus.publish(
            ASRResultFinal(session_id="s", text="有"),
            wait_for_completion=True,
        )

        assert order == ["frontend_asr_final"]
        await wait_for_condition(lambda: "scid_model_output" in order)
        assert order[0] == "frontend_asr_final"

    asyncio.run(run())
