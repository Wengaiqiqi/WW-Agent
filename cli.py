"""CLI entrypoint.

Boots the multi-agent orchestrator (``orchestrator.main``).
"""
from __future__ import annotations
import argparse
import sys


def _force_utf8_when_piped() -> None:
    """Emit UTF-8 on stdout/stderr when the output is redirected or captured.

    On a non-English Windows host the default stdio encoding is the locale
    code page (e.g. cp936/GBK). When this process's output is piped — a parent
    ``subprocess.run(..., encoding="utf-8")``, a gateway capturing the turn, or
    a plain ``> out.txt`` redirect — the consumer expects UTF-8, but our Chinese
    replies would go out as GBK bytes and fail to decode (0xa1/0xb9 …).

    We reconfigure to UTF-8 ONLY when the stream is not a TTY: an interactive
    console still decodes per its active code page, so forcing UTF-8 there could
    instead cause on-screen mojibake on a legacy cmd.exe. Piped output has no
    such console in the loop, so UTF-8 is unambiguously correct.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            if not stream.isatty():
                stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError, OSError):
            pass


def main() -> int:
    _force_utf8_when_piped()
    parser = argparse.ArgumentParser(prog="cli.py", description="LangChain agent CLI")
    sub = parser.add_subparsers(dest="command")
    sub_prompt = sub.add_parser("prompt", help="Run one prompt non-interactively")
    sub_prompt.add_argument("prompt", nargs="+")

    args = parser.parse_args()

    from orchestrator.main import main as orch_main
    prompt = " ".join(args.prompt) if args.command == "prompt" else None
    return orch_main(prompt=prompt)


if __name__ == "__main__":
    sys.exit(main())
