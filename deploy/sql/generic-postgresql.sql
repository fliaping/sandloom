CREATE TABLE IF NOT EXISTS agent_sandbox_worker (
  worker_id VARCHAR(128) PRIMARY KEY,
  worker_epoch VARCHAR(64) NOT NULL,
  endpoint VARCHAR(512) NOT NULL,
  status VARCHAR(32) NOT NULL,
  capacity INTEGER NOT NULL,
  running_sessions INTEGER NOT NULL DEFAULT 0,
  profile_hash VARCHAR(191) NOT NULL,
  heartbeat_at TIMESTAMP NOT NULL,
  started_at TIMESTAMP NOT NULL,
  updated_at TIMESTAMP NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_sandbox_worker_heartbeat
  ON agent_sandbox_worker (status, heartbeat_at);

CREATE TABLE IF NOT EXISTS agent_sandbox_route (
  sandbox_id VARCHAR(128) PRIMARY KEY,
  workspace_scope_id VARCHAR(191) NOT NULL,
  worker_id VARCHAR(128),
  worker_epoch VARCHAR(64),
  generation BIGINT NOT NULL DEFAULT 1,
  sandbox_uid INTEGER NOT NULL UNIQUE,
  profile_id VARCHAR(64) NOT NULL,
  profile_hash VARCHAR(191) NOT NULL,
  status VARCHAR(32) NOT NULL,
  storage_mode VARCHAR(16) NOT NULL,
  active_exec_id VARCHAR(128), -- representative running exec_id; NULL when fully idle
  last_active_at TIMESTAMP NOT NULL,
  generation_started_at TIMESTAMP NOT NULL,
  generation_created_by VARCHAR(255),
  ready_at TIMESTAMP,
  last_released_generation BIGINT,
  last_released_at TIMESTAMP,
  last_release_reason VARCHAR(32),
  last_released_by VARCHAR(255),
  last_lifetime_ms BIGINT,
  lifecycle_count INTEGER NOT NULL DEFAULT 1,
  total_lifetime_ms BIGINT NOT NULL DEFAULT 0,
  lifecycle_history_json JSON,
  created_at TIMESTAMP NOT NULL,
  updated_at TIMESTAMP NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_sandbox_route_scope
  ON agent_sandbox_route (workspace_scope_id);
CREATE INDEX IF NOT EXISTS idx_agent_sandbox_route_worker
  ON agent_sandbox_route (worker_id, status);

CREATE TABLE IF NOT EXISTS agent_sandbox_exec (
  sandbox_id VARCHAR(128) NOT NULL,
  exec_id VARCHAR(128) NOT NULL,
  generation BIGINT NOT NULL,
  worker_id VARCHAR(128) NOT NULL,
  status VARCHAR(32) NOT NULL,
  command_json JSON NOT NULL,
  -- Authoritative mutual-exclusion scope. NULL marks a lifecycle-level command
  -- that takes the sandbox exclusively.
  exec_scope VARCHAR(128),
  exit_code INTEGER,
  stdout_text TEXT,
  stderr_text TEXT,
  truncated BOOLEAN NOT NULL DEFAULT FALSE,
  started_at TIMESTAMP,
  finished_at TIMESTAMP,
  created_at TIMESTAMP NOT NULL,
  PRIMARY KEY (sandbox_id, exec_id)
);
CREATE INDEX IF NOT EXISTS idx_agent_sandbox_exec_status
  ON agent_sandbox_exec (sandbox_id, status);
