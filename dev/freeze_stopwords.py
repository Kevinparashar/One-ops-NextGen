"""One-shot build-time script — freezes NLTK's English stopwords into a
checked-in Python module so runtime has zero dependency on nltk.

Pattern (locked 2026-05-29):
  • Dev / CI runs this script to refresh the snapshot
  • Snapshot lives at src/oneops/use_cases/uc05_triage/tools/stopwords_en.py
  • Runtime imports the frozen STOPWORDS_EN frozenset directly — no nltk
    package needed in production, no on-the-fly download, no I/O

Run when:
  • Adding the project for the first time (snapshot doesn't exist yet)
  • You want to refresh against a newer NLTK corpus release
  • You change the language / add a second language

Usage:
  .venv/bin/python dev/freeze_stopwords.py
  .venv/bin/python dev/freeze_stopwords.py --lang english --out <path>
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from textwrap import dedent

_DEFAULT_OUT = (
    Path(__file__).resolve().parent.parent
    / "src" / "oneops" / "use_cases" / "uc05_triage"
    / "tools" / "stopwords_en.py"
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lang", default="english")
    parser.add_argument("--out", type=Path, default=_DEFAULT_OUT)
    args = parser.parse_args()

    try:
        import nltk
    except ImportError:
        print("ERROR: nltk not installed in this environment.", file=sys.stderr)
        print("       Install with: pip install nltk", file=sys.stderr)
        return 1

    nltk.download("stopwords", quiet=True)
    from nltk.corpus import stopwords

    words = sorted(set(stopwords.words(args.lang)))
    if not words:
        print(f"ERROR: no stopwords returned for lang={args.lang!r}", file=sys.stderr)
        return 2

    nltk_version = getattr(nltk, "__version__", "unknown")
    header = dedent(f'''\
        """Frozen English stopwords — auto-generated, do not hand-edit.

        Source : NLTK {nltk_version} `stopwords.words({args.lang!r})`
        Words  : {len(words)}
        Refresh: .venv/bin/python dev/freeze_stopwords.py

        Locked pattern (2026-05-29): build-time snapshot, runtime has zero
        dependency on the nltk package. UC-5 isolation respected — this
        file lives inside the UC-5 tools folder.
        """
        from __future__ import annotations

        STOPWORDS_EN: frozenset[str] = frozenset({{
        ''')
    body_lines = []
    line: list[str] = []
    for w in words:
        item = f'"{w}", '
        if sum(len(s) for s in line) + len(item) > 72:
            body_lines.append("    " + "".join(line))
            line = []
        line.append(item)
    if line:
        body_lines.append("    " + "".join(line))
    footer = "})\n"

    out_text = header + "\n".join(body_lines) + "\n" + footer
    args.out.write_text(out_text)
    print(f"✓ wrote {len(words)} stopwords to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
