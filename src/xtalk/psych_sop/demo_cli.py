"""Module entry point for the psychology SOP demo."""

from __future__ import annotations

from pathlib import Path
import runpy


def main() -> int:
    script = (
        Path(__file__).resolve().parents[3]
        / "examples"
        / "psych_sop_demo"
        / "demo_cli.py"
    )
    runpy.run_path(str(script), run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
