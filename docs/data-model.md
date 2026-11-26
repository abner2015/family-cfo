# 数据模型

完整 ER 图、字段语义与不变量见
[`skills/family-cfo/references/data-model.md`](../skills/family-cfo/references/data-model.md)。

## 快速索引

| 领域 | 表 |
| --- | --- |
| 家庭 | `households`, `family_members` |
| 账户 | `accounts` |
| 导入 | `import_batches`, `raw_transactions` |
| 交易 | `transactions`, `transaction_categories`, `category_rules` |
| 对账 | `transfer_matches`, `refunds` |
| 资产 | `assets`, `asset_valuations` |
| 负债 | `liabilities`, `liability_payments` |
| 预算 | `budgets`, `budget_items` |
| 投资 | `investment_products`, `investment_transactions`, `investment_positions` |
| 规划 | `financial_goals`, `financial_snapshots` |
| 结算 | `settlements`, `settlement_adjustments` |
| 运维 | `sync_records`, `audit_logs`, `system_configs` |

## 三条最重要的规则

1. `*_minor` 是整数分，永远为正；方向看 `direction`。
2. 原币 `amount_minor` 与本位币 `amount_base_minor` 分列，汇率单独保存。
3. 历史只追加：估值、预算版本、快照、审计都不原地覆盖。

## 迁移

```bash
alembic upgrade head
alembic revision --autogenerate -m "add xxx"
```

SQLite 使用 `render_as_batch=True`。`0001_initial` 通过 metadata 建表，后续 revision
必须显式写 `op.*`。
