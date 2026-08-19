import asyncio
import gc
import json
import re
import time
import tracemalloc
from pathlib import Path
from types import SimpleNamespace

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
    ForegroundActionBroker,
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
from xtalk.psych_sop.scid.orchestration.saga import SpeculationSaga
from xtalk.psych_sop.scid.orchestration.event_store import EpisodeEventStore
from xtalk.psych_sop.scid.orchestration.state_graph import (
    DomainEvent,
    RetentionPolicy,
    RuntimePolicy,
    SpeculationPhase,
)
from xtalk.psych_sop.scid.orchestration.supervisor import TurnSupervisor
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

    async def assess(self, *, request):
        del request
        return self.decisions.pop(0)


class FailingAssessor(BackgroundAssessor):
    async def assess(self, *, request):
        del request
        raise RuntimeError("backend unavailable")


class DelayedAssessor(BackgroundAssessor):
    def __init__(self, decision, delay=0.05):
        self.decision = decision
        self.delay = delay

    async def assess(self, *, request):
        del request
        await asyncio.sleep(self.delay)
        return self.decision


class SequencedDelayedAssessor(BackgroundAssessor):
    def __init__(self, items):
        self.items = list(items)
        self.user_texts = []

    async def assess(self, *, request):
        self.user_texts.append(request.user_text)
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
            input_kind=str(context.get("input_kind") or "final"),
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


async def collect_runtime_response(response, timeout=1.0):
    initial_text = (
        await collect_stream(response.initial_stream)
        if response.initial_stream is not None
        else ""
    )
    action_text = ""
    if response.action_stream_task is not None:
        action_stream = await asyncio.wait_for(
            response.action_stream_task,
            timeout=timeout,
        )
        if action_stream is not None:
            action_text = await collect_stream(action_stream)
    return initial_text, action_text


async def accept_runtime_text(runtime, text, interaction_seq=None, timeout=1.5):
    """Exercise the public realtime façade and collect its combined output."""

    seq = (
        runtime.claim_interaction_seq() if interaction_seq is None else interaction_seq
    )
    response = await runtime.accept_text(text, interaction_seq=seq)
    initial_text, action_text = await collect_runtime_response(
        response,
        timeout=timeout,
    )
    if not response.stale and not runtime.is_finished:
        await runtime.acomplete_response_delivery(seq, success=True)
    if not runtime.is_finished:
        await runtime.event_store.flush()
    return SimpleNamespace(
        final_text=f"{initial_text}{action_text}",
        stale=response.stale,
        interaction_seq=seq,
    )


def test_scid_template_loads_scan_fields_and_priority_entries():
    template = load_scid_template()

    assert len(template.scan_order) == 30
    assert template.scan_order[0] == "S1-F3"
    assert "F3" in template.fields
    assert "G3" in template.fields
    assert "K3" in template.fields
    assert template.get_field("S1-F3").target_field_id == "F3"


def test_scid_opening_does_not_expose_scan_progress_label(tmp_path):
    async def run():
        runtime = SCIDDualLMRuntime(
            episode_dir=tmp_path,
            dialogue_model=RuleBasedDialogueModel(),
            prefer_deepseek=False,
        )

        opening = await runtime.start()

        assert "扫描模块第" not in opening
        assert "第 1/30 题" not in opening
        assert "惊恐发作" in opening
        await runtime.aclose(status="aborted")

    asyncio.run(run())


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
    turn = ledger.begin_turn("有过，很明显", interaction_seq=1)

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
    )

    assert ledger.field_states["S1-F3"].score == "3"
    assert "F3" in ledger.queued_module_fields
    assert ledger.current_field_id == "S2-F58"


def test_ledger_rejects_invalid_field_and_empty_evidence():
    ledger = AssessmentLedger(template=load_scid_template())
    turn = ledger.begin_turn("有", interaction_seq=1)

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
        )


def test_ledger_rejects_decision_based_on_stale_state_version():
    ledger = AssessmentLedger(template=load_scid_template())
    turn = ledger.begin_turn("有", interaction_seq=1)

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
            observer_mode="active",
        )


def test_runtime_rejects_unknown_observer_mode(tmp_path):
    with pytest.raises(ValueError, match="observer_mode"):
        SCIDDualLMRuntime(
            episode_dir=tmp_path,
            prefer_deepseek=False,
            observer_mode="typo",
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


def test_scid_episode_id_is_readable_sortable_and_safe(tmp_path):
    runtime = SCIDDualLMRuntime(
        episode_dir=tmp_path,
        dialogue_model=RuleBasedDialogueModel(),
        prefer_deepseek=False,
    )

    assert re.fullmatch(
        r"scid_\d{8}T\d{6}Z_[0-9a-f]{8}",
        runtime.episode_id,
    )
    assert runtime.partial_episode_path.name == (f"{runtime.episode_id}.partial.json")
    assert runtime.event_store.event_log_path.name == (
        f"{runtime.episode_id}.events.jsonl"
    )


def test_turn_supervisor_consumes_deadline_with_injected_monotonic_clock():
    async def run():
        supervisor = TurnSupervisor(
            interaction_seq=1,
            deadline_monotonic=101.0,
            clock=lambda: 100.0,
        )
        await supervisor.start()

        async def operation():
            await asyncio.sleep(0)
            return "ok"

        handle = supervisor.spawn(operation(), name="clock-domain-check")
        assert await handle == "ok"
        await supervisor.close()

    asyncio.run(run())


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
        assert "非诊断性对话" in start
        assert "普通的烦躁或压力" in start
        assert "随时暂停或结束" in start
        assert runtime.snapshot()["product_contract_version"] == "0.1.0"
        assert runtime.snapshot()["deployment_scope"] == "research-only"
        assert runtime.snapshot()["intended_use"] == "non_diagnostic_structured_support"
        snapshot = runtime.snapshot()
        assert snapshot["snapshot_schema_version"] == 4
        assert snapshot["runtime_profile"] == "realtime_v3"
        assert snapshot["raw_transcript_persisted"] is False
        assert snapshot["one_step_speculation_enabled"] is False
        assert snapshot["runtime_turns"] == []
        assert "runtime_mode" not in snapshot
        assert "realtime_turns" not in snapshot

        first = await accept_runtime_text(runtime, "有")
        assert runtime.ledger.field_states["S1-F3"].score == "3"
        assert "担心或害怕" in first.final_text

        second = await accept_runtime_text(runtime, "不知道")
        assert "具体说说" in second.final_text
        assert "S2-F58" not in runtime.ledger.field_states

        third = await accept_runtime_text(runtime, "我想自杀")
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
        response = await accept_runtime_text(runtime, "有这种感觉")

        assert "具体说说" in response.final_text
        assert runtime.ledger.turns[-1].decision["next_action"] == "reask"
        assert runtime.partial_episode_path.exists()
        assert "backend unavailable" not in runtime.partial_episode_path.read_text(
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
        await accept_runtime_text(runtime, "有", interaction_seq=1)

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
        partial = await accept_runtime_text(runtime, "有时候会这样，就比如说")
        assert "继续" in partial.final_text
        assert runtime.pending_user_buffer
        assert runtime.ledger.turns == []

        answered = await accept_runtime_text(runtime, "如果情绪低落我会比较担心")
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
        response = await accept_runtime_text(runtime, "你需要问多少个问题啊")

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
        response = await accept_runtime_text(
            runtime, "为什么每次你都要说好的，我记下了"
        )

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
            observer_mode="off",
        )

        await runtime.start()
        why = await runtime.accept_text(
            "为什么你不继续问啊",
            interaction_seq=1,
        )
        assert why.initial_stream is not None
        why_text = await collect_stream(why.initial_stream)
        assert "处理延迟" in why_text
        assert runtime.interaction_turns[-1].route_decision["route"] == "meta_question"

        interrupted = await runtime.accept_text(
            "我先打断一下，刚才你为什么不继续问问题",
            interaction_seq=2,
        )
        assert interrupted.initial_stream is not None
        await collect_stream(interrupted.initial_stream)
        assert runtime.interaction_turns[-1].route_decision["route"] == "meta_question"
        assert runtime.interaction_mode == "scid"

        paused = await runtime.accept_text(
            "我们先不继续这个",
            interaction_seq=3,
        )
        assert paused.initial_stream is not None
        await collect_stream(paused.initial_stream)
        assert runtime.interaction_turns[-1].route_decision["route"] == "pause_scid"
        assert runtime.interaction_mode == "paused"

        resumed = await runtime.accept_text(
            "我们继续",
            interaction_seq=4,
        )
        assert resumed.initial_stream is not None
        await collect_stream(resumed.initial_stream)
        assert runtime.interaction_turns[-1].route_decision["route"] == "resume_scid"
        assert runtime.interaction_mode == "scid"

        natural_resume = await runtime.accept_text(
            "好的，那你继续",
            interaction_seq=5,
        )
        assert natural_resume.initial_stream is not None
        await collect_stream(natural_resume.initial_stream)
        assert runtime.interaction_turns[-1].route_decision["route"] == "resume_scid"

        repeated = await runtime.accept_text(
            "这道题你刚才问过了",
            interaction_seq=6,
        )
        assert repeated.initial_stream is not None
        repeated_text = await collect_stream(repeated.initial_stream)
        assert "已经问过" in repeated_text
        assert runtime.interaction_turns[-1].route_decision["route"] == "meta_question"
        assert runtime.ledger.turns == []

    asyncio.run(run())


def test_scid_runtime_non_control_chat_enters_backend_without_committing(tmp_path):
    async def run():
        runtime = SCIDDualLMRuntime(
            episode_dir=tmp_path,
            dialogue_model=RuleBasedDialogueModel(),
            prefer_deepseek=False,
        )

        await runtime.start()
        chat = await accept_runtime_text(runtime, "今天天气怎么样")
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
        old_task = asyncio.create_task(
            accept_runtime_text(runtime, "有", interaction_seq=1)
        )
        await asyncio.sleep(0.01)
        new_response = await accept_runtime_text(
            runtime,
            "你需要问多少个问题啊",
            interaction_seq=2,
        )
        await old_task

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
        response = await accept_runtime_text(runtime, "...")

        assert response.final_text == ""
        assert runtime.ledger.turns == []
        assert runtime.interaction_turns[-1].ignored_reason == "asr_noise"

    asyncio.run(run())


@pytest.mark.parametrize("answer", ["no", "yes"])
def test_scid_runtime_keeps_short_english_answers(tmp_path, answer):
    async def run():
        runtime = SCIDDualLMRuntime(
            episode_dir=tmp_path,
            dialogue_model=RuleBasedDialogueModel(),
            prefer_deepseek=False,
        )

        await runtime.start()
        await accept_runtime_text(runtime, answer)

        assert runtime.ledger.turns
        assert runtime.interaction_turns[-1].ignored_reason == ""

    asyncio.run(run())


@pytest.mark.parametrize(
    ("legacy_key", "legacy_value"),
    [
        ("scid_runtime_mode", "realtime"),
        ("scid_enable_wait_text", False),
        ("scid_candidate_pregeneration", True),
        ("scid_optimistic_scan", True),
        ("scid_frontend_initial_timeout_seconds", 1.2),
        ("scid_frontend_streaming", True),
        ("scid_fast_policy_enabled", True),
        ("scid_realtime_action_timeout_seconds", 8.0),
        ("scid_realtime_action_grace_seconds", 1.0),
        ("scid_realtime_observer_planning_enabled", True),
    ],
)
def test_scid_manager_rejects_removed_runtime_config(
    tmp_path,
    legacy_key,
    legacy_value,
):
    with pytest.raises(ValueError) as exc_info:
        SCIDDualLMManager(
            event_bus=EventBus(),
            session_id="s",
            pipeline=object(),
            config={
                "data_dir": str(tmp_path),
                "scid_prefer_deepseek": False,
                legacy_key: legacy_value,
            },
        )

    assert legacy_key in str(exc_info.value)


@pytest.mark.parametrize("invalid_mode", ["", "typo", False, 0, None])
def test_scid_manager_rejects_invalid_observer_mode(tmp_path, invalid_mode):
    with pytest.raises(ValueError, match="observer_mode"):
        SCIDDualLMManager(
            event_bus=EventBus(),
            session_id="s",
            pipeline=object(),
            config={
                "data_dir": str(tmp_path),
                "scid_prefer_deepseek": False,
                "scid_observer_mode": invalid_mode,
            },
        )


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
        )

        await runtime.start()
        response = await runtime.accept_text("有", interaction_seq=1)

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
            observer_mode="active",
            post_initial_action_wait_seconds=0.05,
        )

        await runtime.start()
        await runtime.observe_asr_partial(
            "有时候会持续一阵子",
            interaction_seq=1,
        )
        response = await runtime.accept_text(
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
            observer_mode="active",
        )

        await runtime.start()
        await runtime.observe_asr_partial(
            "有时候会持续一阵子",
            interaction_seq=1,
        )
        response = await runtime.accept_text(
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
            observer_mode="active",
        )

        await runtime.start()
        response = await runtime.accept_text(
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
        turn_record = runtime.runtime_turns[-1]
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
            observer_mode="active",
        )

        await runtime.start()
        await runtime.observe_asr_partial(
            "有时候会这样持续一阵",
            interaction_seq=1,
        )
        response = await runtime.accept_text(
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
        assert runtime.runtime_turns[-1]["broker"]["superseded"]
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
            observer_mode="off",
        )

        await runtime.start()
        first = await runtime.accept_text("有", interaction_seq=1)
        assert first.initial_stream is not None
        first_iterator = first.initial_stream.__aiter__()
        assert await first_iterator.__anext__() == "我听到了"
        await wait_for_condition(
            lambda: runtime.blackboard.assessor_status == "running"
        )

        second = await runtime.accept_text("没有", interaction_seq=2)
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


@pytest.mark.parametrize("candidate_source", ["fast_policy", "observer"])
def test_scid_default_blocks_all_next_field_speculation(
    tmp_path,
    candidate_source,
):
    async def run():
        observer = (
            SequencedObserver([("ask_next_field", 0.99, False)])
            if candidate_source == "observer"
            else RuleBasedObserver()
        )
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
                    reasoning_summary="后台结果不应在安全等待窗口内完成。",
                ),
                delay=1.0,
            ),
            observer=observer,
            dialogue_model=RuleBasedDialogueModel(),
            prefer_deepseek=False,
            observer_mode=("active" if candidate_source == "observer" else "off"),
            post_initial_action_wait_seconds=0.05,
        )

        await runtime.start()
        response = await runtime.accept_text("没有", interaction_seq=1)
        _, action_text = await collect_runtime_response(response, timeout=0.3)

        assert runtime.allow_one_step_speculation is False
        assert runtime.blackboard.speculative_depth == 0
        assert runtime.foreground_actions[-1]["source"] == "assessor"
        assert runtime.foreground_actions[-1]["kind"] == "ask_committed"
        assert "担心或害怕" in action_text
        assert runtime.ledger.field_states["S1-F3"].score == "1"
        await runtime.aclose(status="aborted")

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
                    evidence=["对"],
                    next_action="advance",
                    clarification_question="",
                    reasoning_summary="明确短答。",
                ),
                delay=0.15,
            ),
            dialogue_model=RuleBasedDialogueModel(),
            prefer_deepseek=False,
            allow_one_step_speculation=True,
        )

        await runtime.start()
        response = await runtime.accept_text("对", interaction_seq=1)

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


@pytest.mark.parametrize("answer", ["我觉得应该没有", "完全没有"])
def test_scid_realtime_clear_negative_scan_answer_fast_commits(tmp_path, answer):
    async def run():
        runtime = SCIDDualLMRuntime(
            episode_dir=tmp_path,
            assessor=DelayedAssessor(
                AssessmentDecision(
                    field_id="S1-F3",
                    score="1",
                    confidence=0.9,
                    evidence=[answer],
                    next_action="advance",
                    clarification_question="",
                    reasoning_summary="远端后台故意很慢，本地否定规则应先推进。",
                ),
                delay=1.0,
            ),
            dialogue_model=RuleBasedDialogueModel(),
            prefer_deepseek=False,
            observer_mode="off",
            post_initial_action_wait_seconds=0.05,
        )

        await runtime.start()
        response = await runtime.accept_text(answer, interaction_seq=1)
        _, action_text = await collect_runtime_response(response, timeout=0.5)

        assert "典型的例子" not in action_text
        assert "担心或害怕" in action_text
        assert runtime.ledger.field_states["S1-F3"].score == "1"
        assert runtime.ledger.current_field_id == "S2-F58"
        assert runtime.foreground_actions[-1]["source"] == "assessor"
        assert runtime.foreground_actions[-1]["kind"] == "ask_committed"
        assert runtime.blackboard.speculative_depth == 0
        await runtime.aclose(status="aborted")

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
            observer_mode="off",
            allow_one_step_speculation=True,
        )

        await runtime.start()
        response = await runtime.accept_text(
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


def test_scid_realtime_uncertain_negative_bridge_waits_for_late_assessor(tmp_path):
    async def run():
        runtime = SCIDDualLMRuntime(
            episode_dir=tmp_path,
            assessor=DelayedAssessor(
                AssessmentDecision(
                    field_id="S1-F3",
                    score="?",
                    confidence=0.55,
                    evidence=["我觉得应该没有吧"],
                    next_action="clarify",
                    clarification_question="你说应该没有，是完全没有，还是有些记不清？",
                    reasoning_summary="回答包含明显的不确定性。",
                ),
                delay=0.2,
            ),
            dialogue_model=RuleBasedDialogueModel(),
            prefer_deepseek=False,
            observer_mode="off",
            allow_one_step_speculation=True,
            post_initial_action_wait_seconds=0.05,
        )

        await runtime.start()
        response = await runtime.accept_text(
            "我觉得应该没有吧",
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
        action_text = await collect_stream(action_stream)
        assert "典型的例子" not in action_text
        assert "核对" in action_text or "语境" in action_text
        assert "完全没有" in action_text
        assert runtime.foreground_actions[-1]["source"] == "assessor"
        assert runtime.foreground_actions[-1]["kind"] == "clarify"
        assert runtime.ledger.current_field_id == "S1-F3"
        assert runtime.blackboard.speculative_depth == 0
        await runtime.aclose(status="aborted")

    asyncio.run(run())


def test_scid_realtime_post_initial_wait_accepts_just_late_assessor(tmp_path):
    async def run():
        runtime = SCIDDualLMRuntime(
            episode_dir=tmp_path,
            assessor=DelayedAssessor(
                AssessmentDecision(
                    field_id="S1-F3",
                    score="1",
                    confidence=0.9,
                    evidence=["对"],
                    next_action="advance",
                    clarification_question="",
                    reasoning_summary="明确短答。",
                ),
                delay=0.13,
            ),
            dialogue_model=RuleBasedDialogueModel(),
            prefer_deepseek=False,
            observer_mode="off",
            post_initial_action_wait_seconds=0.2,
        )

        await runtime.start()
        response = await runtime.accept_text("对", interaction_seq=1)

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


def test_scid_realtime_timeout_bridge_then_late_assessor_action(tmp_path):
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
            observer_mode="off",
            post_initial_action_wait_seconds=0.1,
        )

        await runtime.start()
        response = await runtime.accept_text(
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
        action_text = await collect_stream(action_stream)
        assert "典型的例子" not in action_text
        assert "核对" in action_text or "语境" in action_text
        assert "担心或害怕" in action_text
        assert runtime.foreground_actions[-1]["source"] == "assessor"
        assert runtime.foreground_actions[-1]["kind"] == "ask_committed"
        assert runtime.ledger.field_states["S1-F3"].score == "3"
        assert runtime.pending_user_buffer == ""
        assert runtime.blackboard.pending_foreground_probe is None
        record = runtime.snapshot()["runtime_turns"][0]
        assert record["bridge_source"] == "timeout_fallback"
        assert "bridge_text" in record
        assert record["action_delivery_status"] in {
            "delivery_started",
            "delivery_complete",
        }
        events = await runtime.read_events(limit=100)
        event_types = [event["event_type"] for event in events]
        assert "ForegroundBridgeRequested" in event_types
        assert "AssessmentCommitted" in event_types
        assert any(
            event["event_type"] == "ActionSelected"
            and event["payload"]["source"] == "assessor"
            for event in events
        )
        assert not any(
            event["event_type"] == "ActionSelected"
            and event["payload"]["source"] == "timeout_fallback"
            for event in events
        )

    asyncio.run(run())


def test_scid_realtime_timeout_bridge_without_backend_action_ends_safely(tmp_path):
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
                    reasoning_summary="后台结果会晚于本轮 deadline。",
                ),
                delay=1.0,
            ),
            dialogue_model=RuleBasedDialogueModel(),
            prefer_deepseek=False,
            observer_mode="off",
            post_initial_action_wait_seconds=0.05,
            runtime_policy=RuntimePolicy(turn_deadline_seconds=0.25),
        )

        await runtime.start()
        response = await runtime.accept_text(
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
        action_text = await asyncio.wait_for(collect_stream(action_stream), timeout=1.0)
        assert "典型的例子" not in action_text
        assert "？" not in action_text
        assert "核对" in action_text or "语境" in action_text
        assert runtime.foreground_actions == []
        assert runtime.ledger.field_states == {}
        record = runtime.snapshot()["runtime_turns"][0]
        assert record["bridge_source"] == "timeout_fallback"
        assert record["action_delivery_status"] == "not_selected"
        assert record["selection_reason"] in {
            "timeout_bridge_deadline_expired",
            "timeout_bridge_no_backend_action",
        }
        events = await runtime.read_events(limit=100)
        assert any(
            event["event_type"] == "ForegroundBridgeRequested" for event in events
        )
        assert not any(event["event_type"] == "AssessmentCommitted" for event in events)
        assert not any(event["event_type"] == "ActionSelected" for event in events)
        await runtime.aclose(status="aborted")

    asyncio.run(run())


def test_scid_realtime_timeout_bridge_then_late_observer_probe(tmp_path):
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
                    reasoning_summary="后台 Assessor 故意慢于 Observer probe。",
                ),
                delay=1.0,
            ),
            observer=SequencedObserver([(0.2, "ask_duration", 0.96, False)]),
            dialogue_model=RuleBasedDialogueModel(),
            prefer_deepseek=False,
            observer_mode="active",
            post_initial_action_wait_seconds=0.05,
        )

        await runtime.start()
        response = await runtime.accept_text(
            "有时候会持续一阵子，但我说不准",
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
        action_text = await collect_stream(action_stream)
        assert "典型的例子" not in action_text
        assert "核对" in action_text or "语境" in action_text
        assert "持续多久" in action_text
        assert runtime.ledger.field_states == {}
        assert runtime.pending_user_buffer
        assert runtime.blackboard.pending_foreground_probe is not None
        assert runtime.foreground_actions[-1]["source"] == "observer"
        assert runtime.foreground_actions[-1]["kind"] == "clarify"
        events = await runtime.read_events(limit=100)
        event_types = [event["event_type"] for event in events]
        assert "ForegroundBridgeRequested" in event_types
        assert "ForegroundProbeRequested" in event_types
        assert any(
            event["event_type"] == "ActionSelected"
            and event["payload"]["source"] == "observer"
            for event in events
        )
        await runtime.aclose(status="aborted")

    asyncio.run(run())


def test_scid_realtime_timeout_bridge_stops_on_new_final(tmp_path):
    async def run():
        assessor = SequencedDelayedAssessor(
            [
                (
                    1.0,
                    AssessmentDecision(
                        field_id="S1-F3",
                        score="3",
                        confidence=0.8,
                        evidence=["有时候会持续一阵子"],
                        next_action="advance",
                        clarification_question="",
                        reasoning_summary="旧回答不应在新 final 后追加播出。",
                    ),
                ),
                (
                    0.0,
                    AssessmentDecision(
                        field_id="S1-F3",
                        score="1",
                        confidence=0.9,
                        evidence=["完全没有"],
                        next_action="advance",
                        clarification_question="",
                        reasoning_summary="新回答提交。",
                    ),
                ),
            ]
        )
        runtime = SCIDDualLMRuntime(
            episode_dir=tmp_path,
            assessor=assessor,
            dialogue_model=RuleBasedDialogueModel(),
            prefer_deepseek=False,
            observer_mode="off",
            post_initial_action_wait_seconds=0.05,
        )

        await runtime.start()
        first = await runtime.accept_text(
            "有时候会持续一阵子，但我说不准",
            interaction_seq=1,
        )
        assert first.initial_stream is not None
        await collect_stream(first.initial_stream)
        assert first.action_stream_task is not None
        first_stream = await asyncio.wait_for(first.action_stream_task, timeout=0.5)
        assert first_stream is not None
        first_iterator = first_stream.__aiter__()
        bridge_chunk = await asyncio.wait_for(first_iterator.__anext__(), timeout=0.5)
        assert "核对" in bridge_chunk or "语境" in bridge_chunk

        second = await runtime.accept_text("完全没有", interaction_seq=2)
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(first_iterator.__anext__(), timeout=0.5)

        assert second.initial_stream is not None
        await collect_stream(second.initial_stream)
        assert second.action_stream_task is not None
        second_stream = await asyncio.wait_for(second.action_stream_task, timeout=0.5)
        assert second_stream is not None
        second_text = await collect_stream(second_stream)
        assert "担心或害怕" in second_text
        assert runtime.ledger.field_states["S1-F3"].score == "1"
        assert len(runtime.ledger.field_states) == 1
        first_trace = runtime.snapshot()["latency_traces"][0]
        assert first_trace["stale"] is True
        await runtime.aclose(status="aborted")

    asyncio.run(run())


def test_scid_realtime_timeout_bridge_stops_on_stop_command(tmp_path):
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
                    reasoning_summary="stop 后旧回答不能提交或播出。",
                ),
                delay=1.0,
            ),
            dialogue_model=RuleBasedDialogueModel(),
            prefer_deepseek=False,
            observer_mode="off",
            post_initial_action_wait_seconds=0.05,
        )

        await runtime.start()
        first = await runtime.accept_text(
            "有时候会持续一阵子，但我说不准",
            interaction_seq=1,
        )
        assert first.initial_stream is not None
        await collect_stream(first.initial_stream)
        assert first.action_stream_task is not None
        first_stream = await asyncio.wait_for(first.action_stream_task, timeout=0.5)
        assert first_stream is not None
        first_iterator = first_stream.__aiter__()
        bridge_chunk = await asyncio.wait_for(first_iterator.__anext__(), timeout=0.5)
        assert "核对" in bridge_chunk or "语境" in bridge_chunk

        stopped = await runtime.accept_text("结束评估", interaction_seq=2)
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(first_iterator.__anext__(), timeout=0.5)

        assert stopped.initial_stream is not None
        stopped_text = await collect_stream(stopped.initial_stream)
        assert "结束" in stopped_text or "先到这里" in stopped_text
        assert runtime.ledger.field_states == {}
        assert runtime.foreground_actions == []
        assert runtime.status == "stopped"
        assert runtime._terminal_pending_status == "stopped"
        await runtime.aclose(status="aborted")

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
            observer_mode="off",
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

        response = await runtime.accept_text(
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
        assert runtime.ledger.field_states["S2-F58"].score == "1"
        raw_user_text = runtime.ledger.field_states["S2-F58"].raw_user_text
        assert "之前已经说过没有这种担心" in raw_user_text
        assert "我再确认一次，确实没有" in raw_user_text

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
        )

        await runtime.start()
        response = await runtime.accept_text("完全没有", interaction_seq=1)

        assert response.initial_stream is not None
        initial = await collect_stream(response.initial_stream)
        assert "回答很明确" not in initial
        assert "没有" in initial or "否定" in initial
        assert "？" not in initial
        assert response.action_stream_task is not None
        action_stream = await response.action_stream_task
        assert action_stream is not None
        await collect_stream(action_stream)
        assert runtime.ledger.field_states["S1-F3"].score == "1"

        await runtime.aclose(status="aborted")

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
                "scid_observer_mode": "off",
                "scid_allow_one_step_speculation": True,
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

        await bus.publish(LLMAgentLoop(session_id="s"), wait_for_completion=True)
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
            == 2
        )

        consume_events = [
            event
            for event in bus.get_history()
            if isinstance(event, ConsumeLLMAgentGenerationRequested)
        ]
        chunks = []
        async for chunk in consume_events[-1].stream:
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
            == 2
        )

    asyncio.run(run())


def test_scid_manager_publishes_boundary_when_asr_arrives_before_loop(tmp_path):
    async def run():
        bus = EventBus(enable_history=True, max_history=80)
        published_texts = []

        async def consume(event):
            published_texts.append(await collect_stream(event.stream))

        bus.subscribe(ConsumeLLMAgentGenerationRequested, consume, priority=99)
        manager = SCIDDualLMManager(
            event_bus=bus,
            session_id="s",
            pipeline=object(),
            config={
                "data_dir": str(tmp_path),
                "scid_prefer_deepseek": False,
                "scid_observer_mode": "off",
            },
        )

        await bus.publish(
            ASRResultFinal(session_id="s", text="你需要问多少个问题？"),
            wait_for_completion=True,
        )
        await wait_for_condition(lambda: not manager._active_asr_tasks)

        assert len(published_texts) == 2
        assert "非诊断性对话" in published_texts[0]
        assert "30" in published_texts[1]

        await bus.publish(LLMAgentLoop(session_id="s"), wait_for_completion=True)
        assert len(published_texts) == 2

    asyncio.run(run())


def test_scid_manager_closing_turn_stream_cancels_action_task(tmp_path):
    async def run():
        manager = SCIDDualLMManager(
            event_bus=EventBus(),
            session_id="s",
            pipeline=object(),
            config={
                "data_dir": str(tmp_path),
                "scid_prefer_deepseek": False,
                "scid_observer_mode": "off",
            },
        )
        manager.runtime.dialogue_model = SlowInitialDialogueModel(delay=1.0)
        manager.runtime.assessor = DelayedAssessor(
            AssessmentDecision(
                field_id="S1-F3",
                score="3",
                confidence=0.9,
                evidence=["有"],
                next_action="advance",
                clarification_question="",
                reasoning_summary="慢速后台结果。",
            ),
            delay=1.0,
        )

        await manager.runtime.start()
        response = await manager.runtime.accept_text("有", interaction_seq=1)
        assert response.initial_stream is not None
        assert response.action_stream_task is not None
        stream = manager._runtime_turn_stream(
            interaction_seq=1,
            initial_stream=response.initial_stream,
            action_stream_task=response.action_stream_task,
        )
        assert await stream.__anext__() == "我听到了"

        await stream.aclose()

        assert response.action_stream_task.done()
        assert response.action_stream_task.cancelled()
        await asyncio.sleep(0)
        assert response.action_stream_task not in manager.runtime._action_tasks

    asyncio.run(run())


def test_scid_manager_recovers_action_failure_and_emits_operation_failed(tmp_path):
    async def run():
        manager = SCIDDualLMManager(
            event_bus=EventBus(),
            session_id="s",
            pipeline=object(),
            config={
                "data_dir": str(tmp_path),
                "scid_prefer_deepseek": False,
                "scid_observer_mode": "off",
            },
        )
        manager.runtime.dialogue_model = RuleBasedDialogueModel()
        await manager.runtime.start()
        response = await manager.runtime.accept_text(
            "我觉得应该没有",
            interaction_seq=1,
        )
        assert response.action_stream_task is not None
        response.action_stream_task.cancel()
        await asyncio.gather(
            response.action_stream_task,
            return_exceptions=True,
        )

        async def fail_action_stream():
            raise TimeoutError("turn deadline expired before child start")

        failed_task = asyncio.create_task(fail_action_stream())
        combined = await collect_stream(
            manager._runtime_turn_stream(
                interaction_seq=1,
                initial_stream=response.initial_stream,
                action_stream_task=failed_task,
            )
        )

        assert "明确没有" in combined
        assert manager.runtime.action_followup_was_published(1) is True
        assert manager.runtime.foreground_actions[-1]["source"] == (
            "runtime_failure_fallback"
        )
        events = await manager.runtime.read_events(limit=100)
        failure = next(
            event for event in events if event["event_type"] == "OperationFailed"
        )
        assert failure["payload"]["operation"] == "action_stream"
        assert failure["payload"]["error_type"] == "TimeoutError"
        assert failure["payload"]["recovered"] is True

        await manager.runtime.acomplete_response_delivery(1, success=True)
        assert manager.runtime.runtime_turns[-1]["action_delivery_status"] == (
            "delivery_complete"
        )
        await manager.runtime.aclose(status="aborted")

    asyncio.run(run())


def test_scid_manager_publish_failure_keeps_boundary_unstarted_and_cancels_action(
    tmp_path,
):
    async def run():
        manager = SCIDDualLMManager(
            event_bus=EventBus(),
            session_id="s",
            pipeline=object(),
            config={
                "data_dir": str(tmp_path),
                "scid_prefer_deepseek": False,
                "scid_observer_mode": "off",
            },
        )

        async def fail_publish(event, wait_for_completion=False):
            del event, wait_for_completion
            return False

        manager.event_bus.publish = fail_publish
        with pytest.raises(RuntimeError, match="product boundary"):
            await manager._ensure_started()
        assert manager._started is False

        manager.runtime.dialogue_model = SlowInitialDialogueModel(delay=1.0)
        response = await manager.runtime.accept_text("有", interaction_seq=1)
        assert response.action_stream_task is not None
        published = await manager._publish_response_stream(
            manager._runtime_turn_stream(
                interaction_seq=1,
                initial_stream=response.initial_stream,
                action_stream_task=response.action_stream_task,
            ),
            action_stream_task=response.action_stream_task,
        )

        assert published is False
        assert response.action_stream_task.cancelled()

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
            observer_mode="active",
            observer_confidence_threshold=0.9,
        )

        await runtime.start()
        first = await runtime.accept_text("有时候会这样", interaction_seq=1)
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

        second = await runtime.accept_text("每次十分钟", interaction_seq=2)
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
                    evidence=["对"],
                    next_action="advance",
                    clarification_question="",
                    reasoning_summary="明确短答。",
                ),
                delay=0.2,
            ),
            observer=SequencedObserver([("ask_next_field", 0.99, False)]),
            dialogue_model=RuleBasedDialogueModel(),
            prefer_deepseek=False,
            observer_mode="active",
            allow_one_step_speculation=True,
        )

        await runtime.start()
        response = await runtime.accept_text("对", interaction_seq=1)
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
            allow_one_step_speculation=True,
        )

        await runtime.start()
        runtime.ledger.current_field_id = "F3"
        runtime.blackboard.sync_committed_state(
            state_version=runtime.ledger.state_version,
            current_field_id="F3",
        )
        response = await runtime.accept_text("有", interaction_seq=1)

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
        )

        await runtime.start()
        response = await runtime.accept_text("有", interaction_seq=1)
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
            },
        )
        manager.runtime.assessor = DelayedAssessor(
            AssessmentDecision(
                field_id="S1-F3",
                score="1",
                confidence=0.9,
                evidence=["有"],
                next_action="advance",
                clarification_question="",
                reasoning_summary="测试用延迟回答。",
            ),
            delay=0.1,
        )

        await bus.publish(
            ASRResultFinal(session_id="s", text="有"),
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
        initial_text, action_text = await collect_runtime_response(response)
        assert "30" in initial_text
        assert action_text == ""
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
        response = await runtime.accept_text(
            "小时候父母工作很忙，我们也经常搬家", interaction_seq=1
        )
        await collect_runtime_response(response)
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
        allow_one_step_speculation=True,
        speculative_depth=0,
        repair_pending=False,
    )

    assert plan.allow_speculation is False
    assert plan.mode == "hold_for_assessor"


@pytest.mark.parametrize("candidate_source", ["fast_policy", "observer"])
@pytest.mark.parametrize(
    "blocking_condition",
    [
        "cautious",
        "safety_sensitive",
        "repair",
        "depth",
        "interaction",
        "field",
        "state_version",
    ],
)
def test_enabled_speculation_sources_obey_all_safety_and_version_gates(
    tmp_path,
    candidate_source,
    blocking_condition,
):
    async def run():
        runtime = SCIDDualLMRuntime(
            episode_dir=tmp_path,
            dialogue_model=RuleBasedDialogueModel(),
            prefer_deepseek=False,
            observer_mode="active",
            allow_one_step_speculation=True,
        )
        runtime.record_asr_final(1)
        source_field_id = runtime.ledger.current_field_id
        source_state_version = runtime.ledger.state_version
        next_field_id = runtime.ledger.preview_next_scan_field_id(source_field_id)
        assert source_field_id is not None
        assert next_field_id is not None

        interpretation = TurnInterpretation(
            interaction_seq=1,
            observer_version=1,
            based_on_state_version=source_state_version,
            field_id=source_field_id,
            dialogue_acts=["answer"],
            current_field_relevance=0.99,
            related_field_ids=[source_field_id],
            related_module_ids=[runtime.ledger.current_field.module or ""],
            contextual_memories=[],
            evidence_candidates=[],
            recommended_action="ask_next_field",
            missing_slots=[],
            needs_deep_assessment=True,
            commit_required=False,
            confidence=0.99,
        )

        def build_action():
            if candidate_source == "fast_policy":
                return runtime._build_fast_policy_action(
                    interaction_seq=1,
                    state_version=source_state_version,
                    user_text="没有",
                )
            return runtime._build_observer_foreground_action(
                interpretation=interpretation,
                interaction_seq=1,
                source_field_id=source_field_id,
                source_state_version=source_state_version,
            )

        baseline_action = build_action()
        assert baseline_action is not None

        if blocking_condition == "cautious":
            runtime.ledger.current_field.latency_mode = "cautious_module"
        elif blocking_condition == "safety_sensitive":
            runtime.ledger.current_field.safety_sensitive = True
        elif blocking_condition == "repair":
            runtime.blackboard.request_repair({"reason": "test repair"})
        elif blocking_condition == "depth":
            runtime.blackboard.begin_speculation(
                source_field_id=source_field_id,
                speculative_field_id=next_field_id,
                source_interaction_seq=1,
                based_on_state_version=source_state_version,
                question_text="candidate",
            )
            assert runtime.blackboard.speculative_depth == 1
        elif candidate_source == "observer":
            if blocking_condition == "interaction":
                interpretation.interaction_seq = 2
            elif blocking_condition == "field":
                interpretation.field_id = next_field_id
            else:
                interpretation.based_on_state_version = source_state_version + 1
        else:
            if blocking_condition == "interaction":
                baseline_action.interaction_seq = 2
            elif blocking_condition == "field":
                baseline_action.field_id = next_field_id
            else:
                baseline_action.based_on_state_version = source_state_version + 1
            broker = ForegroundActionBroker(
                interaction_seq=1,
                state_version=source_state_version,
                field_id=source_field_id,
            )
            assert await broker.submit(baseline_action) is False
            return

        assert build_action() is None

    asyncio.run(run())


def test_scid_one_step_speculation_defers_next_field_reply(tmp_path):
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
            observer_mode="active",
            allow_one_step_speculation=True,
        )

        await runtime.start()
        first = await runtime.accept_text("有", interaction_seq=1)
        _, first_action = await collect_runtime_response(first, timeout=0.2)
        assert "担心或害怕" in first_action

        second = await runtime.accept_text("没有", interaction_seq=2)
        assert runtime.blackboard.speculative_depth == 1
        assert len(runtime.ledger.turns) == 1
        _, second_action = await collect_runtime_response(second, timeout=1.0)

        assert second_action
        assert runtime.ledger.field_states["S1-F3"].score == "3"
        assert runtime.ledger.field_states["S2-F58"].score == "1"
        assert runtime.blackboard.speculative_depth == 0
        assert len(runtime.ledger.turns) == 2

    asyncio.run(run())


def test_scid_speculation_rejection_repairs_without_scoring_next_reply(tmp_path):
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
            observer_mode="active",
            allow_one_step_speculation=True,
        )

        await runtime.start()
        first = await runtime.accept_text("有", interaction_seq=1)
        await collect_runtime_response(first, timeout=0.2)

        speculative_reply = await runtime.accept_text(
            "下一题我没有这种担心", interaction_seq=2
        )
        _, repair = await collect_runtime_response(speculative_reply, timeout=1.0)

        assert "回到刚才" in repair
        assert runtime.ledger.current_field_id == "S1-F3"
        assert runtime.ledger.field_states == {}
        assert runtime.blackboard.repair_pending["announced"] is True

        clarified = await runtime.accept_text("确实有过", interaction_seq=3)
        await collect_runtime_response(clarified, timeout=1.0)
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
            observer_mode="active",
            allow_one_step_speculation=True,
        )

        await runtime.start()
        first = await runtime.accept_text("有", interaction_seq=1)
        await collect_runtime_response(first, timeout=0.2)
        assert runtime.blackboard.speculative_depth == 1

        crisis = await asyncio.wait_for(
            runtime.accept_text("我不想活了", interaction_seq=2),
            timeout=0.2,
        )
        crisis_text, crisis_action = await collect_runtime_response(crisis, timeout=0.2)
        runtime.complete_response_delivery(2, success=True)

        assert "安全" in crisis_text
        assert crisis_action == ""
        assert runtime.is_finished is True
        assert runtime.ledger.terminal_status == "crisis"
        assert runtime.blackboard.speculative_depth == 0
        assert runtime.ledger.field_states == {}

    asyncio.run(run())


def test_scid_crisis_preempts_pending_runtime_assessment(tmp_path):
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
        )

        await runtime.start()
        old = asyncio.create_task(accept_runtime_text(runtime, "有", interaction_seq=1))
        await wait_for_condition(
            lambda: runtime.blackboard.assessor_status == "running"
        )
        crisis = await asyncio.wait_for(
            accept_runtime_text(
                runtime,
                "我不想活了",
                interaction_seq=2,
            ),
            timeout=0.2,
        )

        assert "安全" in crisis.final_text
        assert runtime.ledger.terminal_status == "crisis"
        assert runtime.ledger.field_states == {}
        await old

    asyncio.run(run())


def test_scid_runtime_v3_event_pagination_and_default_redaction(tmp_path):
    async def run():
        runtime = SCIDDualLMRuntime(
            episode_dir=tmp_path,
            dialogue_model=RuleBasedDialogueModel(),
            prefer_deepseek=False,
            observer_mode="off",
        )

        await runtime.start()
        response = await runtime.accept_text(
            "你需要问多少个问题？这是不能写进事件的原话",
            interaction_seq=1,
        )
        await collect_runtime_response(response)
        await runtime.acomplete_response_delivery(1, success=True)

        first_page = await runtime.read_events(limit=3)
        second_page = await runtime.read_events(
            after_seq=first_page[-1]["event_seq"],
            limit=3,
        )
        assert first_page
        assert second_page
        assert first_page[-1]["event_seq"] < second_page[0]["event_seq"]
        assert "不能写进事件的原话" not in runtime.event_store.event_log_path.read_text(
            encoding="utf-8"
        )
        assert runtime.event_store.artifact_log_path.exists() is False
        assert runtime.snapshot()["snapshot_schema_version"] == 4
        assert runtime.snapshot()["runtime_profile"] == "realtime_v3"

        await runtime.aclose(status="aborted")

    asyncio.run(run())


def test_speculation_saga_is_versioned_and_idempotent():
    saga = SpeculationSaga()
    version = saga.begin(
        action_id="action-1",
        source_interaction_seq=1,
        source_field_id="S1-F3",
        speculative_field_id="S2-F58",
    )
    assert version == 1
    assert (
        saga.transition(
            SpeculationPhase.PROPOSED,
            reason="duplicate",
            action_id="action-1",
        )
        == version
    )
    version = saga.transition(
        SpeculationPhase.SELECTED,
        reason="selected",
        action_id="action-1",
        expected_version=version,
    )
    with pytest.raises(RuntimeError):
        saga.transition(
            SpeculationPhase.CONFIRMED,
            reason="illegal_skip",
            action_id="action-1",
            expected_version=version,
        )


def test_scid_runtime_v3_raw_opt_in_uses_separate_artifact_stream(tmp_path):
    async def run():
        runtime = SCIDDualLMRuntime(
            episode_dir=tmp_path,
            dialogue_model=RuleBasedDialogueModel(),
            prefer_deepseek=False,
            observer_mode="off",
            persist_raw_transcript=True,
        )
        raw_text = "这段原话只能进入独立 artifact stream"
        await runtime.start()
        response = await runtime.accept_text(raw_text, interaction_seq=1)
        await collect_runtime_response(response)
        await runtime.acomplete_response_delivery(1, success=True)
        await runtime.event_store.flush()

        assert raw_text not in runtime.event_store.event_log_path.read_text(
            encoding="utf-8"
        )
        assert raw_text in runtime.event_store.artifact_log_path.read_text(
            encoding="utf-8"
        )
        snapshot = runtime.snapshot()
        assert raw_text not in json.dumps(snapshot, ensure_ascii=False)
        assert snapshot["transcript"][0]["text_ref"].startswith("text_")
        await runtime.aclose(status="aborted")

    asyncio.run(run())


@pytest.mark.parametrize("writer_kind", ["event", "artifact"])
def test_scid_event_store_flush_failure_stops_all_writers(
    tmp_path,
    monkeypatch,
    writer_kind,
):
    async def run():
        store = EpisodeEventStore(
            episode_dir=tmp_path,
            episode_id=f"flush-failure-{writer_kind}",
        )
        await store.start()
        if writer_kind == "event":
            monkeypatch.setattr(
                store,
                "_fsync_event_log",
                lambda: (_ for _ in ()).throw(OSError("fsync failed")),
            )
            await store.append(
                DomainEvent(
                    event_seq=1,
                    event_type="TestEvent",
                    episode_id=store.episode_id,
                    payload={},
                )
            )
        else:
            await store.append_artifact(
                interaction_seq=1,
                role="user",
                text="sensitive test text",
            )
            monkeypatch.setattr(
                store,
                "_fsync_artifact_log",
                lambda: (_ for _ in ()).throw(OSError("fsync failed")),
            )

        with pytest.raises(OSError, match="fsync failed"):
            await asyncio.wait_for(store.flush(), timeout=1.0)
        with pytest.raises(RuntimeError):
            await asyncio.wait_for(store.close(), timeout=1.0)

        snapshot = store.snapshot()
        assert snapshot["writer_active"] is False
        assert snapshot["snapshot_writer_active"] is False
        assert snapshot["artifact_writer_active"] is False
        assert store.closed is True

    asyncio.run(run())


def test_scid_runtime_final_flush_failure_is_loud_and_leaves_no_writer(
    tmp_path,
    monkeypatch,
):
    async def run():
        runtime = SCIDDualLMRuntime(
            episode_dir=tmp_path,
            dialogue_model=RuleBasedDialogueModel(),
            prefer_deepseek=False,
            observer_mode="off",
        )
        await runtime.start()
        monkeypatch.setattr(
            runtime.event_store,
            "_fsync_event_log",
            lambda: (_ for _ in ()).throw(OSError("terminal fsync failed")),
        )

        with pytest.raises(OSError, match="terminal fsync failed"):
            await asyncio.wait_for(
                runtime.aclose(status="aborted"),
                timeout=1.0,
            )

        assert runtime.session_actor.closed is True
        assert runtime.event_store.closed is True
        assert runtime.event_store.snapshot()["writer_active"] is False
        assert runtime.event_store.snapshot()["snapshot_writer_active"] is False
        assert runtime.event_store.snapshot()["artifact_writer_active"] is False
        assert runtime.episode_path is None

    asyncio.run(run())


def test_scid_runtime_returns_busy_response_when_control_mailbox_is_saturated(
    tmp_path,
    monkeypatch,
):
    async def run():
        runtime = SCIDDualLMRuntime(
            episode_dir=tmp_path,
            dialogue_model=RuleBasedDialogueModel(),
            prefer_deepseek=False,
            observer_mode="off",
            runtime_policy=RuntimePolicy(
                retention=RetentionPolicy(control_mailbox_capacity=1)
            ),
        )
        await runtime.start()
        release_writer = asyncio.Event()
        original_append = runtime.event_store.append

        async def blocked_append(event):
            await release_writer.wait()
            await original_append(event)

        monkeypatch.setattr(runtime.event_store, "append", blocked_append)
        active = asyncio.create_task(runtime.session_actor.emit("BlockingEvent"))
        await asyncio.sleep(0)
        queued = asyncio.create_task(runtime.session_actor.emit("QueuedEvent"))
        await wait_for_condition(
            lambda: runtime.session_actor.snapshot()["control_mailbox_size"] == 1
        )

        response = await asyncio.wait_for(
            runtime.accept_text("普通回答", interaction_seq=1),
            timeout=0.2,
        )
        initial_text, action_text = await collect_runtime_response(response)
        assert "繁忙" in initial_text
        assert action_text == ""
        assert runtime.runtime_turns[-1]["route"] == "runtime_busy"

        release_writer.set()
        await asyncio.gather(active, queued)
        await runtime.acomplete_response_delivery(1, success=True)
        await runtime.aclose(status="aborted")

    asyncio.run(run())


def test_scid_runtime_v3_keeps_hot_state_bounded_for_500_turns(tmp_path):
    async def run():
        tracemalloc.start()
        runtime = SCIDDualLMRuntime(
            episode_dir=tmp_path,
            dialogue_model=RuleBasedDialogueModel(),
            prefer_deepseek=False,
            observer_mode="off",
        )
        await runtime.start()
        snapshot_size_at_100 = 0
        retained_heap_at_100 = 0

        for interaction_seq in range(1, 501):
            response = await runtime.accept_text(
                "你需要问多少个问题？",
                interaction_seq=interaction_seq,
            )
            await collect_runtime_response(response)
            await runtime.acomplete_response_delivery(
                interaction_seq,
                success=True,
            )
            if interaction_seq == 100:
                gc.collect()
                retained_heap_at_100 = tracemalloc.get_traced_memory()[0]
                snapshot_size_at_100 = len(
                    json.dumps(runtime.snapshot(), ensure_ascii=False).encode("utf-8")
                )

        policy = runtime.runtime_policy.retention
        assert len(runtime.interaction_turns) <= policy.recent_interaction_turns
        assert len(runtime.runtime_turns) <= policy.recent_runtime_turns
        assert len(runtime.latency_traces) <= policy.recent_latency_traces
        assert len(runtime.foreground_actions) <= policy.recent_foreground_actions
        assert (
            len(runtime.blackboard.observer_updates) <= policy.recent_observer_updates
        )
        assert (
            len(runtime.blackboard.partial_plan_history) <= policy.partial_plan_history
        )
        assert len(runtime.blackboard.candidate_evidence) <= policy.candidate_evidence
        assert len(runtime.blackboard.contextual_memories) <= policy.contextual_memories
        assert len(runtime.ledger.turns) <= policy.recent_ledger_turns
        assert runtime.session_actor.state.total_interactions == 500
        assert runtime.session_memory.total_interactions == 500

        snapshot_size_at_500 = len(
            json.dumps(runtime.snapshot(), ensure_ascii=False).encode("utf-8")
        )
        assert snapshot_size_at_500 - snapshot_size_at_100 <= 512 * 1024
        gc.collect()
        retained_heap_at_500 = tracemalloc.get_traced_memory()[0]
        assert retained_heap_at_500 - retained_heap_at_100 <= 20 * 1024 * 1024

        path = await runtime.aclose(status="aborted")
        assert path is not None
        final_snapshot = json.loads(path.read_text(encoding="utf-8"))
        assert final_snapshot["active_task_state"]["turn_supervisor_count"] == 0
        assert final_snapshot["active_task_state"]["assessment_task_count"] == 0
        assert final_snapshot["active_task_state"]["observer_task_count"] == 0
        assert final_snapshot["actor_state"]["actor_active"] is False
        assert final_snapshot["event_log"]["writer_active"] is False
        assert final_snapshot["event_log"]["snapshot_writer_active"] is False
        assert final_snapshot["event_log"]["artifact_writer_active"] is False

        events = []
        after_seq = 0
        while True:
            page = await runtime.read_events(after_seq=after_seq, limit=500)
            if not page:
                break
            events.extend(page)
            after_seq = page[-1]["event_seq"]
        assert [event["event_seq"] for event in events] == list(
            range(1, len(events) + 1)
        )
        tracemalloc.stop()

    asyncio.run(run())


def test_scid_runtime_v3_isolates_three_concurrent_500_turn_sessions(tmp_path):
    async def exercise_session(index):
        runtime = SCIDDualLMRuntime(
            episode_dir=tmp_path / f"session-{index}",
            dialogue_model=RuleBasedDialogueModel(),
            prefer_deepseek=False,
            observer_mode="off",
        )
        await runtime.start()
        for interaction_seq in range(1, 501):
            response = await runtime.accept_text(
                "你需要问多少个问题？",
                interaction_seq=interaction_seq,
            )
            await collect_runtime_response(response)
            await runtime.acomplete_response_delivery(
                interaction_seq,
                success=True,
            )
        path = await runtime.aclose(status="aborted")
        assert path is not None
        return runtime

    async def run():
        runtimes = await asyncio.gather(
            *(exercise_session(index) for index in range(3))
        )
        assert len({runtime.episode_id for runtime in runtimes}) == 3
        for runtime in runtimes:
            assert runtime.session_actor.state.total_interactions == 500
            assert runtime.session_memory.total_interactions == 500
            assert runtime.session_actor.snapshot()["actor_active"] is False
            assert runtime.event_store.snapshot()["writer_active"] is False
            assert not runtime._turn_supervisors

    asyncio.run(run())
