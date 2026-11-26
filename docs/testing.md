# 测试

```bash
pytest -q
pytest -q tests/test_feishu.py
pytest -q -k "settle or refund"
```

## 覆盖矩阵（对应验收要求）

| # | 要求 | 测试 |
| --- | --- | --- |
| 1 | 数据库初始化 | `test_database.py::test_initialize_creates_schema_and_seed_data` |
| 2 | 交易增删改查 | `test_transactions.py::test_create_read_update_and_soft_delete` |
| 3 | CSV 导入 | `test_importers.py::test_import_wechat_bill` |
| 4 | XLSX 导入 | `test_importers.py::test_import_xlsx` |
| 5 | 重复文件检测 | `test_importers.py::test_duplicate_file_detected_by_hash` |
| 6 | 跨平台重复识别 | `test_transactions.py::test_cross_platform_duplicate_is_conservative` |
| 7 | 内部转账识别 | `test_transactions.py::test_internal_transfer_matching` |
| 8 | 退款处理 | `test_transactions.py::test_refund_linking_and_category_copy` |
| 9 | 信用卡还款 | `test_transactions.py::test_credit_card_repayment_matching` |
| 10 | 收支分类 | `test_transactions.py::test_classifier_keyword_and_unknown` |
| 11 | 月度统计 | `test_finance.py::test_period_summary_income_expense_refund` |
| 12 | 储蓄率 | 同上 + `test_savings_rate_undefined_without_income` |
| 13 | 净资产计算 | `test_finance.py::test_balance_sheet_and_net_worth` |
| 14 | 预算执行率 | `test_finance.py::test_budget_versioning_and_execution` |
| 15 | 投资收益计算 | `test_finance.py::test_investment_pnl_and_cashflow_separation` |
| 16 | 月度结算状态 | `test_settlement.py`（全文件） |
| 17 | 数据缺失检查 | `test_settlement.py::test_importing_wechat_satisfies_one_expected_source` |
| 18 | 备份恢复 | `test_database.py::test_backup_and_restore_roundtrip` |
| 19 | 飞书 API Mock | `test_feishu.py::test_push_transactions_and_skip_unchanged` |
| 20 | 同步失败重试 | `test_feishu.py::test_retry_on_rate_limit` |
| 21 | 敏感信息脱敏 | `test_cli_skill.py::test_redaction_masks_secrets_and_accounts` |
| 22 | Skill 安装与路径 | `test_cli_skill.py::test_skill_script_runs_from_copied_directory` |

## 测试数据

`tests/fixtures/`：

- `wechat_sample.csv`：含退款对、充值、信用卡还款、转账；
- `alipay_sample.csv`：含余额宝转入、退款；
- `bank_sample.csv`：收入/支出分列；
- `generic_unknown.csv`：触发字段映射。

全部为**合成数据**，不含真实账户或个人信息。XLSX 用例在测试运行时由 CSV 生成，
不把二进制样例提交进仓库。

## 测试原则

- 不使用真实网络：飞书通过 `httpx.MockTransport`；
- 不依赖系统时间：周期参数显式传入；
- 金额断言使用整数分或 `Decimal`，不用浮点相等；
- 断言失败信息保留原始输出，便于定位。
