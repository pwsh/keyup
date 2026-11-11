"""Regenerate the checked-in ``deploy/edge/Caddyfile.*`` artifacts.

``cd backend && .venv/bin/python -m app.edge``.  ``tests/test_edge_render.py`` fails when
the files on disk differ from what :mod:`app.edge.render` produces, so this is how a change
to the template reaches the container.
"""

from __future__ import annotations

import sys
from pathlib import Path

from app.edge.render import write_artifacts


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # Default: the repository root, which is the parent of the `backend/` this package
    # lives in.  An explicit argument is for a test writing into a temporary tree.
    root = Path(argv[0]) if argv else Path(__file__).resolve().parents[3]
    for path in write_artifacts(root):
        print(path)
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
