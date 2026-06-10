import asyncio

from xtalk.serving.event_bus import EventBus
from xtalk.serving.events import (
    ASRResultFinal,
    ConsumeLLMAgentGenerationRequested,
    LLMAgentLoop,
)
from xtalk.serving.modules.psych_sop_manager import PsychSOPManager


def test_psych_sop_manager_publishes_start_stream(tmp_path):
    async def run():
        bus = EventBus(enable_history=True, max_history=20)
        PsychSOPManager(
            event_bus=bus,
            session_id="s",
            pipeline=object(),
            config={
                "data_dir": str(tmp_path),
                "psych_sop_prefer_mem0": False,
                "psych_sop_reset_memory": True,
            },
        )

        await bus.publish(LLMAgentLoop(session_id="s"), wait_for_completion=True)
        events = bus.get_history()
        consume_events = [
            event
            for event in events
            if isinstance(event, ConsumeLLMAgentGenerationRequested)
        ]
        assert consume_events

        chunks = []
        async for chunk in consume_events[-1].stream:
            chunks.append(chunk)
        assert "demo" in "".join(chunks)

    asyncio.run(run())


def test_psych_sop_manager_accepts_asr_final(tmp_path):
    async def run():
        bus = EventBus(enable_history=True, max_history=20)
        PsychSOPManager(
            event_bus=bus,
            session_id="s",
            pipeline=object(),
            config={
                "data_dir": str(tmp_path),
                "psych_sop_prefer_mem0": False,
                "psych_sop_reset_memory": True,
            },
        )

        await bus.publish(
            ASRResultFinal(session_id="s", text="同意"),
            wait_for_completion=True,
        )
        consume_events = [
            event
            for event in bus.get_history()
            if isinstance(event, ConsumeLLMAgentGenerationRequested)
        ]
        assert consume_events

        chunks = []
        async for chunk in consume_events[-1].stream:
            chunks.append(chunk)
        assert "结构化心理量表" in "".join(chunks)

    asyncio.run(run())
