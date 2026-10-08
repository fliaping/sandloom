"""README translations share navigation, assets, and runnable examples."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGLISH = ROOT / "README.md"
CHINESE = ROOT / "README.zh-CN.md"
_FENCE = re.compile(r"^```([^\n]*)\n(.*?)^```$", re.MULTILINE | re.DOTALL)
_IMAGE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")


def _examples(document: str) -> list[tuple[str, str]]:
    """Allow translated comments and diagrams without altering sample commands."""
    examples = []
    for language, body in _FENCE.findall(document):
        if language == "text":
            continue
        lines = []
        for line in body.splitlines():
            if line.lstrip().startswith(("#", "//")):
                continue
            line = re.sub(r"\s+#.*$", "", line).rstrip()
            if line:
                lines.append(line)
        examples.append((language, "\n".join(lines)))
    return examples


def test_readmes_have_reciprocal_language_links() -> None:
    english = ENGLISH.read_text(encoding="utf-8")
    chinese = CHINESE.read_text(encoding="utf-8")
    assert "[简体中文](README.zh-CN.md)" in english.split("##", 1)[0]
    assert "[English](README.md)" in chinese.split("##", 1)[0]


def test_chinese_readme_preserves_examples_and_assets() -> None:
    english = ENGLISH.read_text(encoding="utf-8")
    chinese = CHINESE.read_text(encoding="utf-8")
    assert _examples(chinese) == _examples(english)
    assert _IMAGE.findall(chinese) == _IMAGE.findall(english)
