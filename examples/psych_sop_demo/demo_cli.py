#!/usr/bin/env python
"""Text-only Psychology SOP-Agent demo.

Run from the xtalk repository root:

    python examples/psych_sop_demo/demo_cli.py --scale GAD-7 --reset-memory
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from xtalk.psych_sop.counseling_agent import CounselingAgent
from xtalk.psych_sop.episode_logger import EpisodeLogger
from xtalk.psych_sop.evolution_summarizer import EvolutionSummarizer
from xtalk.psych_sop.memory_backend import DEBUG_USER_ID, create_memory_backend
from xtalk.psych_sop.safety_guard import SafetyGuard
from xtalk.psych_sop.scale_engine import ScaleEngine
from xtalk.psych_sop.sop_navigator import SOPNavigator


SUPPORTED_RUNNABLE_SCALES = {"GAD-7", "PHQ-9"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a text-only psychology SOP demo.")
    parser.add_argument("--scale", default="GAD-7", help="GAD-7 or PHQ-9")
    parser.add_argument(
        "--reset-memory", action="store_true", help="Clear local demo memory"
    )
    parser.add_argument(
        "--experiment-id",
        default="psych_sop_demo",
        help="Experiment id written to episode logs and memory metadata",
    )
    parser.add_argument(
        "--no-mem0",
        action="store_true",
        help="Force LocalJsonMemoryBackend even when MEM0_API_KEY exists",
    )
    return parser.parse_args()


def normalize_scale_id(value: str) -> str:
    text = value.strip().upper().replace("_", "-")
    aliases = {"GAD7": "GAD-7", "PHQ9": "PHQ-9", "SCL90": "SCL-90"}
    return aliases.get(text, text)


def select_scale(user_text: str, default_scale: str) -> str | None:
    text = user_text.strip()
    if not text:
        return default_scale
    normalized = normalize_scale_id(text)
    if normalized in SUPPORTED_RUNNABLE_SCALES:
        return normalized
    if "焦虑" in text:
        return "GAD-7"
    if "抑郁" in text or "情绪" in text or "低落" in text:
        return "PHQ-9"
    return None


def is_skip(text: str) -> bool:
    return text.strip().lower() in {"跳过", "略过", "skip"}


def is_explain(text: str) -> bool:
    lowered = text.strip().lower()
    return lowered in {"解释", "说明", "什么意思", "help"} or "解释" in lowered


def build_metadata(
    *,
    scope: str,
    scale_id: str,
    experiment_id: str,
    sop_version: str,
    prompt_version: str,
    result: str | None = None,
    failure_type: str | None = None,
) -> dict[str, Any]:
    return {
        "scope": scope,
        "task": "psych_sop_scale_demo",
        "scale_id": scale_id,
        "experiment_id": experiment_id,
        "sop_version": sop_version,
        "prompt_version": prompt_version,
        "result": result,
        "failure_type": failure_type,
    }


def main() -> int:
    args = parse_args()
    default_scale = normalize_scale_id(args.scale)
    if default_scale not in SUPPORTED_RUNNABLE_SCALES:
        print("当前 text-only demo 只支持 GAD-7 和 PHQ-9 跑完整流程。")
        return 2

    navigator = SOPNavigator.from_yaml()
    safety_guard = SafetyGuard()
    agent = CounselingAgent()
    memory = create_memory_backend(prefer_mem0=not args.no_mem0)
    if args.reset_memory:
        memory.reset()

    selected_scale = default_scale
    engine = ScaleEngine()
    scale = engine.load_scale(selected_scale)
    logger = EpisodeLogger(
        user_id=DEBUG_USER_ID,
        task="psych_sop_scale_demo",
        scale_id=selected_scale,
        sop_version=navigator.sop_spec.sop_id,
        prompt_version=agent.prompt_version,
        experiment_id=args.experiment_id,
    )

    score: int | None = None
    interpretation: dict[str, Any] | None = None
    status = "failed"
    failure_type: str | None = None
    last_assistant = ""

    try:
        while True:
            node = navigator.current_node()
            memory_context = memory.search(
                query=f"{selected_scale} {node.id}",
                scope="dialogue_memory",
                top_k=3,
            )
            assistant_text = agent.render(
                node_id=node.id,
                action=(node.allowed_actions[0] if node.allowed_actions else "noop"),
                scale_title=scale.title,
                current_question=(
                    engine.get_current_question()
                    if engine.state
                    and engine.state.status == "in_progress"
                    and node.id in {"SCALE_LOOP", "CLARIFY_ITEM"}
                    else None
                ),
                options=engine.get_options() if engine.scale else [],
                progress=(
                    engine.get_progress()
                    if engine.state and engine.state.status == "in_progress"
                    else None
                ),
                score=score,
                interpretation=interpretation,
                crisis_response=navigator.sop_spec.crisis_response,
                memory_context=memory_context,
                selected_scale=selected_scale,
            )
            print(f"\nAssistant: {assistant_text}")
            last_assistant = assistant_text

            if node.id in {"SUPPORTIVE_CLOSE", "CRISIS_RESPONSE", "ABORTED"}:
                status = {
                    "SUPPORTIVE_CLOSE": "completed",
                    "CRISIS_RESPONSE": "crisis",
                    "ABORTED": "aborted",
                }[node.id]
                break

            user_text = input("User: ").strip()
            safety = safety_guard.classify(user_text)
            if safety.risk_level != "none":
                logger.add_safety_event(
                    {
                        "risk_level": safety.risk_level,
                        "risk_type": safety.risk_type,
                        "matched_signals": safety.matched_signals,
                    }
                )
            if safety.should_interrupt_sop:
                logger.add_turn(
                    node_id=node.id,
                    action="safety_interrupt",
                    assistant_text=assistant_text,
                    user_text=user_text,
                )
                navigator.step(user_text, {"safety_interrupt": True})
                failure_type = "safety_interrupt"
                continue

            context: dict[str, Any] = {}
            if node.id == "SCALE_SELECTION":
                maybe_scale = select_scale(user_text, default_scale)
                if maybe_scale:
                    selected_scale = maybe_scale
                    scale = engine.load_scale(selected_scale)
                    logger.episode["scale_id"] = selected_scale
                    context["selected_scale"] = selected_scale
                else:
                    print("Assistant: 目前请在 GAD-7 和 PHQ-9 中选择一个。")
                    logger.add_turn(
                        node_id=node.id,
                        action="select_scale_retry",
                        assistant_text=assistant_text,
                        user_text=user_text,
                    )
                    continue

            if node.id == "RISK_CHECK":
                context["high_risk"] = False

            if node.id == "SCALE_LOOP":
                if is_explain(user_text):
                    context["needs_clarification"] = True
                    logger.increment_clarification()
                elif is_skip(user_text):
                    assert engine.state is not None
                    engine.skip_question(engine.state.current_index)
                    context["user_skips"] = True
                else:
                    option_id, confidence, reason = engine.parse_answer(user_text)
                    if option_id is None or confidence < 0.6:
                        print(
                            "Assistant: 我还不能确定你的选择。请回复 0、1、2、3，"
                            "或输入“解释”“跳过”“退出”。"
                        )
                        logger.add_turn(
                            node_id=node.id,
                            action=f"clarify_answer:{reason}",
                            assistant_text=assistant_text,
                            user_text=user_text,
                        )
                        logger.increment_clarification()
                        continue
                    assert engine.state is not None
                    engine.record_answer(
                        engine.state.current_index,
                        option_id,
                        user_text,
                        confidence=confidence,
                    )
                    context["answer_recorded"] = True

            next_action = navigator.step(user_text, context)
            logger.add_turn(
                node_id=node.id,
                action=next_action.action,
                assistant_text=assistant_text,
                user_text=user_text,
            )
            memory.add_dialogue_turn(
                user_text,
                assistant_text,
                metadata=build_metadata(
                    scope="dialogue_memory",
                    scale_id=selected_scale,
                    experiment_id=args.experiment_id,
                    sop_version=navigator.sop_spec.sop_id,
                    prompt_version=agent.prompt_version,
                ),
            )

            if next_action.node_id == "RISK_CHECK" and engine.state is None:
                scale = engine.load_scale(selected_scale)

            if next_action.node_id == "SCALE_LOOP" and engine.state is None:
                scale = engine.load_scale(selected_scale)
                engine.start_scale(selected_scale)

            if next_action.node_id == "RECORD_ANSWER":
                has_next = engine.has_next_question()
                if has_next:
                    engine.next_question()
                navigator.step(user_text, {"has_next_question": has_next})

            if navigator.current_node().id == "COMPUTE_SCORE":
                score = engine.compute_score()
                interpretation = engine.get_score_interpretation(score)
                navigator.step(user_text, {"score_computed": True})
                memory.add_note(
                    (
                        f"{selected_scale} 最近一次量表结果：总分 {score}，"
                        f"解释：{interpretation.get('label')}。"
                    ),
                    scope="scale_state_summary",
                    metadata=build_metadata(
                        scope="scale_state_summary",
                        scale_id=selected_scale,
                        experiment_id=args.experiment_id,
                        sop_version=navigator.sop_spec.sop_id,
                        prompt_version=agent.prompt_version,
                        result="completed",
                    ),
                )

    except KeyboardInterrupt:
        print("\nAssistant: 已收到中断，我们先停在这里。")
        status = "aborted"
        failure_type = "keyboard_interrupt"
        if engine.state and engine.state.status == "in_progress":
            engine.abort_scale(failure_type)
    finally:
        answers = engine.state.answers if engine.state else {}
        skipped = engine.state.skipped if engine.state else []
        if status == "failed" and failure_type is None:
            failure_type = "unexpected_exit"
        logger.set_result(
            status=status,
            answers=answers,
            skipped_questions=skipped,
            score=score,
            interpretation=interpretation,
            failure_type=failure_type,
            dropout=status in {"aborted", "failed"},
        )
        if last_assistant:
            logger.add_note(f"last_assistant={last_assistant[:200]}")
        episode_path = logger.save()
        summary = EvolutionSummarizer().summarize(logger.episode)
        memory.add_note(
            summary,
            scope="evolution_memory",
            metadata=build_metadata(
                scope="evolution_memory",
                scale_id=selected_scale,
                experiment_id=args.experiment_id,
                sop_version=navigator.sop_spec.sop_id,
                prompt_version=agent.prompt_version,
                result=status,
                failure_type=failure_type,
            ),
        )
        print(f"\nEpisode log: {episode_path}")
        print(f"Evolution summary: {summary}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
