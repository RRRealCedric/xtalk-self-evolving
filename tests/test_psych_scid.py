import asyncio
import time
from pathlib import Path

import pytest

from xtalk.psych_sop.scid import (
    AssessmentDecision,
    AssessmentLedger,
    BackgroundAssessor,
    CandidateUtteranceCache,
    ClinicalLatencyController,
    DeepSeekAssessor,
    DeepSeekObserver,
    DialogueModel,
    IncrementalObserver,
    PartialObserverPlan,
    RuleBasedAssessor,
    RuleBasedDialogueModel,
    RuleBasedObserver,
    SCIDDualLMRuntime,
    SCIDPDFWidgetExtractor,
    TurnInterpretation,
    load_scid_template,
)
from xtalk.psych_sop.scid.assessment import backend as assessor_module
from xtalk.psych_sop.scid.decision import (
    DecisionParseError,
    parse_assessment_decision,
)
from xtalk.psych_sop.scid.frontend import frontend_tool_names
from xtalk.psych_sop.scid.ledger import LedgerValidationError
from xtalk.psych_sop.scid.observer import (
    ObserverParseError,
    turn_interpretation_from_payload,
)
from xtalk.psych_sop.scid.policy import observer as observer_module
from xtalk.psych_sop.scid.schema import DialogueDirective
from xtalk.serving.event_bus import EventBus
from xtalk.serving.events import (
    ASRResultFinal,
    ASRResultPartial,
    ConsumeLLMAgentGenerationRequested,
    LLMAgentLoop,
    TurnLLMAgentStopRequested,
)
from xtalk.serving.modules.scid_dual_lm_manager import SCIDDualLMManager


class MockAssessor(BackgroundAssessor):
    def __init__(self, decisions):
        self.decisions = list(decisions)

    async def assess(self, *, ledger, user_text, turn_id, observer_context=None):
        del ledger, user_text, turn_id, observer_context
        return self.decisions.pop(0)


class FailingAssessor(BackgroundAssessor):
    async def assess(self, *, ledger, user_text, turn_id, observer_context=None):
        del ledger, user_text, turn_id, observer_context
        raise RuntimeError("backend unavailable")


class DelayedAssessor(BackgroundAssessor):
    def __init__(self, decision, delay=0.05):
        self.decision = decision
        self.delay = delay

    async def assess(self, *, ledger, user_text, turn_id, observer_context=None):
        del ledger, user_text, turn_id, observer_context
        await asyncio.sleep(self.delay)
        return self.decision


class SequencedDelayedAssessor(BackgroundAssessor):
    def __init__(self, items):
        self.items = list(items)
        self.user_texts = []

    async def assess(self, *, ledger, user_text, turn_id, observer_context=None):
        del ledger, turn_id, observer_context
        self.user_texts.append(user_text)
        delay, decision = self.items.pop(0)
        await asyncio.sleep(delay)
        return decision


class SequencedObserver(IncrementalObserver):
    def __init__(self, actions):
        self.actions = list(actions)

    async def observe(self, *, context):
        item = self.actions.pop(0)
        if len(item) == 4:
            delay, action, confidence, commit_required = item
            await asyncio.sleep(delay)
        else:
            action, confidence, commit_required = item
        field = context.get("current_field") or {}
        slot_by_action = {
            "ask_duration": "duration",
            "ask_frequency": "frequency",
            "ask_most_of_day": "most_of_day",
            "ask_impairment": "impairment",
            "clarify_time_window": "time_window",
        }
        return TurnInterpretation(
            interaction_seq=int(context["interaction_seq"]),
            observer_version=int(context["observer_version"]),
            based_on_state_version=int(context["state_version"]),
            field_id=context.get("current_field_id"),
            dialogue_acts=["answer"],
            current_field_relevance=0.95,
            related_field_ids=[context["current_field_id"]],
            related_module_ids=[str(field.get("module") or "")],
            contextual_memories=[],
            evidence_candidates=[],
            recommended_action=action,
            missing_slots=(
                [slot_by_action[action]] if action in slot_by_action else []
            ),
            needs_deep_assessment=True,
            commit_required=commit_required,
            confidence=confidence,
            source="test",
        )


class EchoDialogueModel(DialogueModel):
    def __init__(self, text="我理解你的意思。", delay=0.0):
        self.text = text
        self.delay = delay
        self.directives = []

    async def render(self, directive):
        self.directives.append(directive)
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.text


class StreamingCaptureDialogueModel(DialogueModel):
    def __init__(self, chunks=None):
        self.chunks = chunks or ["我", "听到了。"]
        self.directives = []
        self.contexts = []

    async def render(self, directive):
        chunks = []
        async for chunk in self.stream(directive):
            chunks.append(chunk)
        return "".join(chunks)

    async def stream(self, directive, context=None):
        self.directives.append(directive)
        self.contexts.append(context or {})
        for chunk in self.chunks:
            await asyncio.sleep(0)
            yield chunk


class SlowInitialDialogueModel(DialogueModel):
    def __init__(self, delay=0.05):
        self.delay = delay

    async def render(self, directive):
        return directive.question_text

    async def stream(self, directive, context=None):
        del context
        if directive.directive_type == "realtime_converse":
            yield "我听到了"
            await asyncio.sleep(self.delay)
            yield "。"
            return
        if directive.question_text:
            yield directive.question_text


async def wait_for_condition(predicate, timeout=1.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition was not met before timeout")


async def collect_stream(stream):
    chunks = []
    async for chunk in stream:
        chunks.append(chunk)
    return "".join(chunks)


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


def test_ledger_rejects_decision_based_on_stale_state_version():
    ledger = AssessmentLedger(template=load_scid_template())
    turn = ledger.begin_turn("有")

    with pytest.raises(LedgerValidationError, match="state version"):
        ledger.apply_decision(
            AssessmentDecision(
                field_id="S1-F3",
                score="3",
                confidence=0.8,
                evidence=["有"],
                next_action="advance",
                clarification_question="",
                reasoning_summary="肯定回答。",
            ),
            turn_id=turn.turn_id,
            raw_user_text=turn.user_text,
            expected_field_id="S1-F3",
            expected_state_version=1,
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


def test_observer_schema_rejects_scoring_authority():
    payload = {
        "interaction_seq": 1,
        "observer_version": 1,
        "based_on_state_version": 0,
        "field_id": "S1-F3",
        "dialogue_acts": ["answer"],
        "current_field_relevance": 0.9,
        "related_field_ids": ["S1-F3"],
        "related_module_ids": ["F"],
        "contextual_memories": [],
        "evidence_candidates": [],
        "recommended_action": "ask_next_field",
        "missing_slots": [],
        "needs_deep_assessment": True,
        "commit_required": False,
        "confidence": 0.95,
        "score": "3",
    }

    with pytest.raises(ObserverParseError, match="forbidden"):
        turn_interpretation_from_payload(payload)


def test_deepseek_observer_uses_flash_json_without_thinking(monkeypatch):
    captured = {}

    class FakeChatModel:
        pass

    def fake_chat_openai(**kwargs):
        captured.update(kwargs)
        return FakeChatModel()

    monkeypatch.setattr(observer_module, "ChatOpenAI", fake_chat_openai)

    observer = DeepSeekObserver(api_key="sk-test")

    assert observer.model_name == "deepseek-v4-flash"
    assert captured["model"] == "deepseek-v4-flash"
    assert captured["max_tokens"] == 600
    assert captured["extra_body"] == {"thinking": {"type": "disabled"}}
    assert captured["model_kwargs"]["response_format"] == {"type": "json_object"}


def test_deepseek_assessor_uses_pro_json_with_max_thinking(monkeypatch):
    captured = {}

    class FakeChatModel:
        pass

    def fake_chat_openai(**kwargs):
        captured.update(kwargs)
        return FakeChatModel()

    monkeypatch.setattr(assessor_module, "ChatOpenAI", fake_chat_openai)

    assessor = DeepSeekAssessor(api_key="sk-test")

    assert assessor.model_name == "deepseek-v4-pro"
    assert captured["model"] == "deepseek-v4-pro"
    assert captured["extra_body"] == {
        "thinking": {"type": "enabled", "reasoning_effort": "max"}
    }
    assert captured["model_kwargs"]["response_format"] == {"type": "json_object"}


def test_realtime_active_observer_must_use_a_distinct_model(tmp_path):
    with pytest.raises(ValueError, match="must differ"):
        SCIDDualLMRuntime(
            episode_dir=tmp_path,
            backend_model="same-model",
            observer_model="same-model",
            prefer_deepseek=False,
            runtime_mode="realtime",
            observer_mode="active",
        )


def test_partial_plan_rejects_seq_field_state_and_age_mismatches(tmp_path):
    runtime = SCIDDualLMRuntime(
        episode_dir=tmp_path,
        prefer_deepseek=False,
    )
    interpretation = TurnInterpretation(
        interaction_seq=1,
        observer_version=1,
        based_on_state_version=runtime.ledger.state_version,
        field_id=runtime.ledger.current_field_id,
        dialogue_acts=["answer"],
        current_field_relevance=0.95,
        related_field_ids=[runtime.ledger.current_field_id],
        related_module_ids=["F"],
        contextual_memories=[],
        evidence_candidates=[],
        recommended_action="ask_duration",
        missing_slots=["duration"],
        needs_deep_assessment=True,
        commit_required=False,
        confidence=0.96,
    )
    plan = PartialObserverPlan(
        interaction_seq=1,
        field_id=runtime.ledger.current_field_id,
        based_on_state_version=runtime.ledger.state_version,
        observer_version=1,
        partial_text="有时候会持续一阵子",
        interpretation=interpretation,
        ready_at=time.time(),
    )

    assert (
        runtime._partial_plan_rejection_reason(
            plan=plan,
            final_text="有时候会持续一阵子",
            interaction_seq=2,
        )
        == "interaction_seq_mismatch"
    )

    plan.interaction_seq = 1
    plan.field_id = "S2-F58"
    assert (
        runtime._partial_plan_rejection_reason(
            plan=plan,
            final_text="有时候会持续一阵子",
            interaction_seq=1,
        )
        == "field_mismatch"
    )

    plan.field_id = runtime.ledger.current_field_id
    plan.based_on_state_version = runtime.ledger.state_version + 1
    assert (
        runtime._partial_plan_rejection_reason(
            plan=plan,
            final_text="有时候会持续一阵子",
            interaction_seq=1,
        )
        == "state_version_mismatch"
    )

    plan.based_on_state_version = runtime.ledger.state_version
    plan.ready_at = time.time() - 4.0
    assert (
        runtime._partial_plan_rejection_reason(
            plan=plan,
            final_text="有时候会持续一阵子",
            interaction_seq=1,
        )
        == "plan_expired"
    )

    plan.ready_at = time.time()
    plan.partial_text = "有时候我在和别人说话会害怕"
    assert (
        runtime._partial_plan_rejection_reason(
            plan=plan,
            final_text="有时候和别人讲话我会害怕",
            interaction_seq=1,
        )
        == ""
    )


def test_scid_runtime_uses_configured_deepseek_key_without_env(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    runtime = SCIDDualLMRuntime(
        episode_dir=tmp_path,
        dialogue_model=RuleBasedDialogueModel(),
        deepseek_api_key="sk-test",
        prefer_deepseek=True,
    )

    assert isinstance(runtime.assessor, DeepSeekAssessor)
    assert isinstance(runtime.observer, DeepSeekObserver)


def test_scid_runtime_falls_back_without_any_deepseek_key(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    runtime = SCIDDualLMRuntime(
        episode_dir=tmp_path,
        dialogue_model=RuleBasedDialogueModel(),
        prefer_deepseek=True,
    )

    assert isinstance(runtime.assessor, RuleBasedAssessor)
    assert isinstance(runtime.observer, RuleBasedObserver)


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


def test_scid_telemetry_records_router_assessor_frontend_and_commit(tmp_path):
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
                    )
                ]
            ),
            dialogue_model=RuleBasedDialogueModel(),
            prefer_deepseek=False,
        )

        await runtime.start()
        await runtime.accept_text("有", interaction_seq=1)

        trace = runtime.snapshot()["latency_traces"][0]
        assert trace["interaction_seq"] == 1
        assert trace["field_id"] == "S1-F3"
        assert trace["route"] == "scid_answer"
        assert trace["pre_router_started_at"] is not None
        assert trace["pre_router_finished_at"] is not None
        assert trace["assessor_started_at"] is not None
        assert trace["assessor_finished_at"] is not None
        assert trace["ledger_committed_at"] is not None
        assert trace["frontend_followup_started_at"] is not None
        assert trace["frontend_followup_finished_at"] is not None
        await wait_for_condition(
            lambda: runtime.snapshot()["latency_traces"][0]["observer_action"]
            is not None
        )
        trace = runtime.snapshot()["latency_traces"][0]
        assert trace["assessor_action"] == "advance"
        assert trace["observer_assessor_agree"] is True

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


def test_scid_runtime_repeated_ack_complaint_is_meta_question(tmp_path):
    async def run():
        runtime = SCIDDualLMRuntime(
            episode_dir=tmp_path,
            dialogue_model=RuleBasedDialogueModel(),
            prefer_deepseek=False,
        )

        await runtime.start()
        response = await runtime.accept_text("为什么每次你都要说好的，我记下了")

        assert "重复" in response.final_text
        assert runtime.ledger.current_field_id == "S1-F3"
        assert runtime.ledger.field_states == {}
        assert runtime.ledger.turns == []
        assert runtime.interaction_turns[-1].route_decision["route"] == "meta_question"

    asyncio.run(run())


def test_scid_realtime_continue_words_respect_meta_and_negation(tmp_path):
    async def run():
        runtime = SCIDDualLMRuntime(
            episode_dir=tmp_path,
            dialogue_model=RuleBasedDialogueModel(),
            prefer_deepseek=False,
            runtime_mode="realtime",
            observer_mode="off",
        )

        await runtime.start()
        why = await runtime.accept_text_realtime(
            "为什么你不继续问啊",
            interaction_seq=1,
        )
        assert why.initial_stream is not None
        why_text = await collect_stream(why.initial_stream)
        assert "处理延迟" in why_text
        assert runtime.interaction_turns[-1].route_decision["route"] == "meta_question"

        interrupted = await runtime.accept_text_realtime(
            "我先打断一下，刚才你为什么不继续问问题",
            interaction_seq=2,
        )
        assert interrupted.initial_stream is not None
        await collect_stream(interrupted.initial_stream)
        assert runtime.interaction_turns[-1].route_decision["route"] == "meta_question"
        assert runtime.interaction_mode == "scid"

        paused = await runtime.accept_text_realtime(
            "我们先不继续这个",
            interaction_seq=3,
        )
        assert paused.initial_stream is not None
        await collect_stream(paused.initial_stream)
        assert runtime.interaction_turns[-1].route_decision["route"] == "pause_scid"
        assert runtime.interaction_mode == "paused"

        resumed = await runtime.accept_text_realtime(
            "我们继续",
            interaction_seq=4,
        )
        assert resumed.initial_stream is not None
        await collect_stream(resumed.initial_stream)
        assert runtime.interaction_turns[-1].route_decision["route"] == "resume_scid"
        assert runtime.interaction_mode == "scid"

    asyncio.run(run())


def test_scid_runtime_non_control_chat_enters_backend_without_committing(tmp_path):
    async def run():
        runtime = SCIDDualLMRuntime(
            episode_dir=tmp_path,
            dialogue_model=RuleBasedDialogueModel(),
            prefer_deepseek=False,
        )

        await runtime.start()
        chat = await runtime.accept_text("今天天气怎么样")
        assert runtime.interaction_mode == "scid"
        assert "回到刚才" in chat.final_text
        assert runtime.ledger.current_field_id == "S1-F3"
        assert runtime.ledger.field_states == {}
        assert runtime.ledger.turns[-1].user_text == "今天天气怎么样"
        assert runtime.ledger.turns[-1].decision["next_action"] == "reask"
        assert runtime.interaction_turns[-1].route_decision["route"] == "scid_answer"

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


def test_scid_progressive_non_scid_routes_do_not_create_followup(tmp_path):
    async def run():
        runtime = SCIDDualLMRuntime(
            episode_dir=tmp_path,
            dialogue_model=RuleBasedDialogueModel(),
            prefer_deepseek=False,
            runtime_mode="parallel",
        )

        await runtime.start()
        response = await runtime.accept_text_progressive(
            "你需要问多少个问题啊",
            interaction_seq=1,
        )

        assert response.followup_task is None
        assert "30" in response.initial_text
        assert runtime.ledger.turns == []
        assert runtime.ledger.current_field_id == "S1-F3"

    asyncio.run(run())


def test_scid_manager_records_asr_partial_without_scoring(tmp_path):
    async def run():
        bus = EventBus(enable_history=True, max_history=50)
        manager = SCIDDualLMManager(
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
            ASRResultPartial(session_id="s", text="有时候"),
            wait_for_completion=True,
        )

        assert manager.runtime.ledger.turns == []
        assert not [
            event
            for event in bus.get_history()
            if isinstance(event, ConsumeLLMAgentGenerationRequested)
        ]

        await bus.publish(
            ASRResultFinal(session_id="s", text="有"),
            wait_for_completion=True,
        )
        await wait_for_condition(
            lambda: bool(manager.runtime.snapshot()["latency_traces"])
        )
        trace = manager.runtime.snapshot()["latency_traces"][0]
        assert trace["asr_partial_first_at"] is not None
        assert trace["asr_final_at"] is not None

    asyncio.run(run())


def test_scid_manager_observes_stable_partial_in_shadow_mode(tmp_path):
    async def run():
        bus = EventBus(enable_history=True, max_history=50)
        manager = SCIDDualLMManager(
            event_bus=bus,
            session_id="s",
            pipeline=object(),
            config={
                "data_dir": str(tmp_path),
                "scid_prefer_deepseek": False,
                "scid_enable_wait_text": False,
                "scid_observer_mode": "shadow",
                "scid_partial_observer_debounce_seconds": 0.01,
            },
        )

        await bus.publish(
            ASRResultPartial(session_id="s", text="有时候会这样"),
            wait_for_completion=True,
        )
        await wait_for_condition(
            lambda: bool(manager.runtime.blackboard.observer_updates)
        )

        update = manager.runtime.blackboard.observer_updates[-1]
        assert update["input_kind"] == "partial"
        assert manager.runtime.ledger.turns == []
        assert not [
            event
            for event in bus.get_history()
            if isinstance(event, ConsumeLLMAgentGenerationRequested)
        ]
        await manager.shutdown()

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


def test_scid_progressive_response_publishes_initial_before_delayed_assessor(tmp_path):
    async def run():
        bus = EventBus(enable_history=True, max_history=80)
        manager = SCIDDualLMManager(
            event_bus=bus,
            session_id="s",
            pipeline=object(),
            config={
                "data_dir": str(tmp_path),
                "scid_prefer_deepseek": False,
                "scid_enable_wait_text": False,
                "scid_runtime_mode": "parallel",
            },
        )
        manager.runtime.assessor = DelayedAssessor(
            AssessmentDecision(
                field_id="S1-F3",
                score="3",
                confidence=0.8,
                evidence=["有"],
                next_action="advance",
                clarification_question="",
                reasoning_summary="肯定回答。",
            ),
            delay=0.25,
        )

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
            >= 1
        )

        consume_events = [
            event
            for event in bus.get_history()
            if isinstance(event, ConsumeLLMAgentGenerationRequested)
        ]
        assert len(consume_events) == 1
        stream = consume_events[0].stream.__aiter__()
        initial = await stream.__anext__()
        assert initial
        assert manager.runtime.ledger.field_states == {}

        followup = await collect_stream(stream)
        assert followup
        assert manager.runtime.ledger.field_states["S1-F3"].score == "3"
        consume_events = [
            event
            for event in bus.get_history()
            if isinstance(event, ConsumeLLMAgentGenerationRequested)
        ]
        assert len(consume_events) == 1
        traces = manager.runtime.snapshot()["latency_traces"]
        assert traces[0]["first_segment_published_at"] is not None
        assert traces[0]["followup_segment_published_at"] is not None

    asyncio.run(run())


def test_scid_progressive_followup_commits_ledger(tmp_path):
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
                ),
                delay=0.01,
            ),
            dialogue_model=RuleBasedDialogueModel(),
            prefer_deepseek=False,
            runtime_mode="parallel",
        )

        await runtime.start()
        response = await runtime.accept_text_progressive("有", interaction_seq=1)

        assert response.initial_text
        assert "S1-F3" not in runtime.ledger.field_states
        assert response.followup_task is not None
        followup = await response.followup_task

        assert "S1-F3" in runtime.ledger.field_states
        assert "担心或害怕" in followup
        assert runtime.progressive_turns[-1]["followup_text"] == followup

    asyncio.run(run())


def test_scid_progressive_initial_uses_frontend_dialogue_model(tmp_path):
    async def run():
        dialogue = EchoDialogueModel(text="听起来你是在否认这类情况。")
        runtime = SCIDDualLMRuntime(
            episode_dir=tmp_path,
            assessor=DelayedAssessor(
                AssessmentDecision(
                    field_id="S1-F3",
                    score="1",
                    confidence=0.8,
                    evidence=["没有"],
                    next_action="advance",
                    clarification_question="",
                    reasoning_summary="否定回答。",
                ),
                delay=0.01,
            ),
            dialogue_model=dialogue,
            prefer_deepseek=False,
            runtime_mode="parallel",
            frontend_initial_timeout_seconds=0.2,
        )

        await runtime.start()
        response = await runtime.accept_text_progressive("没有", interaction_seq=1)

        assert response.initial_text == "听起来你是在否认这类情况。"
        bridge = next(
            item for item in dialogue.directives if item.directive_type == "bridge_ack"
        )
        assert "用户原话：没有" in bridge.progress_text
        assert bridge.question_text == ""
        assert response.followup_task is not None
        await response.followup_task

    asyncio.run(run())


def test_scid_progressive_initial_falls_back_on_frontend_timeout(tmp_path):
    async def run():
        dialogue = EchoDialogueModel(text="这句话不应该及时返回。", delay=0.05)
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
                ),
                delay=0.01,
            ),
            dialogue_model=dialogue,
            prefer_deepseek=False,
            runtime_mode="parallel",
            frontend_initial_timeout_seconds=0.001,
        )

        await runtime.start()
        response = await runtime.accept_text_progressive("有", interaction_seq=1)

        assert "有过" in response.initial_text or "出现过" in response.initial_text
        assert response.initial_text != "好的，我记下了。"
        assert response.followup_task is not None
        await response.followup_task

    asyncio.run(run())


def test_scid_progressive_ambiguous_negative_is_not_called_clear(tmp_path):
    async def run():
        runtime = SCIDDualLMRuntime(
            episode_dir=tmp_path,
            assessor=DelayedAssessor(
                AssessmentDecision(
                    field_id="S1-F3",
                    score="?",
                    confidence=0.5,
                    evidence=["我觉得应该没有吧"],
                    next_action="clarify",
                    clarification_question="你目前更倾向于没有，是这样吗？",
                    reasoning_summary="回答带有不确定语气。",
                ),
                delay=0.01,
            ),
            dialogue_model=RuleBasedDialogueModel(),
            prefer_deepseek=False,
            runtime_mode="parallel",
        )

        await runtime.start()
        response = await runtime.accept_text_progressive(
            "我觉得应该没有吧",
            interaction_seq=1,
        )

        assert "回答很明确" not in response.initial_text
        assert (
            "不确定" in response.initial_text or "不是完全确定" in response.initial_text
        )
        assert response.followup_task is not None
        await response.followup_task

    asyncio.run(run())


def test_scid_progressive_stale_followup_is_dropped(tmp_path):
    async def run():
        bus = EventBus(enable_history=True, max_history=100)
        manager = SCIDDualLMManager(
            event_bus=bus,
            session_id="s",
            pipeline=object(),
            config={
                "data_dir": str(tmp_path),
                "scid_prefer_deepseek": False,
                "scid_enable_wait_text": False,
                "scid_runtime_mode": "parallel",
            },
        )
        manager.runtime.assessor = DelayedAssessor(
            AssessmentDecision(
                field_id="S1-F3",
                score="3",
                confidence=0.8,
                evidence=["有"],
                next_action="advance",
                clarification_question="",
                reasoning_summary="肯定回答。",
            ),
            delay=0.2,
        )

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
            >= 1
        )
        await bus.publish(
            ASRResultFinal(session_id="s", text="你需要问多少个问题啊"),
            wait_for_completion=True,
        )
        await wait_for_condition(
            lambda: len(manager.runtime.snapshot()["latency_traces"]) >= 2
        )
        await asyncio.sleep(0.3)

        assert manager.runtime.ledger.field_states == {}
        traces = manager.runtime.snapshot()["latency_traces"]
        assert traces[0]["stale"] is True
        assert traces[0]["followup_segment_published_at"] is None

    asyncio.run(run())


def test_scid_realtime_frontend_stream_starts_before_delayed_assessor(tmp_path):
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
                ),
                delay=0.25,
            ),
            dialogue_model=StreamingCaptureDialogueModel(["嗯", "，我听到了。"]),
            prefer_deepseek=False,
            runtime_mode="realtime",
        )

        await runtime.start()
        response = await runtime.accept_text_realtime("有", interaction_seq=1)

        assert response.initial_stream is not None
        first = await collect_stream(response.initial_stream)
        assert "听到了" in first
        assert runtime.ledger.field_states == {}
        trace = runtime.snapshot()["latency_traces"][0]
        assert trace["frontend_first_token_at"] is not None
        assert trace["assessor_finished_at"] is None
        assert response.action_stream_task is not None
        action_stream = await response.action_stream_task
        assert action_stream is not None
        await collect_stream(action_stream)

    asyncio.run(run())


def test_scid_realtime_promotes_matching_partial_observer_plan(tmp_path):
    async def run():
        runtime = SCIDDualLMRuntime(
            episode_dir=tmp_path,
            assessor=DelayedAssessor(
                AssessmentDecision(
                    field_id="S1-F3",
                    score="3",
                    confidence=0.8,
                    evidence=["有时候会持续一阵子"],
                    next_action="advance",
                    clarification_question="",
                    reasoning_summary="后台结果不应抢在已提交的追问后播出。",
                ),
                delay=1.0,
            ),
            observer=SequencedObserver(
                [
                    (0.0, "ask_duration", 0.96, False),
                    (1.0, "hold_for_assessor", 0.95, False),
                ]
            ),
            dialogue_model=RuleBasedDialogueModel(),
            prefer_deepseek=False,
            runtime_mode="realtime",
            observer_mode="active",
            enable_candidate_pregeneration=False,
            fast_policy_enabled=False,
            post_initial_action_wait_seconds=0.05,
        )

        await runtime.start()
        await runtime.observe_asr_partial(
            "有时候会持续一阵子",
            interaction_seq=1,
        )
        response = await runtime.accept_text_realtime(
            "有时候会持续一阵子，但我说不准",
            interaction_seq=1,
        )

        assert response.initial_stream is not None
        await collect_stream(response.initial_stream)
        assert response.action_stream_task is not None
        action_stream = await asyncio.wait_for(
            response.action_stream_task,
            timeout=0.2,
        )
        assert action_stream is not None
        assert "持续多久" in await collect_stream(action_stream)
        assert runtime.foreground_actions[-1]["source"] == "observer_partial"
        trace = runtime.snapshot()["latency_traces"][0]
        assert trace["partial_plan_ready_at"] is not None
        assert trace["partial_plan_promoted_at"] is not None
        assert runtime.ledger.field_states == {}

    asyncio.run(run())


def test_scid_realtime_rejects_mismatched_partial_plan(tmp_path):
    async def run():
        runtime = SCIDDualLMRuntime(
            episode_dir=tmp_path,
            assessor=DelayedAssessor(
                AssessmentDecision(
                    field_id="S1-F3",
                    score="1",
                    confidence=0.9,
                    evidence=["完全没有"],
                    next_action="advance",
                    clarification_question="",
                    reasoning_summary="明确否定。",
                ),
                delay=0.0,
            ),
            observer=SequencedObserver(
                [
                    ("ask_duration", 0.96, False),
                    ("hold_for_assessor", 0.95, False),
                ]
            ),
            dialogue_model=RuleBasedDialogueModel(),
            prefer_deepseek=False,
            runtime_mode="realtime",
            observer_mode="active",
            enable_candidate_pregeneration=False,
            fast_policy_enabled=False,
        )

        await runtime.start()
        await runtime.observe_asr_partial(
            "有时候会持续一阵子",
            interaction_seq=1,
        )
        response = await runtime.accept_text_realtime(
            "完全没有",
            interaction_seq=1,
        )

        assert response.initial_stream is not None
        await collect_stream(response.initial_stream)
        assert response.action_stream_task is not None
        action_stream = await response.action_stream_task
        assert action_stream is not None
        await collect_stream(action_stream)
        trace = runtime.snapshot()["latency_traces"][0]
        assert trace["partial_plan_promoted_at"] is None
        assert trace["partial_plan_rejected_reason"] == "partial_final_mismatch"
        assert runtime.foreground_actions[-1]["source"] == "assessor"

    asyncio.run(run())


def test_scid_realtime_assessor_supersedes_observer_before_boundary(tmp_path):
    async def run():
        runtime = SCIDDualLMRuntime(
            episode_dir=tmp_path,
            assessor=DelayedAssessor(
                AssessmentDecision(
                    field_id="S1-F3",
                    score="3",
                    confidence=0.9,
                    evidence=["有时候会这样"],
                    next_action="advance",
                    clarification_question="",
                    reasoning_summary="信息足以推进。",
                ),
                delay=0.01,
            ),
            observer=SequencedObserver([("ask_duration", 0.96, False)]),
            dialogue_model=SlowInitialDialogueModel(delay=0.05),
            prefer_deepseek=False,
            runtime_mode="realtime",
            observer_mode="active",
            enable_candidate_pregeneration=False,
            fast_policy_enabled=False,
        )

        await runtime.start()
        response = await runtime.accept_text_realtime(
            "有时候会这样",
            interaction_seq=1,
        )

        assert response.initial_stream is not None
        await collect_stream(response.initial_stream)
        assert response.action_stream_task is not None
        action_stream = await response.action_stream_task
        assert action_stream is not None
        assert "担心或害怕" in await collect_stream(action_stream)
        assert runtime.foreground_actions[-1]["source"] == "assessor"
        turn_record = runtime.realtime_turns[-1]
        assert turn_record["broker"]["superseded"]
        assert len(runtime.foreground_actions) == 1

    asyncio.run(run())


def test_scid_realtime_final_observer_replaces_matching_partial_candidate(
    tmp_path,
):
    async def run():
        runtime = SCIDDualLMRuntime(
            episode_dir=tmp_path,
            assessor=DelayedAssessor(
                AssessmentDecision(
                    field_id="S1-F3",
                    score="3",
                    confidence=0.8,
                    evidence=["有时候会这样"],
                    next_action="advance",
                    clarification_question="",
                    reasoning_summary="后台较慢。",
                ),
                delay=1.0,
            ),
            observer=SequencedObserver(
                [
                    ("ask_duration", 0.96, False),
                    ("ask_duration", 0.97, False),
                ]
            ),
            dialogue_model=SlowInitialDialogueModel(delay=0.05),
            prefer_deepseek=False,
            runtime_mode="realtime",
            observer_mode="active",
            enable_candidate_pregeneration=False,
            fast_policy_enabled=False,
        )

        await runtime.start()
        await runtime.observe_asr_partial(
            "有时候会这样持续一阵",
            interaction_seq=1,
        )
        response = await runtime.accept_text_realtime(
            "有时候会这样持续一阵子",
            interaction_seq=1,
        )

        assert response.initial_stream is not None
        await collect_stream(response.initial_stream)
        assert response.action_stream_task is not None
        action_stream = await response.action_stream_task
        assert action_stream is not None
        assert "持续多久" in await collect_stream(action_stream)
        assert runtime.foreground_actions[-1]["source"] == "observer"
        assert runtime.realtime_turns[-1]["broker"]["superseded"]
        assert len(runtime.foreground_actions) == 1

    asyncio.run(run())


def test_scid_realtime_stale_initial_cancels_old_assessor(tmp_path):
    async def run():
        assessor = SequencedDelayedAssessor(
            [
                (
                    1.0,
                    AssessmentDecision(
                        field_id="S1-F3",
                        score="3",
                        confidence=0.9,
                        evidence=["有"],
                        next_action="advance",
                        clarification_question="",
                        reasoning_summary="旧结果不得提交。",
                    ),
                ),
                (
                    0.0,
                    AssessmentDecision(
                        field_id="S1-F3",
                        score="1",
                        confidence=0.9,
                        evidence=["没有"],
                        next_action="advance",
                        clarification_question="",
                        reasoning_summary="最新回答提交。",
                    ),
                ),
            ]
        )
        runtime = SCIDDualLMRuntime(
            episode_dir=tmp_path,
            assessor=assessor,
            dialogue_model=SlowInitialDialogueModel(delay=1.0),
            prefer_deepseek=False,
            runtime_mode="realtime",
            observer_mode="off",
            fast_policy_enabled=False,
        )

        await runtime.start()
        first = await runtime.accept_text_realtime("有", interaction_seq=1)
        assert first.initial_stream is not None
        first_iterator = first.initial_stream.__aiter__()
        assert await first_iterator.__anext__() == "我听到了"
        await wait_for_condition(
            lambda: runtime.blackboard.assessor_status == "running"
        )

        second = await runtime.accept_text_realtime("没有", interaction_seq=2)
        await first_iterator.aclose()
        assert first.action_stream_task is not None
        assert await asyncio.wait_for(first.action_stream_task, timeout=0.2) is None

        assert second.initial_stream is not None
        await collect_stream(second.initial_stream)
        assert second.action_stream_task is not None
        second_action = await asyncio.wait_for(
            second.action_stream_task,
            timeout=0.3,
        )
        assert second_action is not None
        await collect_stream(second_action)

        assert runtime.ledger.field_states["S1-F3"].score == "1"
        assert len(runtime.ledger.field_states) == 1
        first_trace = runtime.snapshot()["latency_traces"][0]
        assert first_trace["stale"] is True

    asyncio.run(run())


def test_scid_realtime_clear_scan_answer_uses_fast_policy_candidate(tmp_path):
    async def run():
        runtime = SCIDDualLMRuntime(
            episode_dir=tmp_path,
            assessor=DelayedAssessor(
                AssessmentDecision(
                    field_id="S1-F3",
                    score="1",
                    confidence=0.9,
                    evidence=["没有"],
                    next_action="advance",
                    clarification_question="",
                    reasoning_summary="否定回答。",
                ),
                delay=0.15,
            ),
            dialogue_model=RuleBasedDialogueModel(),
            prefer_deepseek=False,
            runtime_mode="realtime",
            fast_policy_enabled=True,
        )

        await runtime.start()
        response = await runtime.accept_text_realtime("没有", interaction_seq=1)

        assert response.initial_stream is not None
        await collect_stream(response.initial_stream)
        assert response.action_stream_task is not None
        action_stream = await response.action_stream_task
        assert action_stream is not None
        action_text = await collect_stream(action_stream)
        assert "担心或害怕" in action_text
        assert runtime.foreground_actions[-1]["source"] == "fast_policy"
        assert runtime.blackboard.speculative_depth == 1
        await wait_for_condition(
            lambda: "S1-F3" in runtime.ledger.field_states,
            timeout=1.0,
        )
        assert runtime.ledger.field_states["S1-F3"].score == "1"

    asyncio.run(run())


def test_scid_realtime_uncertain_negative_waits_for_clinical_action(tmp_path):
    async def run():
        runtime = SCIDDualLMRuntime(
            episode_dir=tmp_path,
            assessor=DelayedAssessor(
                AssessmentDecision(
                    field_id="S1-F3",
                    score="?",
                    confidence=0.55,
                    evidence=["应该没有吧"],
                    next_action="clarify",
                    clarification_question="你说应该没有，是完全没有，还是有些记不清？",
                    reasoning_summary="回答包含明显的不确定性。",
                ),
                delay=0.01,
            ),
            dialogue_model=RuleBasedDialogueModel(),
            prefer_deepseek=False,
            runtime_mode="realtime",
            observer_mode="off",
            fast_policy_enabled=True,
        )

        await runtime.start()
        response = await runtime.accept_text_realtime(
            "应该没有吧",
            interaction_seq=1,
        )

        assert response.initial_stream is not None
        await collect_stream(response.initial_stream)
        assert response.action_stream_task is not None
        action_stream = await response.action_stream_task
        assert action_stream is not None
        assert "完全没有" in await collect_stream(action_stream)
        assert runtime.foreground_actions[-1]["source"] == "assessor"
        assert runtime.blackboard.speculative_depth == 0

    asyncio.run(run())


def test_scid_realtime_action_grace_accepts_just_late_assessor(tmp_path):
    async def run():
        runtime = SCIDDualLMRuntime(
            episode_dir=tmp_path,
            assessor=DelayedAssessor(
                AssessmentDecision(
                    field_id="S1-F3",
                    score="1",
                    confidence=0.9,
                    evidence=["没有"],
                    next_action="advance",
                    clarification_question="",
                    reasoning_summary="明确否定。",
                ),
                delay=0.13,
            ),
            dialogue_model=RuleBasedDialogueModel(),
            prefer_deepseek=False,
            runtime_mode="realtime",
            observer_mode="off",
            fast_policy_enabled=False,
            realtime_action_timeout_seconds=0.1,
            realtime_action_grace_seconds=0.2,
            post_initial_action_wait_seconds=0.2,
        )

        await runtime.start()
        response = await runtime.accept_text_realtime("没有", interaction_seq=1)

        assert response.initial_stream is not None
        await collect_stream(response.initial_stream)
        assert response.action_stream_task is not None
        action_stream = await asyncio.wait_for(
            response.action_stream_task,
            timeout=0.5,
        )
        assert action_stream is not None
        assert "担心或害怕" in await collect_stream(action_stream)
        assert runtime.foreground_actions[-1]["source"] == "assessor"
        assert runtime.ledger.field_states["S1-F3"].score == "1"

    asyncio.run(run())


def test_scid_realtime_timeout_always_returns_safe_same_field_probe(tmp_path):
    async def run():
        runtime = SCIDDualLMRuntime(
            episode_dir=tmp_path,
            assessor=DelayedAssessor(
                AssessmentDecision(
                    field_id="S1-F3",
                    score="3",
                    confidence=0.8,
                    evidence=["和别人说话会害怕"],
                    next_action="advance",
                    clarification_question="",
                    reasoning_summary="后台最终会返回，但已超过本轮时限。",
                ),
                delay=1.0,
            ),
            dialogue_model=RuleBasedDialogueModel(),
            prefer_deepseek=False,
            runtime_mode="realtime",
            observer_mode="off",
            fast_policy_enabled=False,
            realtime_action_timeout_seconds=0.1,
            realtime_action_grace_seconds=0.0,
            post_initial_action_wait_seconds=0.1,
        )

        await runtime.start()
        response = await runtime.accept_text_realtime(
            "我和别人说话时会害怕",
            interaction_seq=1,
        )

        assert response.initial_stream is not None
        await collect_stream(response.initial_stream)
        assert response.action_stream_task is not None
        action_stream = await asyncio.wait_for(
            response.action_stream_task,
            timeout=0.5,
        )
        assert action_stream is not None
        assert "典型的例子" in await collect_stream(action_stream)
        assert runtime.foreground_actions[-1]["source"] == "timeout_fallback"
        assert runtime.ledger.field_states == {}
        assert "和别人说话时会害怕" in runtime.pending_user_buffer
        assert runtime.blackboard.pending_foreground_probe is not None

    asyncio.run(run())


def test_scid_realtime_promotes_confirmed_speculative_reply(tmp_path):
    async def run():
        assessor = SequencedDelayedAssessor(
            [
                (
                    0.0,
                    AssessmentDecision(
                        field_id="S2-F58",
                        score="1",
                        confidence=0.9,
                        evidence=["没有这种担心"],
                        next_action="advance",
                        clarification_question="",
                        reasoning_summary="明确否定。",
                    ),
                )
            ]
        )
        runtime = SCIDDualLMRuntime(
            episode_dir=tmp_path,
            assessor=assessor,
            dialogue_model=RuleBasedDialogueModel(),
            prefer_deepseek=False,
            runtime_mode="realtime",
            observer_mode="off",
            fast_policy_enabled=False,
        )

        await runtime.start()
        runtime.ledger.current_field_id = "S2-F58"
        runtime.blackboard.sync_committed_state(
            state_version=runtime.ledger.state_version,
            current_field_id="S2-F58",
        )
        state = runtime.blackboard.begin_speculation(
            source_field_id="S1-F3",
            speculative_field_id="S2-F58",
            source_interaction_seq=0,
            based_on_state_version=runtime.ledger.state_version,
            question_text=runtime.ledger.current_field.question_text,
        )
        state.status = "confirmed"
        state.deferred_user_text = "之前已经说过没有这种担心"

        response = await runtime.accept_text_realtime(
            "我再确认一次，确实没有",
            interaction_seq=1,
        )
        assert response.initial_stream is not None
        await collect_stream(response.initial_stream)
        assert response.action_stream_task is not None
        action_stream = await response.action_stream_task
        assert action_stream is not None
        await collect_stream(action_stream)

        assert runtime.blackboard.speculative_depth == 0
        assert "之前已经说过没有这种担心" in assessor.user_texts[-1]
        assert "我再确认一次，确实没有" in assessor.user_texts[-1]
        assert runtime.ledger.field_states["S2-F58"].score == "1"

    asyncio.run(run())


def test_scid_realtime_initial_bridge_is_richer_without_asking_a_question(tmp_path):
    async def run():
        runtime = SCIDDualLMRuntime(
            episode_dir=tmp_path,
            assessor=DelayedAssessor(
                AssessmentDecision(
                    field_id="S1-F3",
                    score="1",
                    confidence=0.9,
                    evidence=["完全没有"],
                    next_action="advance",
                    clarification_question="",
                    reasoning_summary="明确否定。",
                ),
                delay=0.2,
            ),
            dialogue_model=RuleBasedDialogueModel(),
            prefer_deepseek=False,
            runtime_mode="realtime",
            fast_policy_enabled=False,
        )

        await runtime.start()
        response = await runtime.accept_text_realtime("完全没有", interaction_seq=1)

        assert response.initial_stream is not None
        initial = await collect_stream(response.initial_stream)
        assert "回答很明确" not in initial
        assert "没有" in initial or "否定" in initial
        assert "？" not in initial
        assert runtime.ledger.field_states == {}

        runtime.cancel_background_tasks()

    asyncio.run(run())


def test_scid_realtime_manager_keeps_initial_and_action_in_one_stream(tmp_path):
    async def run():
        bus = EventBus(enable_history=True, max_history=80)
        manager = SCIDDualLMManager(
            event_bus=bus,
            session_id="s",
            pipeline=object(),
            config={
                "data_dir": str(tmp_path),
                "scid_prefer_deepseek": False,
                "scid_runtime_mode": "realtime",
                "scid_observer_mode": "off",
                "scid_fast_policy_enabled": True,
            },
        )
        manager.runtime.assessor = DelayedAssessor(
            AssessmentDecision(
                field_id="S1-F3",
                score="1",
                confidence=0.9,
                evidence=["没有"],
                next_action="advance",
                clarification_question="",
                reasoning_summary="明确否定。",
            ),
            delay=0.1,
        )

        await bus.publish(
            ASRResultFinal(session_id="s", text="没有"),
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
            == 1
        )

        consume_events = [
            event
            for event in bus.get_history()
            if isinstance(event, ConsumeLLMAgentGenerationRequested)
        ]
        chunks = []
        async for chunk in consume_events[0].stream:
            chunks.append(chunk)

        assert len(chunks) >= 2
        assert "担心或害怕" in "".join(chunks)
        assert (
            len(
                [
                    event
                    for event in bus.get_history()
                    if isinstance(event, ConsumeLLMAgentGenerationRequested)
                ]
            )
            == 1
        )

    asyncio.run(run())


def test_scid_manager_coalesces_tiny_tts_preamble_clause():
    async def run():
        async def source():
            yield "我想再确认一下"
            yield "，您有没有遇到过这种情况？"
            yield "这是一个足够长的完整说明部分，后面继续。"

        text = await collect_stream(
            SCIDDualLMManager._coalesce_short_tts_clauses(source())
        )

        assert "我想再确认一下，" not in text
        assert "我想再确认一下 您有没有遇到过这种情况？" in text
        assert "这是一个足够长的完整说明部分，" in text

    asyncio.run(run())


def test_scid_realtime_observer_probe_holds_field_and_combines_reply(tmp_path):
    async def run():
        assessor = SequencedDelayedAssessor(
            [
                (
                    1.0,
                    AssessmentDecision(
                        field_id="S1-F3",
                        score="3",
                        confidence=0.8,
                        evidence=["有时候会这样"],
                        next_action="advance",
                        clarification_question="",
                        reasoning_summary="原回答可能足够。",
                    ),
                ),
                (
                    0.0,
                    AssessmentDecision(
                        field_id="S1-F3",
                        score="3",
                        confidence=0.9,
                        evidence=["有时候会这样，每次十分钟"],
                        next_action="advance",
                        clarification_question="",
                        reasoning_summary="追问后信息完整。",
                    ),
                ),
            ]
        )
        runtime = SCIDDualLMRuntime(
            episode_dir=tmp_path,
            assessor=assessor,
            observer=SequencedObserver(
                [
                    ("ask_duration", 0.95, False),
                    ("hold_for_assessor", 0.95, False),
                ]
            ),
            dialogue_model=RuleBasedDialogueModel(),
            prefer_deepseek=False,
            runtime_mode="realtime",
            observer_mode="active",
            observer_confidence_threshold=0.9,
            fast_policy_enabled=False,
        )

        await runtime.start()
        first = await runtime.accept_text_realtime("有时候会这样", interaction_seq=1)
        assert first.initial_stream is not None
        await collect_stream(first.initial_stream)
        assert first.action_stream_task is not None
        first_action = await asyncio.wait_for(first.action_stream_task, timeout=0.2)
        assert first_action is not None
        assert "持续多久" in await collect_stream(first_action)
        assert runtime.foreground_actions[-1]["source"] == "observer"
        assert runtime.ledger.current_field_id == "S1-F3"
        assert runtime.ledger.turns == []
        assert runtime.blackboard.pending_foreground_probe is not None
        assert "有时候会这样" in runtime.pending_user_buffer

        second = await runtime.accept_text_realtime("每次十分钟", interaction_seq=2)
        assert second.initial_stream is not None
        await collect_stream(second.initial_stream)
        assert second.action_stream_task is not None
        second_action = await asyncio.wait_for(second.action_stream_task, timeout=0.2)
        assert second_action is not None
        await collect_stream(second_action)

        assert "有时候会这样" in assessor.user_texts[-1]
        assert "每次十分钟" in assessor.user_texts[-1]
        assert runtime.ledger.field_states["S1-F3"].score == "3"
        assert runtime.blackboard.pending_foreground_probe is None
        assert runtime.pending_user_buffer == ""

    asyncio.run(run())


def test_scid_realtime_observer_can_propose_one_speculative_scan_step(tmp_path):
    async def run():
        runtime = SCIDDualLMRuntime(
            episode_dir=tmp_path,
            assessor=DelayedAssessor(
                AssessmentDecision(
                    field_id="S1-F3",
                    score="1",
                    confidence=0.9,
                    evidence=["没有"],
                    next_action="advance",
                    clarification_question="",
                    reasoning_summary="明确否定。",
                ),
                delay=0.2,
            ),
            observer=SequencedObserver([("ask_next_field", 0.99, False)]),
            dialogue_model=RuleBasedDialogueModel(),
            prefer_deepseek=False,
            runtime_mode="realtime",
            observer_mode="active",
            enable_optimistic_scan=True,
            fast_policy_enabled=False,
        )

        await runtime.start()
        response = await runtime.accept_text_realtime("没有", interaction_seq=1)
        assert response.initial_stream is not None
        await collect_stream(response.initial_stream)
        assert response.action_stream_task is not None
        action_stream = await asyncio.wait_for(response.action_stream_task, timeout=0.1)
        assert action_stream is not None
        assert "担心或害怕" in await collect_stream(action_stream)
        assert runtime.foreground_actions[-1]["source"] == "observer"
        assert runtime.blackboard.speculative_depth == 1

        await wait_for_condition(
            lambda: "S1-F3" in runtime.ledger.field_states,
            timeout=1.0,
        )
        assert runtime.ledger.field_states["S1-F3"].score == "1"

    asyncio.run(run())


def test_scid_realtime_cautious_module_does_not_optimistically_advance(tmp_path):
    async def run():
        runtime = SCIDDualLMRuntime(
            episode_dir=tmp_path,
            assessor=DelayedAssessor(
                AssessmentDecision(
                    field_id="F3",
                    score="?",
                    confidence=0.4,
                    evidence=["不确定"],
                    next_action="clarify",
                    clarification_question="你能具体说说当时发生了什么吗？",
                    reasoning_summary="模块入口信息不足。",
                ),
                delay=0.01,
            ),
            dialogue_model=RuleBasedDialogueModel(),
            prefer_deepseek=False,
            runtime_mode="realtime",
            fast_policy_enabled=True,
        )

        await runtime.start()
        runtime.ledger.current_field_id = "F3"
        runtime.blackboard.sync_committed_state(
            state_version=runtime.ledger.state_version,
            current_field_id="F3",
        )
        response = await runtime.accept_text_realtime("有", interaction_seq=1)

        assert response.initial_stream is not None
        await collect_stream(response.initial_stream)
        assert response.action_stream_task is not None
        action_stream = await response.action_stream_task
        assert action_stream is not None
        text = await collect_stream(action_stream)
        assert "具体说说" in text
        assert runtime.foreground_actions[-1]["source"] == "assessor"
        assert runtime.blackboard.speculative_depth == 0

    asyncio.run(run())


def test_scid_realtime_context_privacy(tmp_path):
    async def run():
        dialogue = StreamingCaptureDialogueModel(["我理解。"])
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
                ),
                delay=0.01,
            ),
            dialogue_model=dialogue,
            prefer_deepseek=False,
            runtime_mode="realtime",
        )

        await runtime.start()
        response = await runtime.accept_text_realtime("有", interaction_seq=1)
        assert response.initial_stream is not None
        await collect_stream(response.initial_stream)

        context_text = str(dialogue.contexts[-1])
        assert "score" not in context_text
        assert "diagnosis" not in context_text
        assert "raw_payload" not in context_text
        assert "decision" not in context_text

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


def test_scid_manager_invalidates_prior_assessment_when_new_final_arrives(tmp_path):
    async def run():
        bus = EventBus(enable_history=True, max_history=50)
        stop_calls = 0

        async def delay_second_stop(event):
            nonlocal stop_calls
            del event
            stop_calls += 1
            if stop_calls == 2:
                await asyncio.sleep(0.3)

        bus.subscribe(TurnLLMAgentStopRequested, delay_second_stop, priority=99)
        manager = SCIDDualLMManager(
            event_bus=bus,
            session_id="s",
            pipeline=object(),
            config={
                "data_dir": str(tmp_path),
                "scid_prefer_deepseek": False,
                "scid_enable_wait_text": False,
            },
        )
        manager.runtime.assessor = DelayedAssessor(
            AssessmentDecision(
                field_id="S1-F3",
                score="1",
                confidence=0.9,
                evidence=["没有"],
                next_action="advance",
                clarification_question="",
                reasoning_summary="否定回答。",
            ),
            delay=0.1,
        )

        await bus.publish(
            ASRResultFinal(session_id="s", text="没有"),
            wait_for_completion=True,
        )
        await wait_for_condition(
            lambda: manager.runtime.blackboard.assessor_status == "running"
        )

        await bus.publish(
            ASRResultFinal(session_id="s", text="你需要问多少个问题？"),
            wait_for_completion=True,
        )
        await wait_for_condition(lambda: stop_calls == 2)
        await wait_for_condition(lambda: not manager._active_asr_tasks)

        assert manager.runtime.ledger.field_states == {}
        assert manager.runtime.ledger.current_field_id == "S1-F3"
        assert manager.runtime.latency_traces[1].stale is True

    asyncio.run(run())


def test_scid_observer_shadow_records_all_non_noise_conversation(tmp_path):
    async def run():
        runtime = SCIDDualLMRuntime(
            episode_dir=tmp_path,
            dialogue_model=RuleBasedDialogueModel(),
            prefer_deepseek=False,
            observer_mode="shadow",
        )

        await runtime.start()
        response = await runtime.accept_text("你要问多少个问题？", interaction_seq=1)
        assert "30" in response.final_text
        await wait_for_condition(
            lambda: runtime.interaction_turns[-1].observer_decision is not None
        )

        observation = runtime.interaction_turns[-1].observer_decision
        assert observation["dialogue_acts"]
        assert runtime.ledger.turns == []
        assert runtime.blackboard.observer_updates

    asyncio.run(run())


def test_scid_observer_keeps_background_as_context_not_committed_evidence(tmp_path):
    async def run():
        runtime = SCIDDualLMRuntime(
            episode_dir=tmp_path,
            dialogue_model=RuleBasedDialogueModel(),
            prefer_deepseek=False,
            observer_mode="shadow",
        )

        await runtime.start()
        await runtime.accept_text(
            "小时候父母工作很忙，我们也经常搬家", interaction_seq=1
        )
        await wait_for_condition(lambda: bool(runtime.blackboard.contextual_memories))

        memory = runtime.blackboard.contextual_memories[-1]
        assert memory["status"] == "context_only"
        assert "小时候" in memory["content"]
        assert runtime.ledger.field_states == {}

    asyncio.run(run())


def test_candidate_utterance_cache_is_scoped_by_state_version():
    async def run():
        cache = CandidateUtteranceCache(RuleBasedDialogueModel())
        directive = DialogueDirective(
            directive_type="candidate_question",
            field_id="S2-F58",
            question_text="下一题是什么？",
            instruction="只问候选问题。",
        )

        generated = await cache.pre_generate(
            field_id="S1-F3",
            state_version=0,
            directives={"ask_next_field": directive},
        )
        assert generated["ask_next_field"] == "下一题是什么？"
        assert cache.get(field_id="S1-F3", state_version=0, action="ask_next_field")

        cache.invalidate_before(1)
        assert (
            cache.get(field_id="S1-F3", state_version=0, action="ask_next_field")
            is None
        )

    asyncio.run(run())


def test_latency_controller_forbids_speculation_for_conservative_gate():
    template = load_scid_template()
    controller = ClinicalLatencyController(observer_confidence_threshold=0.9)
    interpretation = TurnInterpretation(
        interaction_seq=1,
        observer_version=1,
        based_on_state_version=0,
        field_id="F3",
        dialogue_acts=["answer"],
        current_field_relevance=0.98,
        related_field_ids=["F3"],
        related_module_ids=["F"],
        contextual_memories=[],
        evidence_candidates=[],
        recommended_action="ask_next_field",
        missing_slots=[],
        needs_deep_assessment=True,
        commit_required=False,
        confidence=0.99,
    )

    plan = controller.plan(
        field=template.get_field("F3"),
        interpretation=interpretation,
        optimistic_scan_enabled=True,
        speculative_depth=0,
        repair_pending=False,
    )

    assert plan.allow_speculation is False
    assert plan.mode == "hold_for_assessor"


def test_scid_optimistic_scan_asks_next_before_assessor_commit(tmp_path):
    async def run():
        runtime = SCIDDualLMRuntime(
            episode_dir=tmp_path,
            assessor=DelayedAssessor(
                AssessmentDecision(
                    field_id="S1-F3",
                    score="3",
                    confidence=0.9,
                    evidence=["有"],
                    next_action="advance",
                    clarification_question="",
                    reasoning_summary="明确肯定。",
                ),
                delay=0.2,
            ),
            dialogue_model=RuleBasedDialogueModel(),
            observer=RuleBasedObserver(),
            prefer_deepseek=False,
            runtime_mode="parallel",
            observer_mode="active",
            enable_optimistic_scan=True,
        )

        await runtime.start()
        response = await runtime.accept_text_progressive("有", interaction_seq=1)
        followup = await asyncio.wait_for(response.followup_task, timeout=0.1)

        assert "担心或害怕" in followup
        assert runtime.ledger.field_states == {}
        assert runtime.blackboard.speculative_depth == 1
        assert runtime.snapshot()["latency_traces"][0]["speculative_advance"] is True

        await wait_for_condition(
            lambda: "S1-F3" in runtime.ledger.field_states,
            timeout=1.0,
        )
        await wait_for_condition(
            lambda: runtime.blackboard.speculative_depth == 0,
            timeout=1.0,
        )
        assert runtime.ledger.current_field_id == "S2-F58"

    asyncio.run(run())


def test_scid_optimistic_scan_defers_one_next_field_reply(tmp_path):
    async def run():
        runtime = SCIDDualLMRuntime(
            episode_dir=tmp_path,
            assessor=SequencedDelayedAssessor(
                [
                    (
                        0.2,
                        AssessmentDecision(
                            field_id="S1-F3",
                            score="3",
                            confidence=0.9,
                            evidence=["有"],
                            next_action="advance",
                            clarification_question="",
                            reasoning_summary="明确肯定。",
                        ),
                    ),
                    (
                        0.0,
                        AssessmentDecision(
                            field_id="S2-F58",
                            score="1",
                            confidence=0.9,
                            evidence=["没有"],
                            next_action="advance",
                            clarification_question="",
                            reasoning_summary="明确否定。",
                        ),
                    ),
                ]
            ),
            dialogue_model=RuleBasedDialogueModel(),
            observer=RuleBasedObserver(),
            prefer_deepseek=False,
            runtime_mode="parallel",
            observer_mode="active",
            enable_optimistic_scan=True,
        )

        await runtime.start()
        first = await runtime.accept_text_progressive("有", interaction_seq=1)
        assert "担心或害怕" in await asyncio.wait_for(first.followup_task, timeout=0.1)

        second = await runtime.accept_text_progressive("没有", interaction_seq=2)
        assert runtime.blackboard.speculative_depth == 1
        assert len(runtime.ledger.turns) == 1
        second_followup = await asyncio.wait_for(second.followup_task, timeout=1.0)

        assert second_followup
        assert runtime.ledger.field_states["S1-F3"].score == "3"
        assert runtime.ledger.field_states["S2-F58"].score == "1"
        assert runtime.blackboard.speculative_depth == 0
        assert len(runtime.ledger.turns) == 2

    asyncio.run(run())


def test_scid_optimistic_rejection_repairs_without_scoring_next_reply(tmp_path):
    async def run():
        runtime = SCIDDualLMRuntime(
            episode_dir=tmp_path,
            assessor=SequencedDelayedAssessor(
                [
                    (
                        0.2,
                        AssessmentDecision(
                            field_id="S1-F3",
                            score=None,
                            confidence=0.55,
                            evidence=[],
                            next_action="clarify",
                            clarification_question="这种情况确实发生过吗？",
                            reasoning_summary="仍缺少明确回答。",
                        ),
                    ),
                    (
                        0.0,
                        AssessmentDecision(
                            field_id="S1-F3",
                            score="3",
                            confidence=0.9,
                            evidence=["确实有"],
                            next_action="advance",
                            clarification_question="",
                            reasoning_summary="澄清后明确肯定。",
                        ),
                    ),
                ]
            ),
            dialogue_model=RuleBasedDialogueModel(),
            observer=RuleBasedObserver(),
            prefer_deepseek=False,
            runtime_mode="parallel",
            observer_mode="active",
            enable_optimistic_scan=True,
        )

        await runtime.start()
        first = await runtime.accept_text_progressive("有", interaction_seq=1)
        await asyncio.wait_for(first.followup_task, timeout=0.1)

        speculative_reply = await runtime.accept_text_progressive(
            "下一题我没有这种担心", interaction_seq=2
        )
        repair = await asyncio.wait_for(speculative_reply.followup_task, timeout=1.0)

        assert "回到刚才" in repair
        assert runtime.ledger.current_field_id == "S1-F3"
        assert runtime.ledger.field_states == {}
        assert runtime.blackboard.repair_pending["announced"] is True

        clarified = await runtime.accept_text_progressive("确实有过", interaction_seq=3)
        await asyncio.wait_for(clarified.followup_task, timeout=1.0)
        assert runtime.ledger.field_states["S1-F3"].score == "3"
        assert runtime.blackboard.repair_pending is None

    asyncio.run(run())


def test_scid_crisis_preempts_pending_speculative_assessment(tmp_path):
    async def run():
        runtime = SCIDDualLMRuntime(
            episode_dir=tmp_path,
            assessor=DelayedAssessor(
                AssessmentDecision(
                    field_id="S1-F3",
                    score="3",
                    confidence=0.9,
                    evidence=["有"],
                    next_action="advance",
                    clarification_question="",
                    reasoning_summary="明确肯定。",
                ),
                delay=1.0,
            ),
            dialogue_model=RuleBasedDialogueModel(),
            observer=RuleBasedObserver(),
            prefer_deepseek=False,
            runtime_mode="parallel",
            observer_mode="active",
            enable_optimistic_scan=True,
        )

        await runtime.start()
        first = await runtime.accept_text_progressive("有", interaction_seq=1)
        await asyncio.wait_for(first.followup_task, timeout=0.1)
        assert runtime.blackboard.speculative_depth == 1

        crisis = await asyncio.wait_for(
            runtime.accept_text_progressive("我不想活了", interaction_seq=2),
            timeout=0.2,
        )

        assert "安全" in crisis.initial_text
        assert crisis.followup_task is None
        assert runtime.is_finished is True
        assert runtime.ledger.terminal_status == "crisis"
        assert runtime.blackboard.speculative_depth == 0
        assert runtime.ledger.field_states == {}

    asyncio.run(run())


def test_scid_crisis_preempts_pending_sequential_assessment(tmp_path):
    async def run():
        runtime = SCIDDualLMRuntime(
            episode_dir=tmp_path,
            assessor=DelayedAssessor(
                AssessmentDecision(
                    field_id="S1-F3",
                    score="3",
                    confidence=0.9,
                    evidence=["有"],
                    next_action="advance",
                    clarification_question="",
                    reasoning_summary="明确肯定。",
                ),
                delay=1.0,
            ),
            dialogue_model=RuleBasedDialogueModel(),
            prefer_deepseek=False,
            runtime_mode="sequential",
        )

        await runtime.start()
        old = asyncio.create_task(runtime.accept_text("有", interaction_seq=1))
        await wait_for_condition(
            lambda: runtime.blackboard.assessor_status == "running"
        )
        crisis = await asyncio.wait_for(
            runtime.accept_text("我不想活了", interaction_seq=2),
            timeout=0.2,
        )

        assert "安全" in crisis.final_text
        assert runtime.ledger.terminal_status == "crisis"
        assert runtime.ledger.field_states == {}
        with pytest.raises(asyncio.CancelledError):
            await old

    asyncio.run(run())
