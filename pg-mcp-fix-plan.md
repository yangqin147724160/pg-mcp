# pg-mcp 修复计划

本文件基于 `pg-mcp-analysis-report.md` 中的问题清单，给出具体的修复方案、代码改动示例和验证步骤。

---

## 修复目标

1. 让项目在当前主流 Python 环境（3.11/3.12/3.13）下**无需绕过版本要求即可安装**。
2. 修复依赖与导入兼容性问题，确保 `python -m pg_mcp` 能正常启动。
3. 统一代码、测试、文档中的默认值与行为，**让单元测试 248/248 通过**。
4. 消除 mypy/ruff 报错。
5. 修复异步代码中的同步锁、未使用组件等潜在隐患。

---

## 修复清单与方案

### 🔴 P0：阻塞性问题（不修复则无法运行/测试）

#### 1. 修复 FastMCP 导入路径

**文件**：`src/pg_mcp/server.py`

**问题**：`from mcp.server.fastmcp import FastMCP` 在 `mcp >= 2.x` 中已不存在，会抛出 `ModuleNotFoundError`。

**修复**：改为从 `fastmcp` 包导入。

```python
# 修改前
from mcp.server.fastmcp import FastMCP

# 修改后
from fastmcp import FastMCP
```

**验证**：

```bash
python -c "from pg_mcp.server import mcp, query, lifespan; print('Import OK')"
```

---

#### 2. 调整 Python 版本要求

**文件**：`pyproject.toml`

**问题**：`requires-python = ">=3.14"` 过高，当前环境只有 Python 3.13.5，且代码未使用 3.14 特性。

**修复**：降低至 `>=3.11`（与 `tool.mypy.python_version` 和 `tool.ruff.target-version` 保持一致）。

```toml
# 修改前
requires-python = ">=3.14"

# 修改后
requires-python = ">=3.11"
```

同时修改 `.python-version`：

```text
3.11
```

**验证**：在 Python 3.11/3.12/3.13 下执行：

```bash
pip install -e ".[dev]"
```

无需 `PIP_IGNORE_REQUIRES_PYTHON=1` 即可成功安装。

---

#### 3. 统一默认日志格式（修复单元测试失败）

**文件**：`src/pg_mcp/config/settings.py`

**问题**：`ObservabilityConfig.log_format` 默认值为 `"text"`，但 README 和测试期望 `"json"`。

**修复**：将默认值改为 `"json"`。

```python
# 修改前
log_format: Literal["json", "text"] = Field(default="text", description="Log format")

# 修改后
log_format: Literal["json", "text"] = Field(default="json", description="Log format")
```

**验证**：

```bash
python -m pytest tests/unit/test_config.py::TestObservabilityConfig::test_default_values -v
```

应显示 **PASSED**。

---

#### 4. 修复 `QueryResponse.to_dict` 重复定义

**文件**：`src/pg_mcp/models/query.py`

**问题**：`to_dict` 在 line 160 和 line 214 各定义一次，后一个会覆盖前一个，导致 `tokens_used` 保证逻辑可能失效。

**修复**：删除第二个定义，保留并完善第一个。

```python
# 修改前（line 214-219）
def to_dict(self) -> dict[str, Any]:
    """Convert response to dictionary.

    Returns:
        dict: Dictionary representation of query response.
    """
    return self.model_dump(exclude_none=True)

# 修改后：直接删除 line 175-219 的第二个 to_dict 及相关 validators 重复部分
```

同时检查 `validate_data` 和 `validate_error` 两个 validator 是否也需要保留。若保留，确保逻辑正确；若与 Pydantic 默认行为重复，可删除以简化模型。

**验证**：

```bash
python -m pytest tests/unit/test_models.py -v
python -m ruff check src/pg_mcp/models/query.py
```

---

### 🟡 P1：重要质量问题

#### 5. 修复 `ErrorDetail.to_dict` 序列化

**文件**：`src/pg_mcp/models/errors.py`

**问题**：`ErrorDetail.to_dict` 中 `result["code"] = self.code` 直接赋值为 `ErrorCode` 枚举对象，JSON 序列化时可能不被正确识别为字符串。

**修复**：使用 `.value`。

```python
# 修改前
result: dict[str, Any] = {
    "code": self.code,
    "message": self.message,
}

# 修改后
result: dict[str, Any] = {
    "code": self.code.value,
    "message": self.message,
}
```

**验证**：

```bash
python -c "
import json
from pg_mcp.models.errors import ErrorDetail, ErrorCode
d = ErrorDetail(code=ErrorCode.SECURITY_VIOLATION, message='test')
print(json.dumps(d.to_dict()))
"
```

应输出 `{"code": "security_violation", "message": "test"}`。

---

#### 6. 异步代码中改用 `asyncio.Lock`

**文件**：`src/pg_mcp/resilience/circuit_breaker.py`

**问题**：`CircuitBreaker` 使用 `threading.Lock`，在 async 事件循环中调用 `allow_request()` / `record_failure()` 时会阻塞事件循环。

**修复**：改为 `asyncio.Lock`，并把所有同步方法改为 async 方法（或在调用处使用 `asyncio.to_thread`）。推荐直接重构为 async 版本：

```python
# 修改前
from threading import Lock
self._lock = Lock()

# 修改后
import asyncio
self._lock = asyncio.Lock()
```

对应方法示例：

```python
async def allow_request(self) -> bool:
    async with self._lock:
        self._update_state()
        return self._state in (CircuitState.CLOSED, CircuitState.HALF_OPEN)
```

**影响面**：`orchestrator.py` 中 `_generate_sql_with_retry` 调用 `self.circuit_breaker.allow_request()` 需改为 `await`。

**验证**：

```bash
python -m pytest tests/unit/test_resilience.py -v
python -m pytest tests/unit/test_orchestrator.py -v
```

---

#### 7. 删除或接入 `MultiRateLimiter`

**文件**：`src/pg_mcp/server.py`、`src/pg_mcp/services/orchestrator.py`

**问题**：`server.py` 创建了 `_rate_limiter`，但 `QueryOrchestrator` 未使用它，限流失效。

**方案 A（推荐接入）**：将 `MultiRateLimiter` 注入 `QueryOrchestrator`，在 SQL 生成和执行时使用。

```python
# orchestrator.py __init__ 增加参数
rate_limiter: MultiRateLimiter | None = None,

# execute_query 中关键位置使用
async with self.rate_limiter.for_queries():
    results, total_count = await self.sql_executor.execute(generated_sql)
```

**方案 B（暂不接入则删除）**：删除 `server.py` 中 `_rate_limiter` 相关代码，避免误导。

**验证**：根据选择方案，确保 ruff 不再报未使用变量，且测试通过。

---

### 🟢 P2：改进与清理

#### 8. 清理根目录 `main.py`

**文件**：`main.py`

**问题**：根目录 `main.py` 是早期 demo（返回固定值 42 的加法服务器），与项目真实功能无关，容易误导新用户。

**修复方案**（二选一）：

- **方案 A**：删除 `main.py`。
- **方案 B**：将 `main.py` 改为真实入口的转发：

```python
from pg_mcp.__main__ import main

if __name__ == "__main__":
    main()
```

**验证**：

```bash
python main.py
# 或
python -m pg_mcp
```

两者行为一致。

---

#### 9. 修复 ruff `UP041` 告警

**文件**：`src/pg_mcp/db/pool.py`、`src/pg_mcp/server.py`

**问题**：使用了 `asyncio.TimeoutError`，Python 3.11+ 中可直接使用内置 `TimeoutError`。

**修复**：

```python
# db/pool.py 修改前
except asyncio.TimeoutError:

# 修改后
except TimeoutError:
```

```python
# server.py 修改前
except asyncio.TimeoutError:

# 修改后
except TimeoutError:
```

**验证**：

```bash
python -m ruff check src/pg_mcp/db/pool.py src/pg_mcp/server.py
```

---

#### 10. 修复 ruff `ASYNC109` 告警

**文件**：`src/pg_mcp/db/pool.py`

**问题**：异步函数使用 `timeout` 作为参数名，与 `asyncio.timeout` 冲突。

**修复**：重命名为 `timeout_seconds` 或 `seconds`。

```python
# 修改前
async def close_pools(pools: dict[str, Pool], timeout: float = 10.0) -> None:

# 修改后
async def close_pools(pools: dict[str, Pool], timeout_seconds: float = 10.0) -> None:
```

同步更新函数体内对该参数的引用。

**验证**：

```bash
python -m ruff check src/pg_mcp/db/pool.py
```

---

#### 11. 修复 mypy 类型错误

**文件**：`src/pg_mcp/services/sql_validator.py`、`src/pg_mcp/services/sql_generator.py`

**问题**：
- `sql_validator.py` 中 `_check_*` 方法参数声明为 `exp.Expression`，但实际传入 `statement` 类型为 `exp.Expr`，导致类型不匹配。
- `sql_generator.py` 中 `_extract_sql` 返回 `str | None`，但 `re.findall` 返回 `list[Any]`，未做类型断言。

**修复示例**：

```python
# sql_validator.py：统一参数类型为 exp.Expression
# sqlglot 的 exp.Expression 是 exp.Expr 的基类，通常可直接使用
# 若 mypy 仍报错，可显式 cast
from typing import cast

statement = cast(exp.Expression, parsed[0])
```

```python
# sql_generator.py：对正则结果做类型断言
matches: list[str] = re.findall(code_block_pattern, content, re.DOTALL | re.IGNORECASE)
# 或使用 cast
from typing import cast
matches = cast(list[str], re.findall(...))
```

**验证**：

```bash
python -m mypy src
```

应无错误。

---

#### 12. 修复 `server.py` 多余 `type: ignore`

**文件**：`src/pg_mcp/server.py`

**问题**：`async def lifespan(_app: FastMCP) -> AsyncIterator[None]:  # type: ignore[type-arg]` 中 `type: ignore` 已无用。

**修复**：删除注释。

```python
# 修改前
async def lifespan(_app: FastMCP) -> AsyncIterator[None]:  # type: ignore[type-arg]

# 修改后
async def lifespan(_app: FastMCP) -> AsyncIterator[None]:
```

---

#### 13. 为集成/E2E 测试补充 Mock 环境

**文件**：`tests/conftest.py`

**问题**：集成/E2E 测试直接依赖真实 PostgreSQL 和 OpenAI，无法在 CI 或无外部服务的环境中运行。

**建议**：
- 提供 `OPENAI_API_KEY` mock fixture（格式需通过校验，如 `sk-test`）。
- 使用 `unittest.mock` 或 `pytest-asyncio` 的 `AsyncMock` 模拟 `asyncpg.Pool` 和 `openai.AsyncOpenAI`。
- 将依赖外部服务的测试统一标记为 `@pytest.mark.integration`，并在 CI 中默认跳过。

示例 fixture：

```python
@pytest.fixture(autouse=True)
def mock_openai_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-mock-key")
```

**验证**：

```bash
# 默认运行时不执行集成测试
python -m pytest

# 显式运行集成测试
python -m pytest -m integration
```

---

## 推荐修复顺序

建议按以下顺序执行，每步后都运行单元测试，确保不引入回归：

1. **P0-1** 修复 FastMCP 导入（已做）。
2. **P0-2** 调整 Python 版本要求。
3. **P0-3** 修改默认日志格式。
4. **P0-4** 修复 `QueryResponse.to_dict` 重复定义。
5. **P1-5** 修复 `ErrorDetail.to_dict` 序列化。
6. **P1-6** 异步锁改造（需同步修改调用方）。
7. **P1-7** 接入或删除 `MultiRateLimiter`。
8. **P2-8** 清理 `main.py`。
9. **P2-9 / P2-10 / P2-11 / P2-12** 修复 ruff/mypy 告警。
10. **P2-13** 为测试补充 mock 环境。

---

## 验证命令汇总

```bash
# 1. 安装
pip install -e ".[dev]"

# 2. 代码风格
python -m ruff check src tests
python -m ruff format src tests

# 3. 类型检查
python -m mypy src

# 4. 单元测试
python -m pytest tests/unit/ -v

# 5. 覆盖率（目标 >=80%）
python -m pytest --cov=src --cov-report=html --cov-fail-under=80

# 6. 完整测试（需配置 PostgreSQL + OpenAI）
python -m pytest
```

---

## 附录：最小 `.env` 示例

```bash
# 数据库
DATABASE_HOST=localhost
DATABASE_PORT=5432
DATABASE_NAME=blog_small
DATABASE_USER=postgres
DATABASE_PASSWORD=postgres

# OpenAI
OPENAI_API_KEY=sk-your-real-key-here
OPENAI_MODEL=gpt-4o-mini

# 安全
SECURITY_ALLOW_WRITE_OPERATIONS=false
SECURITY_MAX_ROWS=10000
SECURITY_MAX_EXECUTION_TIME=30

# 可观测
OBSERVABILITY_METRICS_ENABLED=false
OBSERVABILITY_LOG_LEVEL=INFO
OBSERVABILITY_LOG_FORMAT=json
```

---

*修复计划基于 2026-10-08 的运行分析结果生成。*
