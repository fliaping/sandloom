"""Every variable the sample environment names has to be a variable something reads.

`.env.example` is the file a deployment copies and edits. A key with a typo, or
one left behind by a rename, is not an error anywhere: `Settings` is configured
with `extra="ignore"`, so the process starts happily with the operator's value
dropped on the floor, and they find out by wondering why the setting had no
effect.

Three mechanisms read these names, so a key is satisfied by any: a matching
`Settings` field, or a literal that appears in `src/` because something calls
`os.getenv` for it. The second covers the `BLOBSTORE_*` family, whose names are
shared with the tools that already use them, and `SANDBOX_ENVIRONMENT`, which
selects behaviour before settings are built. Compose interpolation covers the
host-side listener settings, which are not application settings.
"""

from __future__ import annotations

import re
from pathlib import Path

from agent_sandbox.config import Settings

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

def _example_keys() -> list[str]:
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    return re.findall(r"^([A-Z][A-Z0-9_]*)=", text, re.M)


def _source_text() -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in SRC.rglob("*.py"))


def _compose_keys(text: str) -> set[str]:
    # Ignore comments and escaped dollars: neither reads an operator's value.
    active = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    return set(re.findall(r"(?<!\$)\$\{([A-Z][A-Z0-9_]*)(?=[:?+\-}])", active))


def test_every_variable_in_the_sample_is_read_by_something() -> None:
    fields = set(Settings.model_fields)
    source = _source_text()
    compose_keys = _compose_keys((ROOT / "compose.yaml").read_text(encoding="utf-8"))

    orphans = []
    for key in _example_keys():
        if key.startswith("SANDBOX_") and key[len("SANDBOX_") :].lower() in fields:
            continue
        if f'"{key}"' in source:
            continue
        if key in compose_keys:
            continue
        orphans.append(key)

    assert not orphans, (
        f"{orphans} are set in .env.example but nothing reads them — not a "
        "Settings field, an os.getenv in src/, or a Compose interpolation. An operator would set "
        "one and see no effect, with no error to explain it"
    )


def test_compose_key_detection_requires_an_active_interpolation() -> None:
    assert _compose_keys(
        '# example: ${UNUSED}\n'
        'ports: ["${SANDBOX_BIND_ADDRESS:-127.0.0.1}:${SANDBOX_HTTP_PORT:-8080}:8080"]\n'
        'token: ${SANDBOX_INTERNAL_TOKEN:?required}\n'
        'literal: "$${NOT_INTERPOLATED}"\n'
    ) == {"SANDBOX_BIND_ADDRESS", "SANDBOX_HTTP_PORT", "SANDBOX_INTERNAL_TOKEN"}


def test_the_sample_covers_every_family_it_claims_to() -> None:
    """A sample that omits a family is not wrong, but this one documents its own
    scope: the storage family is named, so it has to stay named."""

    keys = set(_example_keys())

    assert {key for key in keys if key.startswith("BLOBSTORE_")} >= {
        "BLOBSTORE_ENDPOINT",
        "BLOBSTORE_BUCKET",
        "BLOBSTORE_ACCESS_KEY",
        "BLOBSTORE_SECRET_KEY",
    }
    # And the prefix families the settings use.
    assert {"SANDBOX_TOOLCHAINS", "SANDBOX_ISOLATION_LEVEL"} <= keys


def test_the_example_does_not_carry_a_working_token() -> None:
    """A committed token is a token every deployment shares."""

    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    token = re.search(r"^SANDBOX_INTERNAL_TOKEN=(.*)$", text, re.M)

    assert token is not None, ".env.example no longer shows where the token goes"
    assert token.group(1).strip() == "", (
        "SANDBOX_INTERNAL_TOKEN has a value in .env.example; every deployment "
        "that copies the file would share it"
    )
