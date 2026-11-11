#!/usr/bin/env python
"""Rewrite the generated tables inside ``docs/TALKGROUPS.md`` from ``app.vocabulary``.

Run it after adding a category, a service tag or an agency kind::

    cd backend && .venv/bin/python tools/render_vocabulary_docs.py

``tests/test_vocabulary.py`` fails until you do, which is the point: the docs page and the
Help page and the validators are the same three sentences, or they are a bug.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.vocabulary import DOC_BLOCKS, fenced_block  # noqa: E402

DOCS = Path(__file__).resolve().parents[2] / "docs" / "TALKGROUPS.md"


def main() -> int:
    text = DOCS.read_text()
    for name in DOC_BLOCKS:
        pattern = re.compile(
            rf"<!-- generated:{re.escape(name)} -->.*?<!-- /generated:{re.escape(name)} -->",
            re.DOTALL,
        )
        if pattern.search(text) is None:
            print(f"{DOCS}: no <!-- generated:{name} --> block", file=sys.stderr)
            return 1
        text = pattern.sub(lambda _m, n=name: fenced_block(n), text, count=1)
    DOCS.write_text(text)
    print(f"wrote {DOCS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
