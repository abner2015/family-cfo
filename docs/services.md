# 服务接口

Python 调用方（CLI、Skill 脚本、未来 MCP）统一通过这些服务访问账本。
所有服务接收一个已打开的 `Session` 与 `Settings`，不自行创建连接。

## 导入

```python
ImportService(db, settings)
  .import_file(path, platform="auto", account=None, mapping=None,
               mapping_name=None, dry_run=False) -> ImportReport
  .scan_inbox(dry_run=False, directory=None) -> list[ImportReport]
  .detect(table, platform="auto") -> (adapter, score)
```

`ImportReport`：`status / imported / duplicates / failed / errors / period / archive_path`。

适配器协议：

```python
class BillAdapter(Protocol):
    name: str
    platform: str
    def score(self, table: RawTable) -> float: ...
    def parse(self, table, *, default_currency="CNY") -> AdapterResult: ...
```

新增平台只需实现协议并注册到 `ImportService.default_adapters()`。

## 交易

```python
TransactionService(session, settings)
  .create(...) -> CreateResult(transaction, duplicate, duplicate_kind)
  .get(id) / .list(period=..., status=..., only_review=...)
  .categorize(txn_id, category_id, create_rule=False)
  .soft_delete(txn_id, reason=...) / .restore(txn_id)
  .create_category(name, kind, ...) / .list_categories(kind=None)
  .add_rule(pattern, category_id, priority=..., match_type="keyword")
  .reclassify_pending() -> int
```

## 对账

```python
Reconciler(session, settings, period="YYYY-MM")
  .run() -> ReconcileResult
  .confirm_duplicate(txn_id, distinct=False)
```

## 结算

```python
SettlementService(session, settings)
  .ensure(period) / .get(period)
  .mark_source(period, source, status="missing", reason=...)
  .evaluate(period) -> SettlementEvaluation
  .close(period, override_reason=None, pending_policy=None)
  .reopen(period, reason=...)
```

## 财务

```python
period_summary(session, household_id, period) -> PeriodSummary
balance_sheet(session, household_id, as_of=..., settings=...) -> BalanceSheet
build_metrics(session, settings, period=...) -> dict
capture_snapshot(session, household_id, as_of=..., settings=...)
BudgetService(session, settings).set_budget(...) / .execution(period)
AssetService(session, settings).add(...) / .value(...) / .summary()
LiabilityService(session, settings).add(...) / .record_payment(...) / .amortization(...)
InvestmentService(session, settings).record(...) / .revalue(...) / .portfolio()
PlanningService(session, settings).add_goal(...) / .plan(annual_return=...)
```

## 飞书

```python
FeishuClient(settings, transport=None)
  .status() / .healthcheck()
  .list_tables() / .create_table() / .list_fields() / .create_field()
  .search_records() / .list_records() / .get_record()
  .create_record() / .update_record() / .batch_create() / .iter_records()

FeishuSyncService(db, settings, client=None)
  .ensure_tables() -> {schema_key: table_id}
  .push(entity, dry_run=False) -> SyncReport
  .pull(entity) -> SyncReport        # goals | budgets
  .conflicts() -> list[dict]
  .resolve(entity, entity_id, strategy="local|remote")
```

## 错误

所有服务抛 `FamilyCFOError` 子类，带 `code` 与 `detail`：

`ConfigError`, `ValidationError`, `MoneyError`, `ImportError_`, `UnknownFormatError`,
`MappingRequiredError`, `ReconciliationError`, `SettlementError`, `BackupError`,
`FeishuError`, `FeishuNotConfigured`, `FeishuAuthError`, `FeishuApiError`, `ConflictError`。

CLI 将 `code` 映射为退出码；Skill 按 `code` 决定下一步，不解析自然语言错误。
