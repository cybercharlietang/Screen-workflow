"""CLI: ``python -m screen_workflow.viz --root <dir> [--session <id>] [--out <dir>]``."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from screen_workflow.storage.db import Store
from screen_workflow.viz.report import render


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="screen_workflow.viz")
    p.add_argument("--root", default="./local_data", help="storage root dir")
    p.add_argument("--session", default=None, help="filter to one session_id")
    p.add_argument("--out", default="./viz_output", help="output dir for index.html")
    args = p.parse_args(argv)

    store = Store(Path(args.root))
    out_path = render(store, Path(args.out), session_id=args.session)
    store.close()
    print(f"wrote {out_path}")
    print(f"open in browser: file://{out_path.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
