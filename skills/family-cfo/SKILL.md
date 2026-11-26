---
name: family-cfo
description: >
  家庭财务管家：导入微信/支付宝/银行账单，自动分类去重、识别退款与内部转账，
  管理账户、资产负债、预算、投资与家庭目标，执行月度结算，生成月报/年报，并同步到飞书多维表格。
  当用户提到家庭记账、账单导入、消费分析、储蓄率、净资产、房贷、预算、财务目标、
  月度结算或飞书多维表格同步时使用。所有金额由本地 Python 账本计算，禁止心算或编造数据。
version: 0.1.0
author: Family CFO
license: MIT
platforms: [macos, linux]
metadata:
  hermes:
    tags: [finance, family, accounting, budget, feishu, bitable, sqlite]
    category: finance
    requires_toolsets: [terminal]
required_environment_variables:
  - name: FAMILY_CFO_FEISHU_APP_ID
    prompt: 飞书自建应用 App ID
    help: 在 https://open.feishu.cn/app 创建自建应用后获得；仅同步飞书时需要
    required_for: 飞书同步
  - name: FAMILY_CFO_FEISHU_APP_SECRET
    prompt: 飞书自建应用 App Secret
    help: 与 App ID 配套；不要写入仓库或日志
    required_for: 飞书同步
  - name: FAMILY_CFO_FEISHU_APP_TOKEN
    prompt: 飞书多维表格 app_token
    help: 多维表格 URL 中 /base/<app_token> 部分
    required_for: 飞书同步
---

# Family CFO — 家庭财务管家

本地 SQLite 是唯一权威账本，飞书多维表格只是展示与协作层。所有金额、比率、结算结论都由
Python 以 `Decimal` / 整数分计算，**不得由模型心算、估算或补全**。

## When to Use

在以下任一情况加载本 Skill：

- 用户要导入/归集微信、支付宝、银行、信用卡或其他平台账单；
- 用户询问家庭收支、储蓄率、净资产、资产负债率、应急资金、预算执行、投资收益；
- 用户要记录一笔消费（自然语言记账）、修正分类、处理重复/退款/转账；
- 用户要更新房贷余额、资产估值、预算、投资持仓或家庭目标；
- 用户要做月度结算、生成月报/年报、同步飞书、备份/恢复账本；
- 用户询问“我家财务健康吗”“这个月教育支出多少”等需要口径一致的问题。

不要用于：证券交易执行、转账支付、信贷申请、税务申报等对外金融操作。本 Skill 只读账本，
不具备任何付款或交易能力。

## 环境与前置检查

所有命令均以 Skill 目录为基准，先切到 Skill 根目录（即包含 `SKILL.md` 的目录）。
脚本入口是 `scripts/fcfo.py`，它会自动定位已安装的 `family_cfo` 包，或仓库内 `src/`：

```bash
python scripts/fcfo.py doctor          # Python/配置/数据库/完整性
python scripts/fcfo.py --help          # 完整命令列表
```

若提示找不到包，请让用户先执行 `pip install -e .`（仓库根目录），不要自行修改脚本路径。

## 不可违反的规则

1. **先读后写**：任何导入、结算、同步前先跑 `doctor` 或 `db check`。
2. **不编造**：没有账单就回答“该渠道未导入”，不得把缺失渠道当 0 支出。
3. **金额只引用程序输出**：报告、比率、缺口一律来自 CLI/JSON 输出，禁止自行计算后改写。
4. **删除与覆盖需确认**：`tx delete`、`settle --close --reason`、`backup restore`、`feishu resolve`
   必须先向用户说明影响并获得明确同意。
5. **不接触凭证**：不读取、不回显 `.env`、App Secret、支付密码、验证码；日志已脱敏，仍不要主动打印。
6. **定时任务不自动启用**：`automation install` 必须带 `--yes` 且经用户确认。
7. **飞书失败不回滚本地**：同步失败只报告失败项，不改动账本。

## 意图 → 操作映射

| 用户说法 | 执行 |
| --- | --- |
| 导入这个月的微信账单 | `python scripts/fcfo.py import <文件> --platform wechat` 或把文件放入 `data/inbox` 后 `scan` |
| 分析一下上个月家庭消费 | `analyze --month last` |
| 这个月教育支出多少 | `ask "这个月教育支出多少"` 或 `--json analyze` |
| 家里财务健康吗 | `health --month current` |
| 我们家目前有多少净资产？ | `--json analyze --month current`，读取 `balance_sheet.net_worth` |
| 这个月教育支出多少？ | `--json analyze --month current`，在 `categories` 中筛 `slug=education` |
| 看看最近三个月消费趋势 | `--json analyze`，读取 `trend` |
| 更新一下房贷余额 | `liability balance 房贷 --amount <金额> --reason <原因>` |
| 统计今年家庭储蓄率 | `report --year <年份>` 或 `analyze` 汇总；以报告 `totals.savings_rate` 为准 |
| 进行本月账单结算 | `settle --month current --collect` → `settle --month current --status` → 按需 `--close` |
| 同步最新数据到飞书 | `feishu status` → `feishu sync --all` |
| 生成家庭年度财务报告 | `report --year <年份> --format md,xlsx` |

## Procedure

### A. 账单导入

1. 确认文件来源与月份；**绝不**通过模拟登录、抓取凭证或非官方接口获取账单。
2. 先预演，确认识别结果与笔数：
   ```bash
   python scripts/fcfo.py import "<账单文件>" --dry-run
   ```
3. 正式导入（原始文件会只读归档到 `data/archive/`，重复文件按 SHA-256 跳过）：
   ```bash
   python scripts/fcfo.py import "<账单文件>" --platform auto
   ```
4. 若返回 `mapping_required`，把 `suggestion` 中的列映射展示给用户确认，再执行：
   ```bash
   python scripts/fcfo.py import "<文件>" --mapping '{"date":"交易日期","amount":"金额","direction":"收支"}'
   ```
   用户确认后可保存为 `config/mappings.yaml` 中的命名映射，后续用 `--mapping-name`。
5. 批量收件：把文件放入 `data/inbox/`，执行 `scan`；失败文件进入 `data/failed/`，错误报告同目录。
6. **验证**：向用户报告 `imported / duplicates / failed` 三个数字与错误行；不要只说“导入成功”。

### B. 对账与异常处理

```bash
python scripts/fcfo.py reconcile --month last
python scripts/fcfo.py tx list --month last --review
```

- 重复候选：`tx resolve <id> --distinct`（确认是两笔）或 `--duplicate`（作废该笔）。
  跨平台重复只在金额、日期、商户均相似时标记，**从不自动删除**。
- 未分类：`tx fix <id> --category 餐饮`；若用户希望以后自动处理，加 `--rule` 沉淀关键词规则。
- 自定义分类：`category add 宠物 --kind expense --keyword 猫粮 --keyword 宠物医院`，
  之后可由关键词规则自动命中。
- 退款：对账会自动把入账退款关联到 90 天内同商户/同金额的支出；未关联项需人工确认。
- 转账/还款：双向金额与日期匹配后自动排除出收支；单边记录保留待确认，会阻塞关账。
  确认后可用 `tx retype <id> --type transfer --reason "..."` 关闭该审核项；若实际是支出，改为 `--type expense`。
- **模型不直接写分类**：Agent 只能根据账单内容提出候选分类，必须经用户确认后调用
  `tx fix`；对不确定的交易保持 `pending_review`，不得为了“看起来完整”而猜一个分类。

### C. 月度结算

状态机：`COLLECTING → IMPORTING → RECONCILING → REVIEW_REQUIRED / READY_TO_CLOSE → CLOSED`。

```bash
python scripts/fcfo.py settle --month 2026-10 --collect   # 扫描 + 对账
python scripts/fcfo.py settle --month 2026-10 --status    # 查看 checklist 与 blocker
```

关账门槛（任一不满足即阻塞）：

- 应纳入渠道均已导入，或用户明确标记缺失：`--mark-missing bank --reason "..."`；
- 导入无失败行；
- 待确认、重复候选、未关联退款均已处理，或用户授权暂挂：`--pending-policy suspend`；
- 完整性校验通过；
- SQL 与 Python 两套口径的收支金额一致。

只有用户明确同意后才能：

```bash
python scripts/fcfo.py settle --month 2026-10 --close
python scripts/fcfo.py settle --month 2026-10 --close --reason "缺失渠道已人工确认"
python scripts/fcfo.py settle --month 2026-10 --reopen --reason "补充退款"
```

关账会写入净资产快照；重开会记录调整日志，不静默改历史。

### D. 分析、预算、资产负债、投资、目标

```bash
python scripts/fcfo.py --json analyze --month last
python scripts/fcfo.py health --month last
python scripts/fcfo.py ask "上个月教育支出多少"
python scripts/fcfo.py budget set --month last --item 餐饮=1500 --item 教育=1000
python scripts/fcfo.py budget set --year 2026 --item 餐饮=18000
python scripts/fcfo.py budget show --year 2026
python scripts/fcfo.py budget show --month last
python scripts/fcfo.py asset list
python scripts/fcfo.py asset value 自住房 --value 2100000 --valued-on 2026-10-31
python scripts/fcfo.py liability list
python scripts/fcfo.py liability schedule 房贷 --months 12
python scripts/fcfo.py invest portfolio
python scripts/fcfo.py plan --annual
```

- 预算修改自动生成新版本，旧版本保留；
- 资产估值追加历史，不覆盖；自住房不计入流动资产；
- 投资未实现收益只有在用户提供市值时才计算，绝不使用“实时行情”臆测；
- 规划输出必须区分 `actual / estimate / assumption`，并转述 `assumptions` 字段。

### E. 报告

```bash
python scripts/fcfo.py report --month last --format md,xlsx
python scripts/fcfo.py report --year 2026 --format md,xlsx
```

报告落盘到 `data/reports/`。回复用户时给出文件路径，并摘录“数据完整性”与“待确认事项”两节。

### F. 飞书同步

```bash
python scripts/fcfo.py feishu status          # 未配置时必须显示 connected=false，不得假装成功
python scripts/fcfo.py feishu ensure-tables   # 建表/补字段
python scripts/fcfo.py feishu sync --all      # SQLite → 飞书
python scripts/fcfo.py feishu pull --entity goals
python scripts/fcfo.py feishu conflicts
python scripts/fcfo.py feishu resolve goals <id> --strategy local
```

- 只允许回写 `goals` 与 `budgets`，且必须通过 Pydantic 校验；
- 远端在上次同步后被修改 → `conflict`，禁止静默覆盖；先展示冲突再让用户选 `local/remote`；
- 失败项写入 `sync_records`，本地账本不受影响；可直接重跑。

### G. 备份与恢复

```bash
python scripts/fcfo.py backup create
python scripts/fcfo.py backup verify "<备份文件>"
python scripts/fcfo.py backup restore "<备份文件>" --yes
```

恢复前必须确认；工具会自动做 `pre-restore` 安全备份并校验完整性。

## 工具调用与结果验证

- 命令失败时读取 stderr 的错误码：`mapping_required`、`duplicate_file`、`settlement_error`、
  `feishu_not_configured`、`conflict`。按错误码处理，不要猜测成功。
- 每次写操作后至少验证一项：`tx list`、`settle --status`、`db check`、`feishu conflicts`。
- 输出金额必须带币种与期间，例如 “2026-10 支出 ¥2,764.30（已扣退款）”。
- JSON 模式用于精确取值：`--json`；不要把 Markdown 表格再心算一遍。

## Pitfalls

| 现象 | 处理 |
| --- | --- |
| 报告显示某月 0 支出 | 先看 `missing_months` / `sources_present`，缺渠道不是 0 |
| 金额差 100 倍 | 确认没有把“元”当“分”；CLI 输入用元，数据库存分 |
| 重复导入同一文件 | 返回 `duplicate`，用 `batch_id` 指向原批次，不要重复入账 |
| 退款月支出异常 | 退款按发生月冲减；跨月退款在退款月体现，属预期 |
| 飞书 403 | 应用未加入多维表格协作者，或缺少 `bitable:app` 权限 |
| 飞书 1254291 | 同表并发写；本工具已串行并重试，仍失败则降低并发 |
| 结算被阻塞 | 按 `blockers` 逐项处理：补渠道、处理待确认、修完整性 |

## 数据安全

- 只读归档原始账单（`0444`），不修改原始文件；
- 不保存支付密码、银行卡密码、短信验证码；
- `.env` 不入库、不入日志；日志已对 token/长数字脱敏；
- 交易删除为软删除 + 审计；关账/重开/余额调整必须带原因；
- 所有对外金融操作（转账、买卖、借贷）均不在本 Skill 能力范围内。
