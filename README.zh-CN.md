# Sandloom（沙织）

[English](README.md) | 简体中文

*One container. Many sandboxes.* —— 一个容器，多个沙箱。

Sandloom 是面向 AI Agent 的高密度执行服务。一个可信的 Worker 容器可以承载多个相互隔离、持久化的工作空间，避免为每个 Agent 单独启动容器带来的启动开销和内存消耗。

每个沙箱都有独立的 Linux UID、可写工作空间、主目录、软件包缓存、资源限制和 Bubblewrap 命名空间。控制平面提供粘性路由、generation 隔离令牌、幂等执行、空闲回收，以及 HTTP/MCP API。

先阅读[部署指南](docs/DEPLOYMENT.md)，了解环境要求和中间件；也可以通过[环境诊断指南](docs/ENVIRONMENT_DIAGNOSTICS.md)，让 Agent 检查 Worker 的实际能力，提出由运维人员确认的隔离配置。

本页提供完整的中文介绍；下方链接的专题文档目前仍为英文，命令、配置项和 API 名称与英文版保持一致。

![Sandloom 架构图](docs/assets/sandloom-architecture.png)

图中展示的是内置的 Linux/Bubblewrap 执行后端。命名空间和进程级资源限制不等于整个沙箱的聚合资源配额；S3 归档也不是实时工作空间文件系统。

> 状态：Beta。服务针对意外的跨工作空间访问提供较强防护。请将 `basic` 和 `standard` 视为可信 Worker 内的纵深防御；在用于不可信的公共多租户场景前，必须根据自己的威胁模型验证 `strict`。

## 为什么需要 Sandloom

当数千个 Agent 处于空闲状态，或只需要执行短命令时，“每个 Agent 一个容器”的设计会浪费资源。Sandloom 共享较重的外层容器与工具链，同时保持工作空间归属和所选命名空间边界相互独立：

```text
外层 Worker 容器
├── 控制平面 + 调度器
├── 沙箱 A：UID 20001 + Bubblewrap + /workspace
├── 沙箱 B：UID 20002 + Bubblewrap + /workspace
└── 沙箱 C：UID 20003 + Bubblewrap + /workspace
```

这一设计与 [Cloudflare Computer](https://blog.cloudflare.com/cloudflare-computer/) 有相似之处：都将 Agent 的持久化工作空间与执行过程分离，避免为每次操作分配完整容器。Cloudflare 在同一个虚拟文件系统上动态选择轻量级 isolate 或容器。本项目目前在 POSIX 工作空间之上提供 Bubblewrap 后端，并通过执行后端 SPI 支持接入 isolate、虚拟机或远程后端。详细对比见[架构说明](docs/ARCHITECTURE.md)。

## 功能特性

- 一个 Worker 容器承载多个 Agent 沙箱。
- 每个沙箱拥有持久化的工作空间、主目录、环境和软件包缓存。
- 自动探测 Bubblewrap 能力，支持 `basic`、`standard`、`strict` 分级隔离。
- 面向异构环境，支持在基线之上叠加必需或可选能力，并提供机器可读的本地诊断工具；不会静默削弱基线。
- 显式配置网络策略，与隔离强度分开管理。
- 通过可移植的 SQLAlchemy 适配器，支持 MySQL、PostgreSQL 和 SQLite 元数据存储。
- 支持内存或 Redis Worker 注册与发现。
- 支持兼容 S3 的对象存储，使用标准 AWS 凭据链，用于跨 Worker 分发可复用环境以及保存调用方的检查点归档；快照和恢复流程由调用方编排。
- 通过 generation 隔离令牌，阻止旧 Worker 修改已经重新分配的工作空间。
- 挂起与幂等恢复：等待中的 Agent 归还名额并保留工作空间，休眠工作空间有保留期限和磁盘回收。
- 提供 REST 和无状态 Streamable HTTP MCP API。
- 环境模板构建一次，即可在多个沙箱中以只读方式挂载。
- `/admin` 提供无依赖的集群管理页面，无需前端构建，也不依赖 CDN。
- 通过插件入口扩展元数据、注册发现、对象存储、执行后端、凭据代理和遥测接收器。
- 公共与私有代码严格分离：企业内部实现仅通过单独安装的私有包提供。

## 快速开始

Bubblewrap 需要 Linux。在 macOS 或 Windows 上，请通过 Linux 虚拟机或容器运行服务。

```bash
cp .env.example .env
export SANDBOX_INTERNAL_TOKEN="$(openssl rand -base64 32)"
docker build -f Dockerfile.base -t agent-sandbox-base:latest .
docker compose up --build
```

`.env` 会传入容器，其中的配置会生效。少数必须由 Compose 管理的值——内部令牌以及由命名卷承载的路径——定义在 `environment:` 中，优先级高于文件中的值。

默认部署使用 SQLite、进程内 Worker 注册表和命名卷，适合单副本服务。Compose 关闭了外层容器的默认 seccomp 配置，因为默认策略会阻止 Bubblewrap 创建嵌套命名空间所需的系统调用。如果保留这一策略，服务不会降级到更弱的隔离：探测会发现所有级别均不可用，并在启动时退出。使用默认 `RuntimeDefault` 策略的 Kubernetes Pod 也可能遇到这一情况。管理进程是可信的，Agent 命令仍在协商后的 Bubblewrap 边界中执行；这并不授予特权模式或额外的 Linux capabilities。检查就绪状态：

```bash
curl http://127.0.0.1:8080/healthz
```

响应会报告实际探测得到的隔离级别。在 Docker Desktop 和 OrbStack 上通常是 `basic`：外层运行时屏蔽了部分 `/proc` 路径，使 `standard` 无法挂载私有 procfs；`/healthz` 的 `probe_failures` 会说明原因。在同一主机上解除这些屏蔽后，可以达到 `strict`：

```yaml
# compose.override.yaml —— 采用前请阅读 docs/ISOLATION.md。
services:
  agent-sandbox:
    security_opt: !override
      - seccomp=unconfined
      - systempaths=unconfined
```

这会扩大可信管理容器能够访问的范围，需要明确评估后再启用；不要默认快速开始配置已经提供最强边界。详见[隔离说明](docs/ISOLATION.md)。

多副本部署请使用 PostgreSQL 或 MySQL、Redis，并选择共享 RWX POSIX 工作空间，或显式编排检查点与恢复流程。

## 使用示例

`examples/quickstart.py` 使用 Python 标准库演示完整生命周期：解析路由、连接、执行、写入、列出、移动、删除文件，最后释放沙箱。

```bash
export SANDBOX_INTERNAL_TOKEN=...   # 与启动服务时使用的值一致
python examples/quickstart.py
```

脚本会逐步输出操作结果，可以从头到尾阅读；其中每个调用都是自己的客户端需要执行的调用。运行失败时也会先释放沙箱再退出，避免测试遗留资源。

`examples/templates.py` 演示环境模板：在一个沙箱中构建 virtualenv，发布模板，再将其挂载到一个没有安装该环境的沙箱中，并验证挂载为只读。

```bash
python examples/templates.py
```

项目验证的每种部署都会运行这两个示例，避免示例与实际 API 脱节。

`scripts/verify-deployment.py` 验证更完整的能力：沙箱生命周期、文件 API、模板、端到端 MCP 工具调用、管理页面 API，以及通过登录 Shell 和普通 Shell 编译、运行所有已启用语言的程序。它无需安装额外依赖，也不需要令牌以外的权限，适合验证现有部署：

```bash
export SANDBOX_INTERNAL_TOKEN=...
./scripts/verify-deployment.py --base-url http://10.0.0.7:8080 --strict
```

## 隔离级别

通过 `SANDBOX_ISOLATION_LEVEL=auto|basic|standard|strict` 配置级别。启动时会在每个级别执行真实命令；`auto` 选择内核和外层容器实际支持的最高级别。显式指定的级别不可用时，服务启动失败，不会静默削弱边界。

| 级别 | 提供的隔离边界 | 常见外部要求 |
| --- | --- | --- |
| `basic` | 降权至独立 UID、`no_new_privs`、资源限制、user/mount/IPC/UTS 命名空间、只读系统挂载、私有 `/tmp` | 允许 user namespace 和 Bubblewrap |
| `standard` | `basic` + PID 命名空间 + 私有 procfs | 容器策略允许 PID 命名空间和 proc 挂载 |
| `strict` | `standard` + cgroup 命名空间 | 内核及运行时策略允许 cgroup 命名空间 |

`SANDBOX_NETWORK_MODE=host` 保留出站网络访问，`isolated` 增加私有网络命名空间。完整前置条件和降级行为见[隔离说明](docs/ISOLATION.md)。

当前多语言镜像在 `basic` 下支持 Python、Node、Go、Java 和 Rust；镜像内的 Java/Rust 启动适配器显式指定库和 sysroot 路径，不需要挂载 procfs。TypeScript 使用 Node，还需要安装编译器或运行器。更高隔离级别增加进程隔离，与语言支持相互独立。选择级别后，Worker 会在临时沙箱中实际启动已启用的工具，并在 `/healthz` 的 `worker.capabilities.toolchains.checks` 中单独报告测得的可用性，因此自定义镜像可以在不改变隔离级别的情况下报告不同兼容性。[工具链说明](docs/TOOLCHAINS.md)介绍了这些检查及其边界。

各级别的含义保持稳定。如果环境支持不同的能力组合，可以在基线上叠加能力，而不是重新定义某个级别：

```dotenv
SANDBOX_ISOLATION_LEVEL=basic
SANDBOX_ISOLATION_REQUIRED_FEATURES=cgroup_namespace
SANDBOX_ISOLATION_OPTIONAL_FEATURES=pid_namespace
```

每种组合都会经过真实执行探测。必需能力失败会阻止启动；可选能力失败则报告原因并跳过。PID 隔离包含私有 procfs。最终配置的哈希用于区分调度池。采用配置前请运行 `docker compose run --rm --no-deps agent-sandbox python -m agent_sandbox.doctor --json`，详见[环境诊断指南](docs/ENVIRONMENT_DIAGNOSTICS.md)。

不要仅仅为了通过探测，就给 Worker 添加 `--privileged`、挂载 Docker socket 或授予 `CAP_SYS_ADMIN`。探测失败说明外部环境无法安全提供该级别所需的能力。

## 存储与部署适配器

| 职责 | 内置选择 | 配置项 |
| --- | --- | --- |
| 元数据 | SQLite、MySQL、PostgreSQL | `SANDBOX_DATABASE_URL` |
| Worker 注册表 | memory、Redis、私有插件 | `SANDBOX_REGISTRY_BACKEND` |
| 工作空间 | 本地 POSIX、共享 RWX POSIX | `SANDBOX_LOCAL_ROOT` / `SANDBOX_SHARED_ROOT` |
| 检查点 | 兼容 S3 的对象存储 | `BLOBSTORE_*` |
| 执行后端 | Bubblewrap、第三方插件 | `SANDBOX_EXECUTION_BACKEND` |

配置示例：

```bash
# PostgreSQL
SANDBOX_DATABASE_URL=postgresql+asyncpg://user:pass@db/agent_sandbox
SANDBOX_DATABASE_AUTO_DDL=true

# MySQL
SANDBOX_DATABASE_URL=mysql+aiomysql://user:pass@db/agent_sandbox

# 多副本服务
SANDBOX_REGISTRY_BACKEND=redis
SANDBOX_REDIS_URL=redis://redis:6379/0
```

默认的 SQLite 使用 WAL 模式，写入超时为 15 秒，使并发命令等待写锁，而不是立即报 `database is locked`。但它仍然只能同时容纳一个写入者，集群部署建议使用 PostgreSQL。详见[元数据配置](docs/CONFIGURATION.md#metadata)。

`SANDBOX_DATABASE_AUTO_DDL=true` 允许服务自行建表，快速开始依赖这一配置。数据库由外部管理、结构独立发布时，将其设为 `false`，使用 `deploy/sql/generic-postgresql.sql` 或 `deploy/sql/generic-mysql.sql` 建立代码需要的表结构，服务运行时不再建表。两种方言都有测试保障 SQL 文件与代码一致，避免新增字段后部署仍通过测试，却在第一个请求中报字段不存在。

服务会在启动时检查这一问题：接受请求前，将自身所需结构与数据库结构比较，缺少表或字段时拒绝启动，以退出码 3 退出，并在日志中列出缺失项。对于外部管理的数据库，需要注意：

- 升级需要 `ALTER TABLE`。表已经存在时，`CREATE TABLE IF NOT EXISTS` 会跳过整张表，重新执行随项目提供的建表文件不会更新旧表。启动错误会提示这一点；随项目提供的文件始终是完整建表脚本，不会修改已有表。
- 多出的字段仅告警，不阻止启动。数据库结构领先于代码是回滚时的正常情况，服务因此允许启动，避免阻碍回滚。

### 多副本部署

增加副本意味着使用相同镜像和配置，配合共享数据库、共享注册表，以及明确的工作空间存储方案。存储与工作空间不会因为运行了两个进程，就自动变成共享资源：

```yaml
# 每个副本使用相同配置
SANDBOX_DATABASE_URL: postgresql+asyncpg://user:pass@db/agent_sandbox
SANDBOX_DATABASE_AUTO_DDL: "true"          # 或自行执行 deploy/sql/*.sql
SANDBOX_REGISTRY_BACKEND: redis
SANDBOX_REDIS_URL: redis://redis:6379/0
BLOBSTORE_ENDPOINT: http://object-store:9000   # 用于跨 Worker 分发模板
```

`compose.fleet.yaml` 提供可直接运行的示例：两个本项目服务副本连接 PostgreSQL、Redis 和对象存储，分别暴露 18081、18082 端口。`./scripts/verify-fleet.py` 作为客户端验证以下行为：请求转发至沙箱所属副本、跨 Worker 挂载模板、拒绝旧 generation，以及 Worker 失联后正在运行的命令明确失败而非一直挂起、Worker 从可用集群视图消失、其资源最终回收。

```sh
export SANDBOX_INTERNAL_TOKEN=$(openssl rand -hex 32)
# 缩短回收等待，便于快速完成验证；正式默认值以分钟为尺度回收。
SANDBOX_HEARTBEAT_INTERVAL_SECONDS=2 SANDBOX_HEARTBEAT_TTL_SECONDS=10 \
SANDBOX_MAINTENANCE_INTERVAL_SECONDS=2 SANDBOX_IDLE_TTL_SECONDS=45 \
SANDBOX_ORPHAN_RELEASE_GRACE_SECONDS=10 \
    docker compose -f compose.fleet.yaml --profile other-profile up -d --wait
uv run python scripts/verify-fleet.py --strict --short-graces
docker compose -f compose.fleet.yaml --profile other-profile down -v
```

对象存储的 bucket 不需要预先创建：启动时若端点上不存在该 bucket，各副本会尝试创建；失败时会报告 bucket、端点和 S3 错误。

相同的 `SANDBOX_PROFILE_HASH` 同样重要。配置不同的副本仍会注册并显示在集群视图中，但每个副本只将沙箱调度到配置哈希与自身一致的 Worker。因此，集群只完成一半更新时，部分副本可能返回“没有可用 Worker”。测试实际覆盖了两侧行为：`compose.fleet.yaml` 的 `other-profile` 配置启动第三个哈希不同的副本，`verify-fleet.py` 确认它在线，从其他副本连续解析八次都不会选中它，而通过它解析时会落到它自身。

副本只处理自己拥有的沙箱；请求到达错误副本时，通过 `/internal/v1` 内部 API 转发至所属 Worker。内部 API 是 Worker 间协议，不是另一套公共 API：所有路由都要求 `SANDBOX_INTERNAL_TOKEN`，目标 Worker 由 `X-Sandbox-Worker-ID` 指定。目标错误时返回 `409 STALE_SANDBOX_ROUTE`，不会操作其他 Worker 的沙箱。`GET /internal/v1/health` 是目标寻址上的例外：它始终报告响应者自身，不会指向其他 Worker；可用于判断是否需要排空 Worker：

```bash
curl -s -H "Authorization: Bearer $SANDBOX_INTERNAL_TOKEN" \
    http://10.0.0.7:8080/internal/v1/health
# {"status":"UP","worker_id":"...","worker_epoch":"...","capacity":32,
#  "running_sessions":2,"running_execs":1,"disk":{...}}
# 磁盘不可用时，status 为 DRAINING
```

已验证三个副本同时连接空 PostgreSQL 数据库的场景：全部启动、完成注册，没有因并发建表而出现重复键错误；在一个 Worker 上构建的模板可以由另一个 Worker 上的沙箱挂载并读取。连接旧结构数据库的副本会拒绝加入，以退出码 3 退出并指出缺失字段，已经运行的副本则继续服务。

这里有两个需要特别注意的设计行为：`resolve` 根据最近一次心跳中的负载选择 Worker，因此同一个心跳周期内的突发请求可能集中到同一个 Worker，即使旁边还有空闲 Worker；详见[架构说明](docs/ARCHITECTURE.md)中的“Consistency model”。此外，沙箱固定归属于创建它的 Worker，因此 Worker 归属是沙箱的属性，不是单次请求的属性。

容器镜像已包含各适配器的可选依赖，可以直接通过环境变量切换，无需重新构建。直接安装 Python 包时，再选择需要的依赖：

```bash
# 从源码构建两个发行包。先安装 runtime，使应用依赖匹配的本地 wheel，而非公共索引。
(cd runtime && uv build --out-dir ../dist) && uv build --out-dir dist
pip install dist/sandloom_runtime-0.2.0-py3-none-any.whl
pip install 'dist/sandloom-0.2.0-py3-none-any.whl[postgres,redis,s3]'
```

发行包名称为 `sandloom` 和 `sandloom-runtime`，发布前必须确认公共包索引上的名称归属。应用 wheel 提供 `sandloom` 和 `sandloom-doctor` 命令。Python 导入路径、`SANDBOX_*` 环境变量、`agent_sandbox.*` 插件组、API 路径以及现有 Compose 服务和卷名称保持兼容；旧的 `agent-sandbox` 命令也保留。

`scripts/verify-distributions.sh` 会从头构建两个包并验证安装和运行，确保上述步骤经过实际检查。

适配器生命周期约定见[适配器指南](docs/ADAPTERS.md)；不公开企业内部代码的兼容迁移方式见[私有适配器指南](docs/PRIVATE_ADAPTERS.md)。

## 配置

配置主要通过 `SANDBOX_` 前缀的环境变量提供。[配置参考](docs/CONFIGURATION.md)列出每个配置项、默认值、用途，以及拒绝无效配置的启动校验。阅读前请注意：未知变量名会被静默忽略；列表写为 `a,b,c`；进程环境变量优先于 `.env` 文件。

## API 调用流程

API 调用要求 `Authorization: Bearer $SANDBOX_INTERNAL_TOKEN`。

以下入口不需要令牌：`GET /health`（存活探测）、`GET /healthz`（状态文档）、`GET /admin`（仅提供静态管理页面），以及 API 描述 `/openapi.json`、Swagger UI `/docs` 和 ReDoc `/redoc`。通过 UI 调用 API 前，请在 Authorize 中填写令牌。测试固定检查这些例外，新增路由如果没有令牌依赖会导致测试失败，避免意外公开。

API 描述包含全部 `/api/v1` 路由，也列出了副本间使用的 `/internal/v1` Worker 协议。内部路由通过 `X-Sandbox-Worker-ID` 指定目标，拒绝发往其他 Worker 的请求，集群外部客户端不应调用它们。

1. 调用 `POST /api/v1/sandboxes/resolve`，传入稳定的 `sandbox_id` 和 `workspace_scope_id`。
2. 使用返回的 generation 创建或连接沙箱。
3. 执行命令或读写文件时，传回同一个 generation。
4. 在 `finally` 清理路径中调用 `DELETE /api/v1/sandboxes/{id}`。

generation 是隔离令牌。路由重新分配时它会递增，即使旧请求延迟到达，Worker 也会拒绝旧归属下的操作。

主要路由：

- `POST /api/v1/sandboxes/resolve`
- `POST /api/v1/sandboxes/{id}`
- `POST /api/v1/sandboxes/{id}/exec`
- `GET /api/v1/sandboxes/{id}/exec/{exec_id}`
- `POST /api/v1/sandboxes/{id}/exec/{exec_id}/cancel`
- `PUT|GET /api/v1/sandboxes/{id}/files`
- `GET /api/v1/sandboxes/{id}/files/list`
- `POST /api/v1/sandboxes/{id}/files/{mkdir,delete,move}`
- `POST|PUT /api/v1/sandboxes/{id}/templates`
- `GET|DELETE /api/v1/templates[/{name}]`
- `GET /api/v1/sandboxes/{id}/audit`
- `GET /api/v1/sandboxes/diagnostics/runtime`
- `GET /api/v1/admin/{overview,sandboxes,execs}`
- `GET /api/v1/admin/execs/{sandbox_id}/{exec_id}`
- `GET /admin`
- `DELETE /api/v1/sandboxes/{id}`
- `POST /api/v1/sandboxes/mcp/streamable-http`

## 工作空间文件

只有命令执行能力时，Agent 必须调用 `ls` 并解析文本。`GET .../files` 和 `PUT .../files` 提供单文件读写，另外四个路由补齐文件系统适配器所需操作：

```bash
# 列出目录。条目由 lstat 描述，符号链接显示为链接本身，而不是链接目标。
GET  /api/v1/sandboxes/my-agent/files/list?path=/workspace&generation=1

# 创建目录树、删除和移动。
POST /api/v1/sandboxes/my-agent/files/mkdir  {"generation":1,"path":"/workspace/src","parents":true}
POST /api/v1/sandboxes/my-agent/files/delete {"generation":1,"path":"/workspace/old","recursive":true}
POST /api/v1/sandboxes/my-agent/files/move   {"generation":1,"source":"/workspace/a","destination":"/workspace/b"}
```

路径都相对于工作空间根目录解析，越界会被拒绝；但不同操作有意采用不同规则：

- 读写完整解析路径，因此不能通过指向工作空间外部的符号链接写入外部文件。
- 删除和移动仅解析父目录，因此删除符号链接只会删除链接本身，不会操作目标文件。完整解析反而可能误判越界，甚至操作链接指向的目标。

递归删除必须显式开启 `recursive`，工作空间根目录始终不可删除。目录列表分页，并受 `SANDBOX_MAX_LIST_ENTRIES` 限制，默认 1000。列表不包含文件内容，读取内容需调用单文件接口。

## 并行执行

每条命令声明独立的 `exec_scope` 后，同一个沙箱可以同时执行多条命令。不同 scope 并发运行，相同 scope 的第二条执行会被拒绝，使每个 scope 相当于一个串行终端。省略 `exec_scope` 表示生命周期级命令：它会独占沙箱，并等待所有 scope 中的命令结束。

```jsonc
// 两个 Agent 线程同时在同一个沙箱中工作。
{"exec_id": "exec-1", "generation": 3, "argv": ["pytest", "-q"], "exec_scope": "thread-a"}
{"exec_id": "exec-2", "generation": 3, "argv": ["npm", "run", "build"], "exec_scope": "thread-b"}
```

`SANDBOX_MAX_PARALLEL_EXECS_PER_SANDBOX` 限制每个沙箱的并发执行数量，默认 16。不同拒绝原因通过响应区分：

| HTTP 状态 | 错误码 | 含义 |
| --- | --- | --- |
| 429 | `SANDBOX_PARALLEL_EXEC_LIMIT` | 沙箱并发已满，稍后重试。 |
| 409 | `SANDBOX_EXEC_SCOPE_BUSY` | 该 scope 或生命周期级命令正在运行。 |
| 409 | `SANDBOX_FILE_PATH_LOCKED` | 文件操作与生命周期级命令或同一路径操作冲突。 |

文件 API 只锁定目标路径，因此不同文件的读写可以与正在执行的命令并行。写入通过临时文件和原子重命名完成，读取者不会看到写了一半的文件。

## 挂起与恢复

等待耗时任务的 Agent 无需一直占用名额。挂起会释放沙箱的容量名额并保留工作空间；恢复会重新申请名额，并以幂等方式接着使用原来的文件继续工作。

```bash
# 等待期间归还名额。有命令在运行时返回 409。
POST /api/v1/sandboxes/my-agent/suspend   {"generation":3}

# 稍后继续。幂等；之后使用返回的 generation。
POST /api/v1/sandboxes/my-agent/resume
```

挂起与 exec 准入在同一行路由记录上原子判定：有命令在运行时挂起会被拒绝（`409 SANDBOX_SUSPEND_BUSY`），已挂起的沙箱会拒绝命令（`409 SANDBOX_SUSPENDED`），不会影响其他沙箱或其他 scope。恢复时优先在原 Worker 上复用目录；否则通过共享工作空间或对象存储快照迁移到其他 Worker。挂起的沙箱在 `SANDBOX_SUSPENDED_RETENTION_SECONDS`（默认 30 天）后被释放。不冻结进程，也不恢复内存。状态机、快照和磁盘回收见[挂起与恢复](docs/SUSPEND_RESUME.md)。

## 环境模板

每个沙箱最初的 `/envs` 都为空，依赖 NumPy、`node_modules` 或额外 Rust 工具链的应用需要重复安装。模板将这些目录构建一次后归档，再以只读方式挂载到需要它的沙箱中。

```bash
# 在沙箱中构建环境，然后发布。
POST /api/v1/sandboxes/env-builder/templates   {"generation":1,"name":"python-ml","source_path":"/envs/python-ml"}

# 挂载到任意 Worker 上的沙箱。
PUT  /api/v1/sandboxes/my-agent/templates      {"generation":1,"templates":["python-ml"]}
```

模板以可复现归档的 SHA-256 标识，经对象存储分发，在各 Worker 上展开为内容寻址缓存。挂载位置为 `/envs/<name>`，所以模板名称应与构建目录名称一致：在 `/envs/python-ml` 构建后发布为 `python-ml`，挂载路径才能符合脚本 shebang 的预期；名称不匹配会使 `bin/pip` 等入口指向不存在的路径。同一个 Worker 上的所有沙箱共享同一只读目录，因此增加使用者几乎不增加模板本身的开销。使用 `python-ml@sha256:<hex>` 固定版本，保证运行可复现。

跨 Worker 共享需要对象存储；未配置时，模板只能在构建它的 Worker 上使用。生命周期、失败模式和磁盘限制见[模板指南](docs/TEMPLATES.md)。

## 支持的语言

每个沙箱会获得已启用语言的托管环境：

```
SANDBOX_TOOLCHAINS=python,node,go,rust,java
```

`python` 和 `node` 默认启用，并包含在基础镜像中。Go、Rust 和 JDK 由多语言镜像提供：使用 `Dockerfile.polyglot` 构建，工具安装在 `/usr/local` 下，沙箱通过已有的只读系统挂载访问。每种语言在 `/cache` 下拥有软件包缓存，在 `/envs` 下拥有安装前缀，也可以打包为模板。`./scripts/integration-test.sh polyglot` 会构建该镜像，启用全部五种工具链，并分别编译、运行示例程序。

多语言镜像提供无需 procfs 的 Java/Rust 适配器，因此五种工具链均可在 `basic` 下运行。Worker 将真实工具启动结果与命名空间能力分开报告。具体要求与部署验证方式见[工具链说明](docs/TOOLCHAINS.md)。

## 管理页面

![Sandloom 集群管理页面](docs/assets/admin-fleet.jpg)

截图来自真实的本地 Docker 部署，使用在 basic 基线上叠加能力的隔离配置，并包含实际工具链探测结果。图中仅有演示标识和数据，不包含 API 令牌。

其他 API 通常要求调用方已经知道沙箱 ID，而运维人员需要先查看整体状态。打开 `/admin` 并填写内部令牌，即可查看集群容量与 Worker 心跳、废弃沙箱的回收周期、按状态或 scope 筛选的沙箱、近期执行及退出码、已发布模板。页面支持释放沙箱和取消发布模板，其余操作均为只读。

```bash
echo "$SANDBOX_INTERNAL_TOKEN"        # 将令牌粘贴到管理页面
open http://127.0.0.1:8080/admin
```

页面是一个自包含 HTML 文件，无需构建、npm 或 CDN，网络不通时也不需要从外网下载字体。`GET /admin` 只返回静态页面，不需要令牌；页面发起的 API 调用都携带令牌，令牌仅保存在当前标签页的 `sessionStorage` 中。

同样的数据也可以直接通过 API 获取：

```bash
curl -s -H "Authorization: Bearer $SANDBOX_INTERNAL_TOKEN" \
  'http://127.0.0.1:8080/api/v1/admin/sandboxes?status=READY'
```

API、分页限制，以及元数据插件未实现集群查询时的降级方式，见[管理页面指南](docs/ADMIN_CONSOLE.md)。

## 容量诊断

`GET /api/v1/sandboxes/diagnostics/runtime` 基于 Worker 心跳聚合集群数据，容量规划不必逐个查询 Worker：

```jsonc
{
  "workers": [{"worker_id": "worker-a", "status": "ACTIVE", "capacity": 32}],
  "excluded_workers": 1,
  "totals": {
    "session_capacity": 64,
    "running_sessions": 12,
    "remaining_sessions": 52,
    "running_execs": 7,
    "existing_session_exec_capacity": 192,
    "existing_session_free_execs": 185
  },
  "queue_length": null
}
```

注册表是可用性索引，不是权威数据库。没有新鲜心跳的 Worker 计入 `excluded_workers`，而不是悄悄丢弃；大型集群的响应有数量边界，截断时会标记 `truncated`。会话和命令是不同的计量单位：命令并发按沙箱分别限制，不能将其他沙箱的空闲执行槽借过来。

`capacity` 是准入阈值，不是硬性资源上限。检查依据 Worker 最近一次心跳，因此突发请求可能暂时超出阈值，直到下一次心跳更新。

沙箱密度、单条命令成本与吞吐量的实测数据，以及元数据后端为什么可能比 CPU 更早成为瓶颈，见[容量规划](docs/SIZING.md)。请在自己的硬件上重新运行 `scripts/load-test.py`，不要直接套用已有测量值。

## 开发与测试

```bash
uv run pytest -q                       # 控制平面
uv run pytest -q runtime/tests         # runtime 发行包
uv run ruff check src runtime/src tests runtime/tests examples scripts
uv run mypy src runtime/src scripts examples
```

默认测试无需在线数据库或 Bubblewrap 进程；无法连接的后端测试会跳过，而不是失败。

适配器还会连接真实服务验证，因为行锁语义、数据库方言的 `JSON` 处理、Redis TTL 到期、预签名 URL 和 Linux 命名空间无法由测试替身完整模拟：

```bash
./scripts/integration-test.sh            # 全部模式，耗时最长
./scripts/integration-test.sh middleware # MySQL、PostgreSQL、Redis、S3
./scripts/integration-test.sh sandbox    # Linux 容器中的 Bubblewrap
./scripts/integration-test.sh polyglot   # Go、Rust 和 JDK 多语言镜像
```

各后端测试证明的能力以及如何连接自己的服务，见[集成测试指南](docs/INTEGRATION_TESTING.md)；提交变更的要求见[贡献指南](CONTRIBUTING.md)。

## 安全与运维

- 管理进程是可信的，只有 Agent 命令是不可信的。
- 不要将共享的内部 Bearer 令牌直接暴露给不可信用户。
- 在服务前方的网关进行租户身份认证和授权，并从可信身份派生 `workspace_scope_id`。
- 本地工作空间不会自动跨 Worker 迁移。共享工作空间需要单写入者隔离机制和文件系统锁。
- 调用方保存在 S3 中的检查点归档不参与命令执行热路径，仅保存归档不会让本地工作空间自动具备高可用性。
- Bubblewrap 不是虚拟机边界；不可信的公共工作负载可能需要 microVM 执行后端插件。

在生产环境部署前，请阅读 [SECURITY.md](SECURITY.md)。[加固状态与里程碑](docs/HARDENING.md)通过明确的验收标准跟踪剩余的资源控制、网络策略执行和模板快照工作。

## 许可证

Apache-2.0，详见 [LICENSE](LICENSE)。

发布分支或镜像仓库前，请完成[开源发布清单](docs/OPEN_SOURCE_RELEASE.md)，包括密钥轮换和干净提交历史审查。
