"""commit-msg hook that rejects AI attribution (scripts/git/check_commit_msg.py)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "git" / "check_commit_msg.py"
spec = importlib.util.spec_from_file_location("check_commit_msg", SCRIPT)
hook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hook)


@pytest.mark.parametrize(
    "message",
    [
        "Docs: diagram\n\nCo-Authored-By: Claude <noreply@anthropic.com>\n",
        "Fix\n\nco-authored-by: Claude Opus <noreply@anthropic.com>\n",
        "Fix\n\nCo-authored-by: Someone <bot@anthropic.com>\n",
        "Fix\n\n🤖 Generated with [Claude Code](https://claude.com/claude-code)\n",
    ],
)
def test_ai_attribution_is_rejected(tmp_path, message):
    path = tmp_path / "COMMIT_EDITMSG"
    path.write_text(message, encoding="utf-8")
    assert hook.main([str(path)]) == 1


@pytest.mark.parametrize(
    "message",
    [
        "Week 7: Lake Formation tenant isolation\n\n- marts merge\n",
        "Docs: update CLAUDE.md agent rules\n",  # naming the file is fine
        "Pair work\n\nCo-Authored-By: Jane Doe <jane@example.com>\n",
        "Fix\n\n# Co-Authored-By: Claude <noreply@anthropic.com>\n",  # git comment, stripped
    ],
)
def test_other_messages_pass(tmp_path, message):
    path = tmp_path / "COMMIT_EDITMSG"
    path.write_text(message, encoding="utf-8")
    assert hook.main([str(path)]) == 0


def test_usage_error():
    assert hook.main([]) == 2
