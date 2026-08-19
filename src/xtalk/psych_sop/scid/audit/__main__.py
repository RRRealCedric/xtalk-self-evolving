"""Command line renderer for completed SCID episode audits."""

from __future__ import annotations

import argparse

from .projector import write_audit_report


def main() -> None:
    parser = argparse.ArgumentParser(description="Render a redacted SCID Gate 1 audit")
    parser.add_argument("episode_id")
    parser.add_argument("--episode-dir", default="data/psych_sop_demo/episodes")
    parser.add_argument("--output-dir")
    args = parser.parse_args()
    print(
        write_audit_report(
            args.episode_dir, args.episode_id, output_dir=args.output_dir
        )
    )


if __name__ == "__main__":
    main()
