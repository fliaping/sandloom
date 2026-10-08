"""Directory operations: listing, mkdir, delete, and move.

These run against a real filesystem rather than a mocked one, because the
behavior worth proving — that a symlink is not followed out of the workspace,
that `..` is rejected, that a recursive delete is opt-in — is a property of the
path handling and the syscalls, not of a mock.
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest

from agent_sandbox.app import create_app
from agent_sandbox.backends import as_directory_operations
from agent_sandbox.config import Settings
from agent_sandbox.runtime import LocalSandbox, SandboxRuntime

AUTH = {"Authorization": "Bearer test-token"}


def _runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[SandboxRuntime, LocalSandbox]:
    # chown needs root; the tests care about path handling, not ownership.
    monkeypatch.setattr(os, "chown", lambda *_args, **_kwargs: None)
    root = tmp_path / "sb-1"
    for name in ("workspace", "home", "cache", "envs"):
        (root / name).mkdir(parents=True, exist_ok=True)
    settings = Settings(internal_token="token", local_root=tmp_path, min_free_bytes=0)
    return SandboxRuntime(settings), LocalSandbox("sb-1", 1, 20001, root)


# ── listing ──


async def test_listing_describes_each_entry_by_kind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, sandbox = _runtime(tmp_path, monkeypatch)
    (sandbox.workspace / "a.txt").write_text("hello")
    (sandbox.workspace / "sub").mkdir()

    entries, total, has_more = await runtime.list_directory(
        sandbox, "/workspace", limit=100, offset=0
    )

    assert total == 2
    assert has_more is False
    by_name = {entry["name"]: entry for entry in entries}
    assert by_name["a.txt"]["type"] == "file"
    assert by_name["a.txt"]["size_bytes"] == 5
    assert by_name["sub"]["type"] == "directory"
    # Paths come back in the caller's namespace, not the host's.
    assert by_name["a.txt"]["path"] == "/workspace/a.txt"


async def test_a_symlink_is_reported_as_a_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not as the file it points at, and not by following it off the workspace."""
    runtime, sandbox = _runtime(tmp_path, monkeypatch)
    outside = tmp_path / "secret.txt"
    outside.write_text("host secret")
    (sandbox.workspace / "escape").symlink_to(outside)

    entries, _, _ = await runtime.list_directory(sandbox, "/workspace", limit=100, offset=0)

    entry = next(item for item in entries if item["name"] == "escape")
    assert entry["type"] == "symlink"
    # The size is the link's own, so the listing never reveals the target.
    assert entry["size_bytes"] != len("host secret")


async def test_listing_pages_without_reordering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, sandbox = _runtime(tmp_path, monkeypatch)
    for index in range(10):
        (sandbox.workspace / f"file-{index:02d}").write_text("x")

    first, total, has_more = await runtime.list_directory(sandbox, "/workspace", limit=4, offset=0)
    second, _, _ = await runtime.list_directory(sandbox, "/workspace", limit=4, offset=4)

    assert total == 10
    assert has_more is True
    assert [entry["name"] for entry in first] == [f"file-{i:02d}" for i in range(4)]
    assert [entry["name"] for entry in second] == [f"file-{i:02d}" for i in range(4, 8)]


async def test_listing_a_file_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime, sandbox = _runtime(tmp_path, monkeypatch)
    (sandbox.workspace / "a.txt").write_text("hello")

    with pytest.raises(ValueError, match="not a directory"):
        await runtime.list_directory(sandbox, "/workspace/a.txt", limit=10, offset=0)


async def test_listing_rejects_a_traversal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime, sandbox = _runtime(tmp_path, monkeypatch)

    with pytest.raises(ValueError, match=r"\.\."):
        await runtime.list_directory(sandbox, "/workspace/../..", limit=10, offset=0)


# ── mkdir ──


async def test_mkdir_creates_one_level(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime, sandbox = _runtime(tmp_path, monkeypatch)

    await runtime.make_directory(sandbox, "/workspace/new", parents=False)

    created = sandbox.workspace / "new"
    assert created.is_dir()
    # Private to the sandbox UID, like every other directory the API creates.
    assert oct(created.stat().st_mode)[-3:] == "700"


async def test_mkdir_without_parents_refuses_a_missing_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, sandbox = _runtime(tmp_path, monkeypatch)

    with pytest.raises(ValueError, match="parent directory does not exist"):
        await runtime.make_directory(sandbox, "/workspace/a/b/c", parents=False)


async def test_mkdir_with_parents_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Provisioning a tree twice must not fail the second caller."""
    runtime, sandbox = _runtime(tmp_path, monkeypatch)

    await runtime.make_directory(sandbox, "/workspace/a/b/c", parents=True)
    await runtime.make_directory(sandbox, "/workspace/a/b/c", parents=True)

    assert (sandbox.workspace / "a/b/c").is_dir()


async def test_mkdir_on_an_existing_path_without_parents_reports_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, sandbox = _runtime(tmp_path, monkeypatch)
    (sandbox.workspace / "here").mkdir()

    with pytest.raises(ValueError, match="already exists"):
        await runtime.make_directory(sandbox, "/workspace/here", parents=False)


async def test_mkdir_cannot_escape_the_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, sandbox = _runtime(tmp_path, monkeypatch)

    with pytest.raises(ValueError):
        await runtime.make_directory(sandbox, "/workspace/../../evil", parents=True)
    assert not (tmp_path / "evil").exists()


# ── delete ──


async def test_delete_removes_a_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime, sandbox = _runtime(tmp_path, monkeypatch)
    target = sandbox.workspace / "a.txt"
    target.write_text("hello")

    await runtime.delete_path(sandbox, "/workspace/a.txt", recursive=False)

    assert not target.exists()


async def test_deleting_a_populated_directory_requires_recursive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An accidental recursive delete of a workspace cannot be undone."""
    runtime, sandbox = _runtime(tmp_path, monkeypatch)
    tree = sandbox.workspace / "tree"
    tree.mkdir()
    (tree / "file.txt").write_text("keep me")

    with pytest.raises(ValueError, match="not empty"):
        await runtime.delete_path(sandbox, "/workspace/tree", recursive=False)
    assert (tree / "file.txt").exists()

    await runtime.delete_path(sandbox, "/workspace/tree", recursive=True)
    assert not tree.exists()


async def test_deleting_a_symlink_unlinks_it_and_spares_the_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Following the link would let a sandbox delete an arbitrary host file."""
    runtime, sandbox = _runtime(tmp_path, monkeypatch)
    outside = tmp_path / "precious.txt"
    outside.write_text("host data")
    link = sandbox.workspace / "escape"
    link.symlink_to(outside)

    await runtime.delete_path(sandbox, "/workspace/escape", recursive=False)

    assert not link.is_symlink()
    assert outside.exists()
    assert outside.read_text() == "host data"


async def test_deleting_a_symlinked_directory_does_not_recurse_into_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, sandbox = _runtime(tmp_path, monkeypatch)
    outside = tmp_path / "host-tree"
    outside.mkdir()
    (outside / "keep.txt").write_text("host data")
    (sandbox.workspace / "link").symlink_to(outside)

    await runtime.delete_path(sandbox, "/workspace/link", recursive=True)

    assert (outside / "keep.txt").exists()


async def test_the_workspace_root_cannot_be_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, sandbox = _runtime(tmp_path, monkeypatch)

    with pytest.raises(ValueError, match="workspace root"):
        await runtime.delete_path(sandbox, "/workspace", recursive=True)
    assert sandbox.workspace.is_dir()


async def test_deleting_a_missing_path_reports_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, sandbox = _runtime(tmp_path, monkeypatch)

    with pytest.raises(ValueError, match="does not exist"):
        await runtime.delete_path(sandbox, "/workspace/ghost", recursive=False)


# ── move ──


async def test_move_renames_within_the_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, sandbox = _runtime(tmp_path, monkeypatch)
    (sandbox.workspace / "old.txt").write_text("content")

    await runtime.move_path(sandbox, "/workspace/old.txt", "/workspace/new.txt", overwrite=False)

    assert not (sandbox.workspace / "old.txt").exists()
    assert (sandbox.workspace / "new.txt").read_text() == "content"


async def test_move_refuses_to_clobber_without_overwrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, sandbox = _runtime(tmp_path, monkeypatch)
    (sandbox.workspace / "a.txt").write_text("source")
    (sandbox.workspace / "b.txt").write_text("destination")

    with pytest.raises(ValueError, match="already exists"):
        await runtime.move_path(sandbox, "/workspace/a.txt", "/workspace/b.txt", overwrite=False)
    assert (sandbox.workspace / "b.txt").read_text() == "destination"

    await runtime.move_path(sandbox, "/workspace/a.txt", "/workspace/b.txt", overwrite=True)
    assert (sandbox.workspace / "b.txt").read_text() == "source"


async def test_move_cannot_push_a_file_out_of_the_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, sandbox = _runtime(tmp_path, monkeypatch)
    (sandbox.workspace / "a.txt").write_text("content")

    with pytest.raises(ValueError):
        await runtime.move_path(
            sandbox, "/workspace/a.txt", "/workspace/../../out.txt", overwrite=False
        )
    assert not (tmp_path.parent / "out.txt").exists()
    assert (sandbox.workspace / "a.txt").exists()


async def test_move_reports_a_missing_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, sandbox = _runtime(tmp_path, monkeypatch)

    with pytest.raises(ValueError, match="source path does not exist"):
        await runtime.move_path(sandbox, "/workspace/ghost", "/workspace/x", overwrite=False)


async def test_move_reports_a_missing_destination_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, sandbox = _runtime(tmp_path, monkeypatch)
    (sandbox.workspace / "a.txt").write_text("content")

    with pytest.raises(ValueError, match="destination parent"):
        await runtime.move_path(sandbox, "/workspace/a.txt", "/workspace/no/x", overwrite=False)


async def test_moving_a_path_onto_itself_is_a_no_op(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Locking both ends of the same path would otherwise deadlock."""
    runtime, sandbox = _runtime(tmp_path, monkeypatch)
    (sandbox.workspace / "a.txt").write_text("content")

    await runtime.move_path(sandbox, "/workspace/a.txt", "/workspace/a.txt", overwrite=False)

    assert (sandbox.workspace / "a.txt").read_text() == "content"


# ── capability narrowing ──


def test_a_backend_without_directory_operations_is_reported() -> None:
    """A plugin written against an earlier release must still load."""

    class OldBackend:
        async def read_file(self, *_: object) -> str:
            return ""

    assert as_directory_operations(OldBackend()) is None
    assert as_directory_operations(None) is None


def test_the_bundled_runtime_supports_directory_operations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, _ = _runtime(tmp_path, monkeypatch)
    assert as_directory_operations(runtime) is not None


# ── HTTP surface ──


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        internal_token="test-token",
        local_root=tmp_path / "sandboxes",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'control.db'}",
        database_auto_ddl=True,
        profile_hash="profile-a",
        advertise_host="worker.test",
    )


async def test_directory_routes_require_the_internal_token(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        listing = await client.get(
            "/api/v1/sandboxes/sb-1/files/list", params={"path": "/workspace", "generation": 1}
        )
        mkdir = await client.post(
            "/api/v1/sandboxes/sb-1/files/mkdir",
            json={"generation": 1, "path": "/workspace/x"},
        )
        delete = await client.post(
            "/api/v1/sandboxes/sb-1/files/delete",
            json={"generation": 1, "path": "/workspace/x"},
        )
        move = await client.post(
            "/api/v1/sandboxes/sb-1/files/move",
            json={"generation": 1, "source": "/workspace/a", "destination": "/workspace/b"},
        )

    assert listing.status_code == 401
    assert mkdir.status_code == 401
    assert delete.status_code == 401
    assert move.status_code == 401


async def test_the_listing_route_is_not_shadowed_by_the_read_route(tmp_path: Path) -> None:
    """`/files/list` must not be parsed as a read of a file named `list`."""
    app = create_app(_settings(tmp_path))
    routes = {getattr(route, "path", "") for route in app.routes}

    assert "/api/v1/sandboxes/{sandbox_id}/files/list" in routes
    assert "/api/v1/sandboxes/{sandbox_id}/files/mkdir" in routes
    assert "/api/v1/sandboxes/{sandbox_id}/files/delete" in routes
    assert "/api/v1/sandboxes/{sandbox_id}/files/move" in routes

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        # Authenticated, so a 401 would mean the path matched something else.
        response = await client.get(
            "/api/v1/sandboxes/sb-1/files/list",
            headers=AUTH,
            params={"path": "/workspace", "generation": 1},
        )

    assert response.status_code != 401


async def test_the_listing_page_size_is_bounded(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/api/v1/sandboxes/sb-1/files/list",
            headers=AUTH,
            params={"path": "/workspace", "generation": 1, "limit": 999_999},
        )

    assert response.status_code == 422
