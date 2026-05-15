"""CLI entrypoint.

Default: multi-agent orchestrator (added in Phase 5).
--single: legacy single-agent loop.
"""
from __future__ import annotations
import argparse
import sys


def main() -> int:
    parser = argparse.ArgumentParser(prog="cli.py", description="LangChain agent CLI")
    parser.add_argument(
        "--single",
        action="store_true",
        help="Use the legacy single-agent loop instead of the multi-agent orchestrator.",
    )
    parser.add_argument(
        "--output-format",
        choices=("text",),
        default="text",
        help="Reserved for output mode parity (legacy only).",
    )
    sub = parser.add_subparsers(dest="command")
    sub_prompt = sub.add_parser("prompt", help="Run one prompt non-interactively")
    sub_prompt.add_argument("prompt", nargs="+")

    args = parser.parse_args()

    # Phase 2: only --single is fully wired. Default falls back to legacy with a notice.
    # Phase 5 will replace this fallback with `orchestrator.main`.
    if not args.single:
        print(
            "[cli] multi-agent orchestrator not yet wired in this build; "
            "falling back to legacy single-agent loop.",
            file=sys.stderr,
        )

    from legacy.single_agent_loop import run_repl, run_prompt
    if args.command == "prompt":
        return run_prompt(" ".join(args.prompt))
    return run_repl()


if __name__ == "__main__":
    sys.exit(main())
