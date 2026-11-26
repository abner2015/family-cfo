# Family CFO

面向家庭的长期财务管理 Agent：本地 SQLite 账本 + 自动账单归集 + 月度结算 + 财务分析 +
飞书多维表格同步，以 **Hermes Agent Skill** 形式交付。

> 设计原则：**不要求每天记账**。按周/按月批量导入微信、支付宝、银行账单，程序自动去重、
> 分类、识别退款与内部转账，只把少量异常交给用户确认。
>
> 所有金额与比率由 Python `Decimal` / 整数分计算，**不存在由大模型心算的财务结论**。
> 未导入的渠道不会被当作零支出；未配置的飞书会明确显示“未连接”。

---

## 1. 功能

- **账单导入**：CSV / XLSX / XLS；微信、支付宝、银行独立适配器；未知格式支持“字段映射确认”。
- **交易处理**：唯一 ID、跨平台保守去重、退款关联、转账双边匹配、信用卡还款、规则分类。
- **账户**：现金、银行卡、支付平台、信用卡、投资账户；支持多成员、多币种（原币与本位币分离）。
- **收支**：完整分类体系、可自定义规则、自然语言记账（补充入口）。
- **资产**：成本/估值/估值日期/来源/流动性；估值历史追加，不覆盖；自住房不算流动资产。
- **负债**：房贷/车贷/消费贷/信用卡；利率、月供、剩余期限、还款计划与还款记录。
- **预算**：月度/年度、分类/成员、固定/可变；版本化，执行率与超支提醒。
- **投资**：产品、买卖、分红、手续费、持仓、已实现/未实现收益；无行情时不虚构市值。
- **规划**：应急资金、教育、养老、住房、大额消费；输出区分实际/估算/假设。
- **分析**：15+ 指标，全部带公式与数据来源。
- **健康诊断**：应急金、储蓄率、负债率、债务负担、流动性、持仓集中度规则评分。
- **自然语言问答**：`ask "这个月教育支出多少"` 确定性解析，不依赖模型心算。
- **结算**：`COLLECTING → … → CLOSED` 状态机，关账门槛、覆盖审计、重开留痕。
- **飞书**：建表、增量推送、失败重试、限流、冲突检测、目标/预算校验回写。
- **报告**：Markdown / Excel 月报与年报。
- **安全**：只读归档、软删除、审计日志、自动备份/恢复校验、日志脱敏。

## 2. 环境要求

- Python **3.11+**（推荐 3.12）
- macOS / Linux
- 可选：飞书自建应用 + 多维表格（仅同步时需要）

## 3. 安装

```bash
cd family-cfo
python3 -m venv .venv && source .venv/bin/activate   # 或使用 uv
pip install -e ".[dev]"

cp .env.example .env        # 按需填写飞书凭证；不填也可本地使用
family-cfo init
family-cfo doctor
```

`init` 会创建 `data/`、SQLite 库、默认 20 个分类、5 个账户，并执行 Alembic 迁移。

## 4. 五分钟上手

```bash
# 1) 预演导入（不写库）
family-cfo import tests/fixtures/wechat_sample.csv --dry-run

# 2) 正式导入
family-cfo import tests/fixtures/wechat_sample.csv
family-cfo import tests/fixtures/alipay_sample.csv
family-cfo import tests/fixtures/bank_sample.csv

# 3) 对账：重复、转账、退款、还款
family-cfo reconcile --month 2026-10

# 4) 查看待确认与指标
family-cfo tx list --month 2026-10 --review
family-cfo analyze --month 2026-10

# 5) 月度结算
family-cfo settle --month 2026-10 --collect
family-cfo settle --month 2026-10 --status
family-cfo settle --month 2026-10 --mark-missing bank --reason "本月无银行流水"
family-cfo settle --month 2026-10 --close

# 6) 报告
family-cfo report --month 2026-10 --format md,xlsx
```

可选：先用合成数据体验完整流程（仅空账本，数据均以“演示”开头）：

```bash
family-cfo demo --yes
family-cfo health
family-cfo ask "这个月教育支出多少"
```

也可以把账单放进 `data/inbox/`，执行 `family-cfo scan`：
成功文件移到 `processed/`，失败文件与错误报告在 `failed/`，原始文件只读归档到 `archive/`。

## 5. 命令行总览

| 命令 | 作用 |
| --- | --- |
| `init` | 初始化数据库、分类、账户 |
| `doctor` | 环境/配置/完整性检查 |
| `import <file\|dir>` | 导入账单（`--dry-run`、`--platform`、`--mapping-name`） |
| `scan` | 扫描 `data/inbox` |
| `reconcile` | 去重、转账、退款、还款识别 |
| `settle` | 归集/状态/关账/重开/标记缺失渠道 |
| `analyze` | 月度指标、分类、趋势；`--week` 周报 |
| `health` | 财务健康诊断（规则评分，缺数据明确标注） |
| `ask "..."` | 自然语言财务问答（确定性解析） |
| `report` | 月报/年报（md/xlsx） |
| `record "午餐 35 元"` | 自然语言记账（默认需确认） |
| `tx list/fix/retype/delete/restore/resolve` | 交易查询、改分类/类型、软删除、重复处理 |
| `category add/list` | 自定义分类与关键词 |
| `account list/add/balance` | 账户 |
| `asset list/add/value/history/review` | 资产与估值 |
| `liability list/add/pay/schedule/balance` | 负债与还款 |
| `budget set/show/history` | 预算与版本 |
| `goal add/list/progress` / `plan` | 目标与规划 |
| `invest product/buy/sell/dividend/value/portfolio` | 投资 |
| `feishu status/ensure-tables/sync/pull/conflicts/resolve` | 飞书 |
| `backup create/list/verify/restore/prune` | 备份恢复 |
| `automation list/install/run` | 定时任务（默认不启用） |
| `db check/config/mapping` | 完整性与配置 |
| `demo --yes` | 写入合成示例数据（仅空账本） |

全局选项：`--root`、`--db`、`--json`、`--verbose`。`--json` 可放在子命令前或后，
例如 `family-cfo --json analyze --month 2026-10` 或 `family-cfo analyze --month 2026-10 --json`。

## 6. 飞书同步

1. 创建自建应用，申请 `bitable:app`；
2. 新建多维表格，把应用加为**协作者**；
3. 配置 `.env` 中 `FAMILY_CFO_FEISHU_APP_ID/APP_SECRET/APP_TOKEN`；
4. `family-cfo feishu status`（必须 `connected: true`）；
5. `family-cfo feishu ensure-tables`；
6. `family-cfo feishu sync --all`。

详细步骤与错误码见 [`docs/feishu-setup.md`](docs/feishu-setup.md)。
**没有凭证时不会假装连接成功**；测试使用 `MockFeishuServer` 走真实客户端路径。

## 7. 安装到 Hermes

Skill 位于 `skills/family-cfo/`，符合 Hermes `SKILL.md` 规范（YAML frontmatter +
渐进式披露 + `references/`）。

```bash
# 方式一：复制到 Hermes 技能目录
cp -R skills/family-cfo ~/.hermes/skills/finance/family-cfo

# 方式二：在 Hermes 中直接使用
/family-cfo 导入这个月的微信账单
```

前提：Python 包已安装（`pip install -e .`）或设置 `FAMILY_CFO_HOME` 指向本仓库。
Skill 内脚本只使用相对路径（`scripts/fcfo.py`），不含开发机绝对路径。

## 8. 项目结构

```
AGENTS.md               仓库开发约定
pyproject.toml          依赖与入口
alembic.ini / migrations/
config/                 非敏感配置、飞书表结构、字段映射
data/                   inbox / processed / archive / failed / reports / backups
docs/                   架构、数据模型、公式、部署、飞书
skills/family-cfo/      Hermes Skill（SKILL.md/scripts/references/templates）
src/family_cfo/
  core/                 配置、金额、日期、常量、日志脱敏
  database/             SQLAlchemy 模型、迁移、初始化、备份、完整性
  importers/            平台适配器、读取器、字段映射
  transactions/         交易服务、分类、对账、自然语言
  finance/              指标、结算
  assets/ liabilities/ budgets/ investments/ planning/
  integrations/feishu/  客户端、表结构、同步、Mock
  reports/              Markdown / Excel
  automation/           任务与 cron 生成
tests/                  22+ 测试主题，含合成账单样例
```

## 9. 测试

```bash
pytest -q                 # 全部测试
pytest tests/test_feishu.py -q
```

样例账单在 `tests/fixtures/`，全部为**合成数据**，不是真实平台导出。

## 10. 已知边界

- 不提供任何转账/支付/交易执行能力；
- 不抓取银行或支付平台数据，只解析用户主动提供的导出文件；
- 不内置实时行情：投资市值必须由用户提供，否则按成本列示并标注 `estimate`；
- 飞书同步需要用户自建应用与协作者授权，无法由本工具代办；
- 定时任务的 Hermes Cron/Blueprint 字段可能随 Hermes 版本变化，`automation install`
  只生成配置文件，需用户核对后启用。

## 11. 许可

MIT。

## 12. 文档索引

- [安装与初始化](docs/installation.md)
- [架构](docs/architecture.md)
- [数据模型](docs/data-model.md)
- [指标公式](docs/formulas.md)
- [服务接口](docs/services.md)
- [飞书接入](docs/feishu-setup.md)
- [运维手册](docs/operations.md)
- [测试矩阵](docs/testing.md)
