"""commit-msg hook: reject AI attribution in commit messages (owner rule, 2026-11-10).

Commits in this repository are authored by the owner. A commit pushed on 2026-11-10 carried
"Co-Authored-By: Claude <noreply@anthropic.com>", and GitHub kept listing Claude as a
contributor after the commit was amended away. This hook rejects such lines before a commit is
created:

* any ``Co-Authored-By:`` trailer naming Claude or Anthropic
* the "Generated with Claude Code" footer

Usage (pre-commit, ``commit-msg`` stage): ``check_commit_msg.py <path to the message file>``.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_FORBIDDEN = (
    re.compile(r"^\s*co-authored-by:.*\b(claude|anthropic)\b", re.IGNORECASE | re.MULTILINE),
    re.compile(r"generated with \[?claude code\]?", re.IGNORECASE),
)


def violations(message: str) -> list[str]:
    # Lines starting with '#' are git comments and are stripped from the final message.
    text = "\n".join(line for line in message.splitlines() if not line.startswith("#"))
    return [m.group(0).strip() for pattern in _FORBIDDEN for m in pattern.finditer(text)]


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: check_commit_msg.py <commit message file>", file=sys.stderr)
        return 2
    found = violations(Path(args[0]).read_text(encoding="utf-8"))
    for line in found:
        print(f"commit message rejected: AI attribution is not allowed here: {line!r}", file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
