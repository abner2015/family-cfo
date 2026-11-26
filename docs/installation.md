# 安装与初始化

## 1. Python

要求 Python 3.11+（推荐 3.12）。检查：

```bash
python3 --version
```

若使用 `uv`（推荐）：

```bash
uv venv --python 3.12
source .venv/bin/activate
uv pip install -e ".[dev]"
```

或使用标准 venv：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## 2. 目录与配置

```bash
cp .env.example .env
```

非敏感默认值在 `config/config.yaml`；密钥只放 `.env`。数据目录默认 `./data`。

```bash
family-cfo doctor
family-cfo init --household "我家" --member "我"
```

`init` 做什么：

1. 创建 `data/{inbox,processed,archive,failed,reports,backups}`；
2. 执行 Alembic 迁移到 head；
3. 写入 household、主要成员；
4. 写入 20 个系统分类与默认关键词规则；
5. 创建 5 个默认账户（微信零钱、支付宝余额、银行卡、现金、信用卡）；
6. 记录 `schema_version`。

重复执行是幂等的；`--reset --force` 会删除现有数据库（慎用）。

## 3. 首次导入

```bash
family-cfo import ~/Downloads/微信支付账单.csv --dry-run
family-cfo import ~/Downloads/微信支付账单.csv
```

- 原始文件会复制到 `data/archive/<hash12>_<name>` 并设为只读；
- 相同 SHA-256 的文件再次导入会直接返回 `duplicate`；
- 同一批交易出现在不同导出文件中时，按 `(source_platform, external_id)` 去重。

## 4. 未知格式

若返回 `mapping_required`，用返回的 `suggestion.mapping` 组装：

```bash
family-cfo import bill.csv --mapping '{"date":"交易日期","amount":"金额","direction":"收支"}'
```

确认后可保存到 `config/mappings.yaml` 并复用：

```bash
family-cfo import bill.csv --mapping-name cmb_credit
```

## 5. 定时任务

```bash
family-cfo automation list
family-cfo automation install --yes --dest ~/.config/family-cfo
```

生成 `family-cfo.yaml`（Hermes blueprint 形状）与 `family-cfo.crontab`。默认全部 `enabled: false`；
请人工核对后按所用调度器启用。**不加 `--yes` 会拒绝生成。**

## 6. 升级

```bash
git pull
pip install -e ".[dev]"
family-cfo db check
```

启动与 `init` 会自动 `alembic upgrade head`。升级前建议：

```bash
family-cfo backup create --label before-upgrade
```
