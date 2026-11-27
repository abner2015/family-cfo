# 飞书分层存储改造方案

## 问题

飞书多维表格免费版硬限制：

| 限制 | 免费版 | 商业专业版 |
|---|---|---|
| 单表行数 | 2,000 | 20,000 |
| 单个多维表格数据表数 | 100 | 300 |

当前状态（2026-11-27）：

```
FamilyCFO_交易明细   1625 行   ← 已用 81%，2027 年初必然撞线
```

按 2026 年 11 个月 1638 笔推算，年增约 1750 笔，**单表 12 个月后必然写不进去**。

## 方案：月度明细 + 年度汇总

### 表结构

```
FamilyCFO_交易明细_2026-01   ← 月度明细，每表约 150 行
FamilyCFO_交易明细_2026-02
...
FamilyCFO_交易明细_2026-11
FamilyCFO_年度汇总          ← 每年 1 行，12 列指标
FamilyCFO_分类年度汇总       ← 每年每分类 1 行，用于看结构变化
FamilyCFO_资产 / 负债 / 预算 / 目标 / 月度报告   ← 不分表，行数天然可控
```

### 行数预算

| 表 | 每表行数 | 说明 |
|---|---|---|
| 交易明细_YYYY-MM | ~150 | 单月笔数，远低于 2000 |
| 年度汇总 | 1/年 | 20 年才 20 行 |
| 分类年度汇总 | ~15/年 | 20 年才 300 行 |

单多维表格数据表数：11（月明细）+ 4（其他）= 15 张，留足 85 张余量 ≈ **7 年不需分表**。

### 数据流

```
SQLite (权威主库, 1638 笔)
   │
   ├─→ 飞书: 按月分表的明细 + 年度汇总
   │
   └─→ 本地: exports/ 目录 CSV + XLSX 年度汇总 + 备份
```

本地 SQLite 永远是权威源，飞书和本地导出都是它的投影。**任何一侧坏了都能重建**。

## 实现要点

### 1. schema.py 支持动态表名

新增 `TableSpec.periodic` 标记，明细类实体按 `YYYY-MM` 派生表名：

```python
@dataclass
class TableSpec:
    ...
    periodic: str | None = None   # "month" | None
```

### 2. sync.py 分表推送

`_payloads_transactions` 改为按月分组推送，每组一个 table_id。
表名映射持久化到 `system_config`，键 `feishu.table_id.transactions.2026-11`。

### 3. 新增年度汇总实体

`_payloads_annual_summaries`：每年一行，收入/支出/结余/储蓄率/笔数/净资产等。
`_payloads_category_annual_summaries`：每年每分类一行。

### 4. 迁移

现有 `FamilyCFO_交易明细`（1625 行）需要拆到 11 张月表。
用 `batch_create` + 按月分组重建，然后删旧表。

### 5. 本地兜底

`scripts/local_export.py`：导出 CSV + XLSX（含年度汇总页），跑体检。

## 风险与回滚

- 本地 SQLite 不动 → 任何飞书侧失败都可重跑
- 飞书表删除用 `records/batch_delete`，旧表先改名而非直接删
- 幂等：`client_token` 按实体+ID 生成，重跑不会产生重复行
