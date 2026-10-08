from __future__ import annotations

import pytest

from agent_sandbox_runtime import IsolationLevel, negotiate_isolation


def test_auto_selects_strongest_successful_level() -> None:
    supported = {IsolationLevel.BASIC, IsolationLevel.STANDARD}

    selection = negotiate_isolation(
        "auto",
        lambda level: (level in supported, "blocked by outer runtime"),
    )

    assert selection.selected is IsolationLevel.STANDARD
    assert selection.max_supported is IsolationLevel.STANDARD
    assert selection.as_dict()["supported_levels"] == ["basic", "standard"]


def test_explicit_level_never_silently_downgrades() -> None:
    with pytest.raises(RuntimeError, match="strict"):
        negotiate_isolation(
            "strict",
            lambda level: (level is not IsolationLevel.STRICT, "CLONE_NEWCGROUP denied"),
        )


def test_parsing_a_level_object_returns_it_unchanged() -> None:
    """Configuration arrives as text, but the API also takes the enum."""

    assert IsolationLevel.parse(IsolationLevel.STRICT) is IsolationLevel.STRICT


def test_an_unknown_level_name_is_refused_by_value() -> None:
    """A typo in SANDBOX_ISOLATION_LEVEL must not be read as the default."""

    with pytest.raises(ValueError, match="unknown isolation level"):
        IsolationLevel.parse("strick")
