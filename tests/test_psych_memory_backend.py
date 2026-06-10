from pathlib import Path

from xtalk.psych_sop.memory_backend import LocalJsonMemoryBackend


def test_local_json_memory_add_search_reset_export(tmp_path: Path):
    memory_path = tmp_path / "memory.json"
    export_path = tmp_path / "export.json"
    memory = LocalJsonMemoryBackend(path=memory_path)

    memory.add_note("用户正在测试 psychology SOP demo", scope="dialogue_memory")
    results = memory.search("psychology SOP", scope="dialogue_memory")
    assert len(results) == 1
    assert "testing" not in results[0]["content"]

    memory.export(export_path)
    assert export_path.exists()

    memory.reset()
    assert memory.search("psychology SOP", scope="dialogue_memory") == []
