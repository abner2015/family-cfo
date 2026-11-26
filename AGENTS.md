# AGENTS.md — Family CFO 开发约定

本文件面向在本仓库工作的 AI/人类开发者。改动前先读，交付前逐条自检。

## 项目定位

本地优先的家庭财务账本。SQLite 是唯一权威数据源；飞书多维表格是展示/协作层；
Hermes Skill 是交互入口。**不提供转账、支付、证券交易等对外金融能力。**

## 常用命令

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

family-cfo init
family-cfo doctor
pytest -q
pytest tests/test_importers.py -q
```

## 不可破坏的约束

1. **金额**：数据库只存整数分；计算只用 `Decimal`；`float` 仅允许出现在
   pandas 读入边界与 Excel 展示层。
2. **多币种**：`amount_minor`、`currency`、`fx_rate`、`amount_base_minor` 必须同时正确；
   无汇率时挂起，不得默认 1:1。
3. **不编造**：不得生成不存在的交易、分类、估值、行情、测试结果或飞书连接状态。
   无法计算时返回 `unavailable` / `null`，不填 0。
4. **去重保守**：跨平台只标记，不自动删除；退款/转账必须有证据。
5. **审计**：删除、改余额、关账、重开、回写、估值必须写 `audit_logs`；删除/关账/重开需原因。
6. **软删除**：交易不得物理删除。
7. **原始账单**：只读归档（0444），不修改、不覆盖。
8. **凭证**：不读取/打印 `.env`、App Secret、token；日志必须经过 `core.logging.redact`。
9. **飞书失败隔离**：同步异常只写 `sync_records`，不得回滚或破坏本地账本。
10. **定时任务**：默认关闭，`install` 必须 `--yes`。

## 代码结构

- `core/`：纯函数与配置，禁止依赖上层。
- `database/models.py`：唯一 schema 定义；改字段必须同时新增 Alembic revision。
- `importers/`：只解析，不写库；写库由 `importers/service.py` 负责。
- `transactions/`：交易 CRUD、分类、对账；不直接拼 SQL（除已注释的聚合）。
- `finance/metrics.py`：所有指标的唯一实现；报告只消费它的输出。
- `integrations/feishu/`：客户端（重试/限流/token）、schema、sync、mock。
- `cli.py`：只做参数解析、调用服务、输出；业务逻辑不得写在 CLI。

## 修改 schema 的流程

1. 改 `database/models.py`；
2. 新增 `migrations/versions/000X_xxx.py`（显式 `op.add_column` 等，启用 `render_as_batch`）；
3. 更新 `skills/family-cfo/references/data-model.md`；
4. 补迁移/兼容测试；
5. 不要修改 `0001_initial` 已发布的内容。

## 测试要求

- 新功能必须有断言，不允许“只建测试文件”。
- 至少覆盖：正常路径、边界、错误路径、审计/幂等。
- 账单解析用 `tests/fixtures/` 合成文件；禁止真实账单入库。
- 飞书测试使用 `MockFeishuServer`，禁止访问真实 API。
- 提交前运行 `pytest -q`，全绿。

## 输出与文档

- 面向用户的金额带币种与期间，例如 “2026-10 支出 ¥2,764.30”。
- 报告/规划必须区分 `actual / estimate / assumption / unavailable`。
- 新增 CLI 命令需同步更新 `README.md` 与 `SKILL.md` 的命令表。
- 新增环境变量需更新 `.env.example` 与 `docs/operations.md`。

## 完成定义（Definition of Done）

- [ ] `pytest -q` 全绿
- [ ] `family-cfo doctor` 通过
- [ ] `family-cfo init` + 三个样例账单导入 + `settle --status` 可复现
- [ ] 无绝对开发机路径进入 `skills/`
- [ ] 无密钥、真实账单、个人隐私进入仓库
- [ ] 文档与命令一致
