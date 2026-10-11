"""HTTP protocol models."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, Field, field_validator


def _as_utc(value: datetime) -> datetime:
    """Give a stored timestamp its lost offset back.

    The database stores UTC and hands back naive `datetime`s, so a response
    serialized one as `2026-09-30T17:43:53` — a string with no offset, which is
    ambiguous on the wire. The ECMAScript date parser resolves that ambiguity by
    assuming *local* time, so a browser eight hours ahead of UTC read a worker
    that had started a minute earlier as eight hours old, and every consumer
    computing a duration from this API inherited its own offset as an error.
    """

    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


UtcDatetime = Annotated[datetime, AfterValidator(_as_utc)]


class ResolveRequest(BaseModel):
    sandbox_id: str = Field(min_length=3, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    workspace_scope_id: str = Field(min_length=1, max_length=191)
    profile: str = Field(default="coding-default", min_length=1, max_length=64)


class RouteResponse(BaseModel):
    sandbox_id: str
    worker_id: str
    worker_epoch: str
    worker_endpoint: str
    generation: int
    sandbox_uid: int
    status: str
    storage_mode: Literal["local", "shared"]


class LifecycleAuditEntry(BaseModel):
    generation: int
    started_at: UtcDatetime | None
    created_by: str | None
    ready_at: UtcDatetime | None
    released_at: UtcDatetime
    release_reason: str
    released_by: str
    lifetime_ms: int


class SandboxAuditResponse(BaseModel):
    sandbox_id: str
    workspace_scope_id: str
    generation: int
    status: str
    first_created_at: UtcDatetime | None
    generation_started_at: UtcDatetime | None
    generation_created_by: str | None
    ready_at: UtcDatetime | None
    last_active_at: UtcDatetime | None
    active_duration_ms: int | None
    last_released_generation: int | None
    last_released_at: UtcDatetime | None
    last_release_reason: str | None
    last_released_by: str | None
    last_lifetime_ms: int | None
    lifecycle_count: int
    total_lifetime_ms: int
    history_retention_days: int
    history_max_entries: int
    history_max_bytes: int
    history: list[LifecycleAuditEntry]


class CreateSandboxRequest(BaseModel):
    generation: int = Field(ge=1)
    sandbox_uid: int = Field(ge=1)
    profile: str = Field(min_length=1, max_length=64)
    worker_epoch: str = Field(min_length=1, max_length=64)


class ConnectSandboxRequest(BaseModel):
    """Initialize an already-resolved sandbox through the unified API entry point."""

    generation: int = Field(ge=1)


class SuspendSandboxRequest(BaseModel):
    """Release a sandbox's capacity slot and keep its workspace until resumed."""

    generation: int = Field(ge=1)


class SuspendResponse(BaseModel):
    sandbox_id: str
    generation: int
    status: str
    # Whether the object store holds a copy, so a resume can land on any worker.
    snapshot: bool = False
    # False when the sandbox was already suspended: the call changed nothing.
    suspended: bool = True
    # When the reaper will release it; None when retention is disabled.
    retained_until: UtcDatetime | None = None


class ResumeLocalRequest(CreateSandboxRequest):
    """Worker protocol: register a dormant sandbox again."""

    restore: Literal["reuse", "snapshot"] = "reuse"


class ResumeResponse(RouteResponse):
    # False when the sandbox was already awake: the call changed nothing.
    resumed: bool = True
    # `active`: it was not suspended. `reused`: its directory was still on the
    # worker. `restored`: it was rebuilt from the snapshot.
    workspace_source: Literal["active", "reused", "restored"] = "reused"


class ExecRequest(BaseModel):
    exec_id: str = Field(min_length=3, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    generation: int = Field(ge=1)
    argv: list[str] = Field(min_length=1, max_length=256)
    cwd: str = Field(default="/workspace", max_length=4096)
    env: dict[str, str] = Field(default_factory=dict, max_length=128)
    sensitive_env: dict[str, str] = Field(default_factory=dict, max_length=8, repr=False)
    timeout_seconds: int | None = Field(default=None, ge=1, le=86400)
    background: bool = False
    # When set, the execution only excludes other executions in the same scope.
    # When omitted, it is a lifecycle-level command and stays fully exclusive.
    exec_scope: str | None = Field(
        default=None,
        min_length=3,
        max_length=128,
        pattern=r"^[A-Za-z0-9_.:-]+$",
    )

    @field_validator("cwd")
    @classmethod
    def cwd_is_in_workspace(cls, value: str) -> str:
        if value != "/workspace" and not value.startswith("/workspace/"):
            raise ValueError("cwd must stay inside /workspace")
        if ".." in value.split("/"):
            raise ValueError("cwd may not contain ..")
        return value

    @field_validator("argv")
    @classmethod
    def validate_argv(cls, value: list[str]) -> list[str]:
        if any("\x00" in item or len(item) > 65_536 for item in value):
            raise ValueError("an argv entry contains NUL or is too long")
        return value

    @field_validator("env")
    @classmethod
    def validate_env(cls, value: dict[str, str]) -> dict[str, str]:
        if any(len(key) > 256 or len(item) > 65_536 for key, item in value.items()):
            raise ValueError("environment variable name or value is too long")
        return value


class ExecResponse(BaseModel):
    exec_id: str
    status: str
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    duration_ms: int | None = None
    truncated: bool = False


class FileWriteRequest(BaseModel):
    generation: int = Field(ge=1)
    path: str = Field(min_length=1, max_length=4096)
    content_base64: str = Field(max_length=45 * 1024 * 1024)


class FileReadResponse(BaseModel):
    path: str
    content_base64: str


class DirectoryEntry(BaseModel):
    """One entry in a listing, described by `lstat`.

    A symlink is reported as `symlink` rather than as whatever it points at,
    so a caller can tell the difference before following it.
    """

    name: str
    path: str
    type: Literal["file", "directory", "symlink"]
    size_bytes: int
    modified_at: float
    mode: int


class DirectoryListResponse(BaseModel):
    path: str
    entries: list[DirectoryEntry]
    total: int
    has_more: bool


class MakeDirectoryRequest(BaseModel):
    generation: int = Field(ge=1)
    path: str = Field(min_length=1, max_length=4096)
    # Mirrors `mkdir -p`: create missing parents and tolerate an existing
    # directory, so provisioning a tree is idempotent.
    parents: bool = False


class DeletePathRequest(BaseModel):
    generation: int = Field(ge=1)
    path: str = Field(min_length=1, max_length=4096)
    # Deleting a populated tree has to be asked for explicitly; an accidental
    # recursive delete of a workspace cannot be undone.
    recursive: bool = False


class MovePathRequest(BaseModel):
    generation: int = Field(ge=1)
    source: str = Field(min_length=1, max_length=4096)
    destination: str = Field(min_length=1, max_length=4096)
    overwrite: bool = False


class TemplateBuildRequest(BaseModel):
    """Promote part of a running sandbox into a reusable template."""

    generation: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    # Defaults to the environment root, which is the case the feature exists for.
    source_path: str = Field(default="/envs", min_length=1, max_length=4096)
    description: str = Field(default="", max_length=1024)
    labels: dict[str, str] = Field(default_factory=dict)

    @field_validator("labels")
    @classmethod
    def validate_labels(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > 32:
            raise ValueError("too many template labels")
        for key, item in value.items():
            if not key or len(key) > 64 or len(item) > 256:
                raise ValueError("template label key or value has an illegal length")
        return value


class TemplateAttachRequest(BaseModel):
    """Mount named templates into a sandbox.

    Each entry is `name` or `name@sha256:<hex>`. A pinned digest is reproducible;
    a bare name resolves to whatever revision the catalog currently points at.
    """

    generation: int = Field(ge=1)
    templates: list[str] = Field(min_length=1, max_length=16)


class TemplateResponse(BaseModel):
    name: str
    digest: str
    size_bytes: int
    mount_target: str
    created_at: float
    description: str = ""
    source_sandbox_id: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)


class TemplateListResponse(BaseModel):
    templates: list[TemplateResponse]


class AdminSandboxSummary(BaseModel):
    """One row in the console's sandbox table.

    A subset of `Route`: enough to triage, without the lifecycle history that
    makes a full record too heavy to list.
    """

    sandbox_id: str
    workspace_scope_id: str
    worker_id: str | None = None
    generation: int
    sandbox_uid: int
    status: str
    storage_mode: str
    profile_id: str
    created_at: UtcDatetime | None = None
    last_active_at: UtcDatetime | None = None
    ready_at: UtcDatetime | None = None
    lifecycle_count: int = 1
    total_lifetime_ms: int = 0
    idle_seconds: float | None = None


class AdminSandboxListResponse(BaseModel):
    sandboxes: list[AdminSandboxSummary]
    total: int
    limit: int
    offset: int


class AdminExecSummary(BaseModel):
    sandbox_id: str
    exec_id: str
    generation: int
    worker_id: str
    status: str
    exec_scope: str | None = None
    argv: list[str] = Field(default_factory=list)
    exit_code: int | None = None
    truncated: bool = False
    started_at: UtcDatetime | None = None
    finished_at: UtcDatetime | None = None
    created_at: UtcDatetime | None = None
    duration_ms: int | None = None


class AdminExecDetail(AdminExecSummary):
    """One recorded execution, with the output the listing leaves out.

    The listing omits stdout and stderr because a page of fifty commands with
    their output is unreadable -- and the sandbox API cannot answer for a
    released sandbox, which is exactly the one an operator is reading about.
    """

    stdout: str = ""
    stderr: str = ""


class AdminExecListResponse(BaseModel):
    execs: list[AdminExecSummary]
    total: int
    limit: int
    offset: int


class AdminWorkerSummary(BaseModel):
    """A worker as SQL records it, joined with live registry state.

    `live` distinguishes a worker that is heartbeating from one that only
    exists as a stale row, which is the distinction an operator cares about
    when capacity looks wrong.
    """

    worker_id: str
    worker_epoch: str
    endpoint: str
    status: str
    capacity: int
    running_sessions: int
    profile_hash: str
    started_at: UtcDatetime | None = None
    heartbeat_at: UtcDatetime | None = None
    heartbeat_age_seconds: float | None = None
    live: bool = False


class AdminOverviewResponse(BaseModel):
    """Everything the console's landing page needs, in one round trip."""

    sandboxes_by_status: dict[str, int]
    sandbox_total: int
    workers: list[AdminWorkerSummary]
    worker_total: int
    live_worker_total: int
    capacity_total: int
    running_sessions_total: int
    template_total: int
    # False when there is no object store, in which case the count describes
    # this worker rather than the fleet.
    templates_shared: bool = False
    isolation: dict[str, object] = Field(default_factory=dict)
    disk: dict[str, object] = Field(default_factory=dict)
    # The answering replica's reclamation periods, like the isolation level and
    # the disk figures beside them. An operator looking at an idle sandbox is
    # asking when it will be reclaimed, and the defaults of the release are not
    # the answer a deployment that changed them is running with.
    reclamation: dict[str, float] = Field(default_factory=dict)
    fleet_queries_available: bool = True
