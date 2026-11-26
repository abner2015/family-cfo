# 指标口径

所有指标由 `family_cfo.finance.metrics` 计算，金额单位为分，输出时转元。
报告中每个指标都带 `formula` 与 `sources`；无法计算时 `status=unavailable`，不填 0。

| key | 名称 | 公式 | 数据来源 |
| --- | --- | --- | --- |
| month_income | 月度收入 | Σ inflow & txn_type=income | transactions |
| month_expense | 月度支出 | Σ(expense+fee+reversal) − Σ refund | transactions |
| month_net | 月度结余 | income − expense | transactions |
| savings_rate | 储蓄率 | net / income；income=0 时不可用 | transactions |
| total_assets | 总资产 | 账户资产 + 非账户资产 + 投资市值 | accounts, assets, investment_positions |
| total_liabilities | 总负债 | 负债余额 + 信用卡欠款 | liabilities, accounts |
| net_worth | 净资产 | 总资产 − 总负债 | 同上 |
| debt_ratio | 资产负债率 | 总负债 / 总资产 | assets, liabilities |
| liquid_ratio | 流动资产比例 | 流动资产 / 总资产 | accounts, assets |
| emergency_months | 应急资金覆盖月数 | 流动资产 / 近 3 个月平均必要支出 | accounts, transactions |
| fixed_expense_ratio | 固定/必要支出比例 | 必要支出 / 总支出 | transactions, categories |
| debt_service_ratio | 债务负担率 | Σ 月供 / 近 3 个月平均月收入 | liabilities, transactions |
| investment_allocation | 投资配置 | 各产品类型市值 / 投资总市值 | investment_positions |
| net_worth_change | 年度净资产变化 | 当前净资产 − 12 个月前快照 | financial_snapshots |
| goal_completion | 目标完成率 | Σ min(当前, 目标) / Σ 目标 | financial_goals |

## 口径细节

- **不计入收支**：内部转账、信用卡还款、充值、提现、投资买入/卖出、借贷。
- **退款**：按退款发生月份冲减支出；跨月退款在退款月体现，不追溯修改已关账月份。
- **必要支出**：分类 `is_essential=true`（餐饮、日用品、住房、水电、交通、医疗、教育、保险）。
- **流动资产**：银行/现金/支付平台余额 + `is_liquid` 资产 + 可快速变现投资；
  **自住房永不计入流动资产**。
- **应急资金**：分母取最近 3 个有支出月份的均值；无数据时 `unavailable`。
- **投资收益**：
  - 已实现 = 卖出净额 − 卖出数量 × 平均成本；
  - 未实现 = 用户提供市值 − 剩余成本；无市值时不计算；
  - 分红单列为现金流，不冲减持仓成本；
  - 手续费计入成本（买入）或扣减卖出所得。
- **年度净资产变化**：需要 12 个月前的快照；缺失时 `unavailable`，不推算。
- **目标完成率**：单目标封顶 100%，避免超额储蓄掩盖其他目标缺口。
