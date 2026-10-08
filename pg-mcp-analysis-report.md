# pg-mcp 项目分析与运行报告

## 1. 项目概述

**pg-mcp** 是一个基于 [Model Context Protocol (MCP)](https://modelcontextprotocol.io) 的 PostgreSQL 自然语言查询服务器。它允许用户通过自然语言提问，由 LLM 生成安全的 SQL 查询，并在 PostgreSQL 数据库上执行后返回结果。

**核心能力：**
- 自然语言转 SQL（基于 OpenAI GPT 模型）
- SQL 安全校验（只读、阻止危险函数、防注入）
- Schema 缓存与自动刷新
- 连接池、熔断器、限流器等弹性机制
- Prometheus 指标暴露
- Claude Desktop 等 MCP 客户端兼容

**版本：** `0.2.1`（来自 `pyproject.toml`）

---

## 2. 技术栈

| 层级 | 技术 |
|------|------|
| 语言 | Python（`pyproject.toml` 要求 `>=3.14`，实际在 Python 3.13.5 上验证） |
| MCP SDK | `fastmcp` / `mcp` |
| 数据库驱动 | `asyncpg`（异步连接池） |
| SQL 解析 | `sqlglot` |
| LLM | `openai` SDK |
| 配置 | `pydantic-settings` |
| 日志/指标 | 自定义 JSON/Text 日志、`prometheus-client` |
| 测试 | `pytest`、`pytest-asyncio`、`pytest-cov` |

---

## 3. 目录结构

```
pg-mcp/
├── src/pg_mcp/                 # 主源码
│   ├── __main__.py             # 真实入口：启动 FastMCP stdio 服务
│   ├── server.py               # FastMCP 服务器、生命周期管理、query tool
│   ├── config/settings.py      # Pydantic 配置（环境变量驱动）
│   ├── models/                 # 数据模型：query、schema、errors
│   ├── services/               # 核心业务
│   │   ├── orchestrator.py     # 查询编排器
│   │   ├── sql_generator.py    # LLM SQL 生成
│   │   ├── sql_validator.py    # SQL 安全校验
│   │   ├── sql_executor.py     # 异步 SQL 执行
│   │   └── result_validator.py # LLM 结果校验
│   ├── db/                     # 连接池、Schema 内省
│   ├── cache/                  # Schema 缓存
│   ├── resilience/             # 熔断器、限流器
│   ├── observability/          # 日志、指标
│   └── prompts/                # LLM Prompt 模板
├── tests/                      # 测试
│   ├── unit/                   # 单元测试（无需外部服务）
│   ├── integration/            # 集成测试（需 PostgreSQL + OpenAI）
│   └── e2e/                    # 端到端测试（需 PostgreSQL + OpenAI）
├── fixtures/                   # 示例数据库 SQL
├── main.py                     # ⚠️ 根目录 demo 文件，与真实服务无关
├── pyproject.toml
├── .env.example
├── docker-compose.yml
└── README.md / QUICKSTART.md / CLAUDE.md
```

> **注意**：根目录 `main.py` 只是一个返回固定值 `42` 的 FastMCP demo，**不是** 真实服务入口。真实入口是 `src/pg_mcp/__main__.py`（即 `python -m pg_mcp`）。

---

## 4. 架构流程

```
用户自然语言问题
    │
    ▼
┌─────────────────┐
│   MCP Server    │  FastMCP (stdio / sse)
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ QueryOrchestrator│ 编排整个流程
└────────┬────────┘
         │
    ┌────┴────┬────────────┐
    ▼         ▼            ▼
SQLGenerator  SQLValidator  SQLExecutor
   (LLM)       (安全)         (DB)
    │           │              │
    ▼           ▼              ▼
SchemaCache   只读/黑名单    ResultValidator
              多语句检测       (LLM 置信度)
```

**查询生命周期：**
1. 接收 `question`、`database`、`return_type` 参数。
2. 解析数据库名，从 `SchemaCache` 加载 Schema。
3. 调用 `SQLGenerator` 生成 SQL，失败时带错误反馈重试（最多 `max_retries` 次）。
4. `SQLValidator` 校验 SQL：只允许 SELECT/WITH/UNION，禁止 DDL/DML、危险函数、多语句。
5. 若 `return_type=sql`，直接返回 SQL。
6. 若 `return_type=result`，`SQLExecutor` 在只读事务中执行，应用行数/超时限制。
7. `ResultValidator` 用 LLM 校验结果与问题的匹配度，生成置信度分数。
8. 返回统一响应结构 `{success, generated_sql, data, error, confidence, tokens_used}`。

---

## 5. 关键配置

配置通过 `.env` 文件或环境变量加载，主要分组：

| 分组 | 关键变量 | 说明 |
|------|----------|------|
| 数据库 | `DATABASE_HOST/PORT/NAME/USER/PASSWORD` | PostgreSQL 连接 |
| 连接池 | `DATABASE_MIN_POOL_SIZE`、`DATABASE_MAX_POOL_SIZE` | 默认 5/20 |
| OpenAI | `OPENAI_API_KEY`、`OPENAI_MODEL` | 默认模型 `gpt-5.2-mini`（代码默认值 `gpt-4o-mini`） |
| 安全 | `SECURITY_ALLOW_WRITE_OPERATIONS`、`SECURITY_BLOCKED_FUNCTIONS`、`SECURITY_MAX_ROWS` | 只读、黑名单、行数限制 |
| 缓存 | `CACHE_ENABLED`、`CACHE_SCHEMA_TTL` | Schema TTL 默认 3600s |
| 弹性 | `RESILIENCE_MAX_RETRIES`、`RESILIENCE_CIRCUIT_BREAKER_THRESHOLD` | 重试与熔断 |
| 可观测 | `OBSERVABILITY_METRICS_ENABLED`、`OBSERVABILITY_METRICS_PORT` | Prometheus 端口默认 9090 |

> 注意：README 中声称默认日志格式为 `json`，但 `settings.py` 中 `ObservabilityConfig.log_format` 默认值为 `"text"`，导致单元测试 `test_default_values` 失败。

---

## 6. 编译与运行结果

### 6.1 环境准备

- 系统仅有 Python 3.13.5，而项目声明 `requires-python = ">=3.14"`。
- 使用 `PIP_IGNORE_REQUIRES_PYTHON=1` 绕过版本检查后，`pip install -e ".[dev]"` 成功安装依赖。
- `uv` 未安装，全程使用 `pip`。

### 6.2 依赖兼容性修复

安装后首次导入失败：

```python
# src/pg_mcp/server.py 原代码
from mcp.server.fastmcp import FastMCP
```

报错：

```
ModuleNotFoundError: No module named 'mcp.server.fastmcp'.
This is mcp 2.x, where FastMCP was renamed to MCPServer.
```

**修复**：将导入改为 `fastmcp` 包提供的 `FastMCP`：

```python
from fastmcp import FastMCP
```

修复后 `from pg_mcp.server import mcp, query, lifespan` 成功，FastMCP API（`@mcp.tool()`、`mcp.run_stdio_async`、`lifespan`）兼容。

### 6.3 启动尝试

使用伪造 OpenAI Key 测试初始化流程：

```powershell
$env:OPENAI_API_KEY="sk-fake-test-key-for-analysis"
$env:DATABASE_HOST="localhost"
$env:DATABASE_NAME="blog_small"
$env:DATABASE_USER="postgres"
$env:DATABASE_PASSWORD="postgres"
$env:OBSERVABILITY_METRICS_ENABLED="false"
python -m pg_mcp
```

结果：
- 配置加载成功。
- 日志初始化成功。
- **数据库连接失败**：本地未运行 PostgreSQL，符合预期。

结论：**代码逻辑可运行到数据库连接阶段**，真实使用需配置正确的 PostgreSQL 与 OpenAI API Key。

---

## 7. 测试情况

### 7.1 单元测试

```bash
python -m pytest tests/unit/ -v
```

**结果：247 通过，1 失败**

唯一失败：

```
tests/unit/test_config.py::TestObservabilityConfig::test_default_values
E   AssertionError: assert 'text' == 'json'
```

原因：测试期望 `log_format` 默认值为 `"json"`，但 `settings.py` 中实际默认值为 `"text"`。

### 7.2 集成 / 端到端测试

```bash
python -m pytest tests/integration/ -v
python -m pytest tests/e2e/ -v
```

**结果：全部失败（集合阶段即失败）**

原因：
- `Settings()` 初始化时校验 `OPENAI_API_KEY` 不能为空。
- 缺少真实的 PostgreSQL 服务。

这些测试需要外部依赖，无法在当前无数据库、无 OpenAI Key 的环境中通过。

### 7.3 代码质量检查

**mypy：**

```bash
python -m mypy src
```

发现 8 个类型错误：

| 文件 | 问题 |
|------|------|
| `models/query.py:214` | `to_dict` 方法重复定义（line 160 与 line 214） |
| `services/sql_validator.py:184-193` | 多个 `_check_*` 方法参数类型不匹配（`Expr` vs `Expression`） |
| `services/sql_generator.py:189,197` | 从正则匹配返回 `Any`，未做类型断言 |
| `server.py:42` | `type: ignore` 注释多余 |

**ruff：**

```bash
python -m ruff check src tests
```

发现 4 个问题：

| 文件 | 问题 |
|------|------|
| `models/query.py:214` | `F811` `to_dict` 重定义 |
| `db/pool.py:82` | `ASYNC109` 异步函数使用 `timeout` 参数名 |
| `db/pool.py:109` | `UP041` 应使用 `TimeoutError` 替代 `asyncio.TimeoutError` |
| `server.py:231` | `UP041` 应使用 `TimeoutError` 替代 `asyncio.TimeoutError` |

---

## 8. 发现的问题与建议

### 8.1 高优先级问题

1. **入口文件混乱**
   - 根目录 `main.py` 是无关 demo，应删除或替换为指向真实入口的说明。
   - 真实入口 `src/pg_mcp/__main__.py` 使用 `python -m pg_mcp` 启动。

2. **FastMCP 导入路径不兼容**
   - 已修复：将 `from mcp.server.fastmcp import FastMCP` 改为 `from fastmcp import FastMCP`。
   - 建议：锁定 `fastmcp` 与 `mcp` 版本，或在 `pyproject.toml` 中明确约束，避免未来 API 再次变更。

3. **Python 版本要求过高且不现实**
   - `requires-python = ">=3.14"` 在当前环境无法直接满足（仅有 3.13.5）。
   - 代码本身并未使用 Python 3.14 特有特性，建议改为 `>=3.11` 或 `>=3.12`。

4. **`models/query.py` 中 `to_dict` 重复定义**
   - 第一个实现使用 `exclude_none=False` 并确保 `tokens_used` 存在；第二个使用 `exclude_none=True`。
   - 第二个会覆盖第一个，可能导致返回字段不一致。建议合并为单一实现。

5. **`models/errors.py` 中 `ErrorDetail.to_dict` 序列化问题**
   - `result["code"] = self.code` 直接赋值 `ErrorCode` 枚举对象，JSON 序列化时可能不符合预期。
   - 建议改为 `self.code.value`。

### 8.2 中优先级问题

6. **异步代码中使用 `threading.Lock`**
   - `resilience/circuit_breaker.py` 使用 `threading.Lock`，在 async 事件循环中会阻塞。
   - 建议改用 `asyncio.Lock` 或采用无锁状态管理。

7. **`MultiRateLimiter` 已实例化但未使用**
   - `server.py` 创建了 `_rate_limiter`，但 `orchestrator.py` 与 SQL 生成/执行流程均未调用。
   - 建议接入到查询编排器中，或移除以减少误导。

8. **默认日志格式不一致**
   - README 与测试认为默认是 `json`，代码默认是 `text`。
   - 建议统一：将 `ObservabilityConfig.log_format` 默认值改为 `"json"`。

9. **`Settings` 强依赖真实环境变量**
   - 集成/E2E 测试因缺少 `OPENAI_API_KEY` 无法收集。
   - 建议为测试提供 mock/fixture，或在测试配置中允许空 Key。

### 8.3 低优先级 / 建议改进

10. **Schema 内省效率**
    - `introspection.py` 对每张表的每列都执行一次 `_is_column_unique` 查询，可优化为单条批量查询。

11. **异常类型细化**
    - `sql_executor.py` 的 catch-all `except Exception` 可进一步区分网络错误、权限错误等。

12. **README 示例模型名**
    - README 中提到 `gpt-5.2-mini`，但该模型名在 OpenAI 当前产品线中不存在（可能是虚构/未来模型），建议核实。

---

## 9. 运行方式

### 9.1 安装（使用 pip）

```bash
# 若 Python 版本低于 3.14，需要忽略版本要求
PIP_IGNORE_REQUIRES_PYTHON=1 pip install -e ".[dev]"
```

### 9.2 配置

```bash
cp .env.example .env
# 编辑 .env，填写 DATABASE_* 与 OPENAI_API_KEY
```

### 9.3 启动服务

```bash
python -m pg_mcp
```

### 9.4 运行测试

```bash
# 单元测试（无需外部服务）
python -m pytest tests/unit/

# 集成/E2E 测试（需 PostgreSQL + OpenAI API Key）
python -m pytest tests/integration/ tests/e2e/
```

### 9.5 代码检查

```bash
python -m ruff check src tests
python -m mypy src
```

---

## 10. 总结

pg-mcp 是一个功能设计完整的 PostgreSQL 自然语言 MCP 服务器，具备 SQL 生成、安全校验、执行、结果验证、缓存、弹性等生产级能力。代码结构清晰，单元测试覆盖较广（248 条）。

但在当前状态下存在若干**可运行的障碍与不一致**：

- 依赖版本约束导致 FastMCP 导入失败（已临时修复）。
- Python 版本声明过高，无法直接安装。
- 根目录 `main.py` 与真实服务入口不一致。
- 配置默认值、模型实现存在内部不一致（`log_format`、`to_dict` 重复定义）。
- 异步代码中混用同步锁、`MultiRateLimiter` 未接入。

**建议下一步：**
1. 统一并锁定 MCP/FastMCP 版本。
2. 调整 Python 版本要求至 `>=3.11` 或 `>=3.12`。
3. 修复 `models/query.py`、`models/errors.py`、日志默认值等代码/测试不一致问题。
4. 为集成/E2E 测试补充 mock 数据库与 mock LLM，使其可在 CI 中运行。
5. 清理或替换根目录 `main.py`。

---

*报告生成时间：2026-10-08*
