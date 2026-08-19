import asyncio
import json

from xtalk.psych_sop.scid import RuleBasedDialogueModel, SCIDDualLMRuntime
from xtalk.psych_sop.scid.audit import (
    build_audit_projection,
    load_episode_events,
    write_audit_report,
)


async def _consume(stream):
    if stream is None:
        return ""
    chunks = []
    async for chunk in stream:
        chunks.append(chunk)
    return "".join(chunks)


def test_gate_one_audit_projects_redacted_model_segment_and_state_facts(tmp_path):
    async def run():
        runtime = SCIDDualLMRuntime(
            episode_dir=tmp_path,
            dialogue_model=RuleBasedDialogueModel(),
            prefer_deepseek=False,
            observer_mode="off",
        )
        await runtime.start()
        response = await runtime.accept_text("没有出现过这种情况", interaction_seq=1)
        await _consume(response.initial_stream)
        if response.action_stream_task is not None:
            await _consume(await response.action_stream_task)
        await runtime.acomplete_response_delivery(1, success=True)
        await runtime.event_store.flush()
        final_path = await runtime.aclose(status="aborted")

        events = load_episode_events(tmp_path, runtime.episode_id)
        projection = build_audit_projection(
            events,
            snapshot=json.loads(final_path.read_text(encoding="utf-8")),
        )
        event_types = {event["event_type"] for event in events}
        assert "ModelCallCompleted" in event_types
        assert "ForegroundSegmentGenerated" in event_types
        assert "ForegroundSegmentDeliveryRecorded" in event_types
        assert "StateDiffRecorded" in event_types
        assert projection["source"]["event_sequence_continuous"] is True
        assert projection["session"]["raw_transcript_persisted"] is False
        assert projection["turns"][0]["model_call_count"] >= 1
        assert projection["turns"][0]["segment_count"] >= 1
        assert projection["turns"][0]["state_diff_count"] >= 1

        audit_dir = write_audit_report(tmp_path, runtime.episode_id)
        session_report = (audit_dir / "session.md").read_text(encoding="utf-8")
        assert "没有出现过这种情况" not in session_report
        assert (audit_dir / "turns" / "0001.md").is_file()

    asyncio.run(run())
