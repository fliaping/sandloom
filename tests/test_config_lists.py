"""How list-valued settings are read from the environment.

pydantic-settings decodes a `list[str]` field from the environment with
`json.loads` before any validator runs. That made the comma form — the only
form an operator would naturally write, and the one the validator was written
to accept — fail with `SettingsError: error parsing value for field ...`, and
the service refused to start. These tests pin both forms so neither regresses.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from agent_sandbox.config import Settings


def _toolchains(monkeypatch: pytest.MonkeyPatch, value: str) -> list[str]:
    monkeypatch.setenv("SANDBOX_TOOLCHAINS", value)
    return Settings().toolchains


def test_a_comma_separated_list_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """The form the documentation and every shell example use."""
    assert _toolchains(monkeypatch, "python,node,go,rust,java") == [
        "python",
        "node",
        "go",
        "rust",
        "java",
    ]


def test_a_single_value_is_a_list_of_one(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _toolchains(monkeypatch, "rust") == ["rust"]


def test_surrounding_whitespace_is_trimmed(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _toolchains(monkeypatch, " python , node ") == ["python", "node"]


def test_a_json_array_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    """The form that worked before must keep working.

    Splitting `["python","go"]` on its commas produces `'["python"'`, which
    would surface as a baffling "unknown toolchain" error for a configuration
    that used to start.
    """
    assert _toolchains(monkeypatch, '["python","go"]') == ["python", "go"]


def test_an_empty_value_is_refused_rather_than_disabling_everything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blank line must not silently leave every sandbox without a toolchain.

    Falling back to the default would be a guess about intent; failing names
    the field, which is the useful answer for a template that left a value
    blank by accident.
    """
    monkeypatch.setenv("SANDBOX_TOOLCHAINS", "")
    with pytest.raises(ValueError, match="at least one toolchain"):
        Settings()


def test_an_unknown_toolchain_is_rejected_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reported against the field, not as an opaque parse failure."""
    monkeypatch.setenv("SANDBOX_TOOLCHAINS", "cobol")
    with pytest.raises(ValueError, match="cobol"):
        Settings()


@pytest.mark.parametrize(
    ("variable", "field"),
    [
        ("SANDBOX_HTTP_PROXIES", "http_proxies"),
        ("SANDBOX_READONLY_MOUNTS", "readonly_mounts"),
        ("SANDBOX_MCP_ALLOWED_HOSTS", "mcp_allowed_hosts"),
    ],
)
def test_every_list_setting_accepts_the_comma_form(
    monkeypatch: pytest.MonkeyPatch, variable: str, field: str
) -> None:
    """Not just toolchains: the same decoding applies to all of them."""
    monkeypatch.setenv(variable, "alpha,beta")
    assert getattr(Settings(), field) == ["alpha", "beta"]


def _referenced_names() -> dict[str, set[str]]:
    """Every field name as it appears outside its own declaration, file by file."""

    root = Path(__file__).resolve().parents[1]
    sources = {
        path: path.read_text(encoding="utf-8")
        for path in list((root / "src").rglob("*.py")) + list((root / "runtime" / "src").rglob("*.py"))
    }
    referenced: dict[str, set[str]] = {}
    for field in Settings.model_fields:
        hits = set()
        for path, text in sources.items():
            for line in text.splitlines():
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if re.search(rf"\b{field}\b", line) and not re.match(rf"^    {field}: ", line):
                    hits.add(str(path.relative_to(root)))
        referenced[field] = hits
    return referenced


def test_every_setting_is_read_by_something() -> None:
    """A setting nothing reads is a knob that lies to whoever sets it.

    `Settings` ignores unknown variables and accepts every field it declares, so
    an inert one fails in the worst way available: the operator sets it, the
    service starts happily, and nothing changes. Two shipped that way —
    `SANDBOX_TELEMETRY_SINK` and `SANDBOX_MYSQL_CONNECT_TIMEOUT_SECONDS` — and
    neither was visible to any test, because a field with no reader also has no
    behaviour to assert on.
    """

    unread = sorted(field for field, hits in _referenced_names().items() if not hits)

    assert not unread, (
        f"{unread} are declared as settings but nothing reads them. Either wire "
        "them up or remove them: a knob that does nothing is discovered by the "
        "operator who set it and waited for an effect"
    )
