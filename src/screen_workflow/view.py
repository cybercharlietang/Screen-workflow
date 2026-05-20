"""View-only entry point: viz server + auto-rerender, no capture daemon.

Thin wrapper around screen_workflow.live with --no-daemon forced on.
Useful when you've finished capturing and just want to browse + re-label
without further screen activity being recorded.
"""

from __future__ import annotations

import sys

from screen_workflow.live import main as live_main


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    return live_main(["--no-daemon", *argv])


if __name__ == "__main__":
    sys.exit(main())
