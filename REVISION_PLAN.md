# pg-mcp 第二次作业整改方案（评审意见逐条落实）

> 批改意见对照工程实现的具体接线方案。目标：组件全接线，248 测试全绿，演示链路不受影响。

## 批改意见 → 问题清单

| # | 批改点 | 现状核查结论 |
|---|--------|--------------|
| 1 | settings 仅单库 | `Settings.database` 单个 `DatabaseConfig`；`server.py:99` 注释 "single database configuration" |
| 2 | SQLValidator 硬编码 | `server.py:162-167`: `blocked_tables=None / blocked_columns=None / allow_explain=False` |
| 3 | 无 retry 模块 | 无 `resilience/retry.py`；重试循环藏在 orchestrator 内部私有方法 |
| 4 | 熔断未实例化 | orchestrator:102 已实例化 CircuitBreaker，但接线对外不可见（需保留并明示） |
| 5 | metrics 未传入 orchestrator | `MetricsCollector` 单例存在但零调用（"定义了没用"） |
| 6 | request_context 未使用 | tracing.py 完整实现，orchestrator 却手写 `uuid.uuid4()` |
| 7 | generate() 丢弃 usage | `sql_generator.py` 拿到 `response.usage` 后未读，tokens 恒 0 |
| 8 | tracing/rate_limiter/circuit_breaker 未接线 | tracing 0 调用；rate_limiters 传参存在但未在 execute_query 中生效 |

## 改动总览（7 个既有文件 + 2 个新文件）

### 1. `src/pg_mcp/config/settings.py` — 多库 + 安全校验配置
- `Settings.databases: dict[str, DatabaseConfig]`（新增）
  - 校验器保证兼容：若 `DATABASES` 未配置，自动把旧的 `DATABASE_*` 单库（`database` 字段）包装成 `{name: DatabaseConfig}` dict
  - 支持进阶多库 `DATABASES=postgres,analytics`（进阶 env 配置）
- `SecurityConfig` 新增字段（SECURITY_ env_prefix 生效）：
  - `blocked_tables: list[str] = []`
  - `blocked_columns: list[str] = []`
  - `allow_explain: bool = False`
- `ResilienceConfig` 增加：`retry_enabled: bool = True`（显式 retry 开关）

### 2. `src/pg_mcp/config/__init__.py` — 导出新配置项

### 3. `src/pg_mcp/db/pool.py` — 池参数化
- `create_pool(dsn, min_size, max_size, command_timeout)` 把硬编码 30 改为 `db_config.command_timeout` 参数

### 4. `src/pg_mcp/observability/tracing.py` — 修复 trace_async 并将被使用
- `trace_async` 的实现依赖 setLogRecordFactory + 指针遮蔽，不适合高并发（全局 factory 替换有竞态）。
- **改为基于 contextvars 的 LogRecordFactory**（安全实现：factory 从 `_request_id_var.get()` 读当前值，而非闭包缓存）——注意 `trace_async` 将不再被 orchestrator 使用，因为 orchestrator 直接用 `request_context()`；`trace_async` 保留给未来其他异步入口。

### 5. `src/pg_mcp/observability/logging.py` — 修复 JSONFormatter
- `JSONFormatter.format` 已输出 `request_id`（如有）→ 确认即可（无改动）
- 保证 `request_id` 从 ContextVar 读取（TracingLogger 内部已实现，但 JSONFormatter 目前只读 `record.request_id`）→ 修改为 ContextVar 优先，兼容 extra 传参

### 6. 新建 `src/pg_mcp/resilience/retry.py` — 独立重试模块
```python
class RetryExecutor:
    def __init__(self, config: ResilienceConfig): ...
    async def execute(self, func: Callable[..., Awaitable[T]], *,
                     on_retry: Callable[[Exception, int], None] | None = None,
                     retryable: Callable[[Exception], bool] | None = None) -> T:
        # 指数退避：delay = retry_delay * (backoff_factor ** attempt)
```
- orchestrator 内部失败反馈重试逻辑改为组合 `RetryExecutor`（校验失败 重试走 `on_retry` 钩子），保留"错误反馈注入下一轮 generate"的语义

### 7. `src/pg_mcp/services/sql_generator.py` — 读取 token usage
- `generate()` 返回 `tuple[str, int | None]`：`usage = response.usage; tokens = usage.total_tokens if usage else None`
- `SQLGenerator.generate_with_tokens()` 不需要——直接改签名

### 8. `src/pg_mcp/services/orchestrator.py` — 全链路接线
- `__init__` 新增 `metrics: MetricsCollector | None = None`
- 删除手写 uuid；`execute_query` 整体包入 `request_context()`
- `_generate_sql_with_retry` 改为组合 `RetryExecutor`：
  - `func` = 生成+校验闭包
  - `retryable` = 校验失败或 LLM 可重试错误
  - 每次成功/失败调用 `circuit_breaker.record_success/record_failure`（保留原逻辑）
- 全部接入 metrics：
  - `increment_query_request(status=success/error, database=db)`
  - `query_duration.time()` 全流程耗时
  - `observe_db_query_duration(execution_time_ms/1000)`
  - `increment_sql_rejected(reason)` 验证失败时
  - `increment_llm_call("generate_sql")` / `observe_llm_latency(...)` / `increment_llm_tokens(...)`
- rate_limiters 接线：
  - `execute_query` 全程包 `rate_limiters["query"].acquire()`
  - `_generate_sql_with_retry` 内包 `rate_limiters["llm"].acquire()`
  - 用 `AsyncExitStack` 管理 acquire/release 生命周期
- `tokens_used` 真实读取 generate() 返回值

### 9. `src/pg_mcp/server.py` — lifespan 全面改造
- 多库支持：`for name, cfg in _settings.databases.items(): _pools[name] = await create_pool(cfg)`
- SQLValidator 参数全部来自配置：
  ```python
  SQLValidator(
      config=_settings.security,
      blocked_tables=_settings.security.blocked_tables or None,
      blocked_columns=_settings.security.blocked_columns or None,
      allow_explain=_settings.security.allow_explain,
  )
  ```
- SQLExecutor 每库创建，orchestrator 接受主库 executor
- 删除过期注释："For single database configuration..." 及重复的 `global` 行、重复的 `# 7.` 步骤编号
- `start_auto_refresh` 从注释代码改为正式启用（受 CacheConfig.enabled 控制）
- metrics 传给 orchestrator

## 不动的部分
- `main.py`、`web_demo`、`mock_openai`、`fixtures` 全部无改动
- `models/`、`cache/`、`observability/logging.py` 主逻辑不动
- 248 个单测全部通过为硬性要求（test_orchestrator 需适配改动）

## 验收标准
1. 248 个单元测试全部通过（test_orchestrator 适配后）
2. `ruff check src` 无错误
3. `mypy src` 无错误
4. 多库配置示例：
   ```bash
   DATABASES=postgres,analytics
   DATABASES_POSTGRES__HOST=...   # 可选进阶、单库默认即可
   ```
5. 演示链路照常工作（可选手动验证）
