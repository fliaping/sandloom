CREATE TABLE IF NOT EXISTS agent_sandbox_worker (
  worker_id VARCHAR(128) NOT NULL PRIMARY KEY,
  worker_epoch VARCHAR(64) NOT NULL,
  endpoint VARCHAR(512) NOT NULL,
  status VARCHAR(32) NOT NULL,
  capacity INT NOT NULL,
  running_sessions INT NOT NULL DEFAULT 0,
  profile_hash VARCHAR(191) NOT NULL,
  heartbeat_at DATETIME(6) NOT NULL,
  started_at DATETIME(6) NOT NULL,
  updated_at DATETIME(6) NOT NULL,
  KEY idx_agent_sandbox_worker_heartbeat (status, heartbeat_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS agent_sandbox_route (
  sandbox_id VARCHAR(128) NOT NULL PRIMARY KEY,
  workspace_scope_id VARCHAR(191) NOT NULL,
  worker_id VARCHAR(128),
  worker_epoch VARCHAR(64),
  generation BIGINT NOT NULL DEFAULT 1,
  sandbox_uid INT NOT NULL,
  profile_id VARCHAR(64) NOT NULL,
  profile_hash VARCHAR(191) NOT NULL,
  status VARCHAR(32) NOT NULL,
  storage_mode VARCHAR(16) NOT NULL,
  active_exec_id VARCHAR(128) COMMENT 'representative running exec_id; NULL when fully idle',
  last_active_at DATETIME(6) NOT NULL,
  generation_started_at DATETIME(6) NOT NULL,
  generation_created_by VARCHAR(255),
  ready_at DATETIME(6),
  last_released_generation BIGINT,
  last_released_at DATETIME(6),
  last_release_reason VARCHAR(32),
  last_released_by VARCHAR(255),
  last_lifetime_ms BIGINT,
  lifecycle_count INT NOT NULL DEFAULT 1,
  total_lifetime_ms BIGINT NOT NULL DEFAULT 0,
  lifecycle_history_json JSON,
  created_at DATETIME(6) NOT NULL,
  updated_at DATETIME(6) NOT NULL,
  UNIQUE KEY uniq_agent_sandbox_uid (sandbox_uid),
  KEY idx_agent_sandbox_route_scope (workspace_scope_id),
  KEY idx_agent_sandbox_route_worker (worker_id, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

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
  exit_code INT,
  stdout_text MEDIUMTEXT,
  stderr_text MEDIUMTEXT,
  truncated TINYINT(1) NOT NULL DEFAULT 0,
  started_at DATETIME(6),
  finished_at DATETIME(6),
  created_at DATETIME(6) NOT NULL,
  PRIMARY KEY (sandbox_id, exec_id),
  KEY idx_agent_sandbox_exec_status (sandbox_id, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
