# 架构

## 分层

```
┌──────────────────────────────────────────────────────────┐
│ Hermes Skill / CLI (thin)                                │
│  skills/family-cfo/SKILL.md  ·  family_cfo/cli.py        │
└───────────────┬──────────────────────────────────────────┘
                │
┌───────────────▼──────────────────────────────────────────┐
│ Services                                                  │
│  ImportService  TransactionService  Reconciler            │
│  SettlementService  BudgetService  AssetService           │
│  LiabilityService  InvestmentService  PlanningService     │
│  FeishuSyncService  Report builders                       │
└───────────────┬──────────────────────────────────────────┘
                │
┌───────────────▼──────────────────────────────────────────┐
│ Domain / persistence                                      │
│  SQLAlchemy 2.x models · Alembic · audit · integrity      │
│  SQLite (WAL, foreign_keys=ON)                            │
└──────────────────────────────────────────────────────────┘
```

- CLI 与 Skill 不包含业务计算；报告只消费 `finance.metrics` 的输出。
- 导入器只做解析（纯函数，输入 `RawTable`，输出 `DraftTransaction`），
  持久化与去重由 `ImportService` 统一处理。

## 账单导入流水线

```
扫描 inbox
  → SHA-256 文件去重
  → 读取 CSV/XLSX/XLS（编码探测、表头定位）
  → 适配器评分（微信 / 支付宝 / 银行）
      ├─ <0.45 → mapping_required + 列建议
      └─ ≥0.45 → 解析为 DraftTransaction
  → 账户解析（平台默认账户或用户指定）
  → 交易落库（external_id 唯一 + dedupe_key 近似去重）
  → 规则分类（未命中 → pending_review）
  → 原始行写入 raw_transactions（保留审计）
  → 批次统计 + 错误报告
  → 原文件只读归档；inbox 文件移入 processed/failed
```

## 对账

`Reconciler.run()` 按顺序执行：

1. 跨平台重复候选（金额、日期、商户相似；仅标记）；
2. 内部转账/信用卡还款双边匹配（金额、日期、账户不同、转账语义）；
3. 退款关联（90 天内、金额 ≤ 原支出、商户相似）；
4. 统计待确认数量。

所有动作写审计；不删除任何交易。用户通过 `tx resolve` 或 `tx fix` 收尾。

## 月度结算

```
COLLECTING ──账单──> IMPORTING ──> RECONCILING ──> READY_TO_CLOSE ──> CLOSED
                                        │
                                        └─阻塞─> REVIEW_REQUIRED
```

- 关账前校验 7 项（渠道、导入错误、待确认、重复、退款、完整性、金额双算）；
- 允许显式覆盖（`--reason`）或待确认暂挂（`--pending-policy suspend`），均留审计；
- 关账写入月末 `financial_snapshots`；
- 重开只改状态并记录 `settlement_adjustments`，不改历史快照。

## 飞书同步

- 客户端：token 缓存（提前 5 分钟刷新）、令牌失效重试一次、10 req/s 限流、
  可重试错误指数退避（0.5s→8s）、错误码映射。
- 推送：本地构建字段 → `payload_hash` → `sync_records` 判断新增/更新/跳过；
  远端 `last_modified_time` 晚于本地且哈希不同 → 冲突，不覆盖。
- 回写：仅 `goals`、`budgets`，Pydantic 校验后写库并审计；预算回写生成新版本。
- 测试：`MockFeishuServer` 实现真实 REST 形状（分页、字段、批量），
  走完整客户端代码路径。

## 关键设计决策

| 决策 | 原因 |
| --- | --- |
| 金额存整数分 | 避免浮点误差；SQLite 无原生 DECIMAL |
| 数量/价格用 `DecimalText` | SQLite 会把 Numeric 转浮点；文本保留精度 |
| 单表 `transactions` + `txn_type` | 家庭规模下比多表继承更易查询与迁移 |
| 初始迁移物化 metadata | v0.1 单仓库，避免手写 26 张表与模型漂移；后续显式 revision |
| 软删除 + 审计 | 财务数据可追溯；关账后重开有记录 |
| SQLite + WAL | 单机、零运维、事务与外键；备份用 Online Backup API |
| 飞书只做展示 | 权限最小化；本地账本不受同步失败影响 |
