# Family CFO 工作流参考

## 1. 导入决策树

```
文件 → 扩展名检查 → SHA-256 是否已导入？
                         ├─ 是 → status=duplicate（不重复入账）
                         └─ 否 → 读取表头 → 适配器评分
                                   ├─ ≥0.45 → 平台适配器解析
                                   └─ <0.45 → mapping_required（返回列建议）
```

- 微信/支付宝/银行适配器各自独立，不共享字段假设。
- 表头前允许出现平台导出的说明行；表头后遇到“共 N 笔”等脚注停止。
- 单行失败不会中断整批，错误写入 `data/failed/<batch>.errors.json`。
- 原始文件复制到 `data/archive/<hash12>_<name>` 并设为只读；收件箱文件移到 `data/processed/`。

## 2. 对账规则

| 场景 | 判定 | 结果 |
| --- | --- | --- |
| 同平台同 `external_id` | 精确重复 | 不入账，计入 `duplicates` |
| 同平台同金额/日期/商户、不同流水号 | 疑似重复 | 入账但 `stats_included=false`、`pending_review` |
| 跨平台同金额/日期/商户（±2 天） | 保守疑似重复 | 仅标记，不删除 |
| 一出一进、金额相同、账户不同、±3 天 | 内部转账 | 双边 `txn_type=transfer`，排除收支 |
| 转入信用卡账户 | 信用卡还款 | 标记 `credit_card_repayment` |
| 入账含“退款/退货/冲正”，90 天内同商户且金额 ≤ 原支出 | 退款 | 关联 `refunds`，继承原分类 |
| 无法匹配的退款 | 未关联 | `pending_review`，阻塞关账 |

## 3. 月度结算

```
COLLECTING ──有账单──> IMPORTING ──> RECONCILING ──> READY_TO_CLOSE ──> CLOSED
                                        │
                                        └──有阻塞──> REVIEW_REQUIRED
```

关账检查项：

1. `sources_present`：每个应纳入渠道有批次或交易，或被标记 missing（必须给原因）。
2. `import_errors`：`failed_rows == 0`。
3. `pending_review`：无待确认，或用户授权 `--pending-policy suspend`。
4. `duplicates`：无未处理重复候选。
5. `refund_links`：无未关联退款。
6. `integrity`：无 error 级完整性问题。
7. `amount_validation`：SQL 聚合与 Python 汇总的收支一致。

关账写入 `financial_snapshots`（月末净资产）；`settlement_adjustments` 记录覆盖原因。
重开：`status=RECONCILING`、`reopened_count+1`，历史快照保留。

## 4. 常用 JSON 取值

```bash
family-cfo --json analyze --month 2026-10
```

- 收入/支出/结余：`summary.income_minor` / `expense_minor` / `net_minor`（单位：分）
- 储蓄率：`summary.savings_rate`（0–1 的小数）
- 净资产：`balance_sheet.net_worth_minor`
- 应急资金：`metrics[key=emergency_months].raw`
- 分类支出：`categories[]`，字段 `slug`、`amount_minor`、`direction`
- 数据质量：`data_quality.estimated_assets`、`stale_valuations`

## 5. 自然语言记账

`record "午餐 35 元"` 只生成**建议**：金额、方向、日期、分类、置信度、警告。
默认必须确认后写入；`--yes` 仅供自动化。未匹配分类 → `pending_review`。

金额解析支持 `¥1,234.56`、`35元`、`35 块`；不支持的表述直接报错，不做猜测。

## 6. 退出码

| 码 | 含义 |
| --- | --- |
| 0 | 成功 |
| 1 | 通用错误（含结算阻塞、飞书错误） |
| 2 | 参数/校验错误（含 mapping 缺失） |
| 3 | （保留） |
