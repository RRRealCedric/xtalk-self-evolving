#!/usr/bin/env python
"""Text-only Psychology SOP-Agent demo.

Run from the xtalk repository root:

    python examples/psych_sop_demo/demo_cli.py --scale GAD-7 --reset-memory
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from xtalk.psych_sop.runtime import PsychSOPRuntime  # noqa: E402


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


def main() -> int:
    args = parse_args()
    try:
        runtime = PsychSOPRuntime(
            scale_id=args.scale,
            experiment_id=args.experiment_id,
            prefer_mem0=not args.no_mem0,
            reset_memory=args.reset_memory,
        )
    except ValueError as exc:
        print(str(exc))
        return 2

    print(f"\nAssistant: {runtime.start()}")
    try:
        while not runtime.is_finished:
            user_text = input("User: ").strip()
            assistant_text = runtime.accept_text(user_text)
            print(f"\nAssistant: {assistant_text}")
    except (KeyboardInterrupt, EOFError):
        print("\nAssistant: 已收到中断，我们先停在这里。")
        runtime.finish(status="aborted", failure_type="keyboard_interrupt")

    if runtime.episode_path:
        print(f"\nEpisode log: {runtime.episode_path}")
    if runtime.evolution_summary:
        print(f"Evolution summary: {runtime.evolution_summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
