# 运维手册

## 数据目录

```
data/
  inbox/        待处理账单（scan 读取）
  processed/    成功导入后的原始位置
  archive/      只读归档（按 SHA-256，0444）
  failed/       失败文件与 <batch>.errors.json
  reports/      月报/年报（md/json/xlsx）
  backups/      数据库备份 + 清单
  automation/   automation install 生成物
```

## 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `FAMILY_CFO_ROOT` | 自动查找 | 项目根目录 |
| `FAMILY_CFO_DATA_DIR` | `./data` | 数据目录 |
| `FAMILY_CFO_DB_PATH` | `./data/family_cfo.db` | SQLite 路径 |
| `FAMILY_CFO_HOUSEHOLD` | `我家` | 家庭名称 |
| `FAMILY_CFO_CURRENCY` | `CNY` | 本位币 |
| `FAMILY_CFO_TIMEZONE` | `Asia/Shanghai` | 时区 |
| `FAMILY_CFO_REVIEW_THRESHOLD` | `200` | 待确认金额阈值 |
| `FAMILY_CFO_DEDUPE_WINDOW_DAYS` | `2` | 跨平台重复窗口 |
| `FAMILY_CFO_TRANSFER_WINDOW_DAYS` | `3` | 转账匹配窗口 |
| `FAMILY_CFO_AUTO_BACKUP` | `true` | 破坏性操作前自动备份 |
| `FAMILY_CFO_FEISHU_APP_ID` | — | 飞书 App ID |
| `FAMILY_CFO_FEISHU_APP_SECRET` | — | 飞书 Secret |
| `FAMILY_CFO_FEISHU_APP_TOKEN` | — | 多维表格 token |
| `FAMILY_CFO_FEISHU_BASE_URL` | `https://open.feishu.cn` | API 域名 |
| `FAMILY_CFO_FEISHU_RATE_LIMIT` | `10` | 每秒请求数 |
| `FAMILY_CFO_FEISHU_MAX_RETRIES` | `3` | 重试次数 |
| `FAMILY_CFO_FEISHU_TIMEOUT` | `20` | 超时秒数 |
| `FAMILY_CFO_HOME` | — | Skill 脚本定位仓库（安装到 Hermes 时需要） |

## 备份

```bash
family-cfo backup create
family-cfo backup create --label before-upgrade
family-cfo backup list
family-cfo backup verify data/backups/family_cfo-YYYYmmdd-HHMMSS.db
family-cfo backup prune --keep 20
```

- 使用 SQLite Online Backup API，兼容 WAL；
- 备份后立即执行 `PRAGMA integrity_check` + 外键校验；
- 同名清单 `.json` 记录 SHA-256、大小、表行数。

## 恢复

```bash
family-cfo backup restore data/backups/<file>.db --yes
```

流程：校验备份 → 生成 `pre-restore` 安全备份 → 断开连接 → 清理 `-wal/-shm` →
恢复 → 再校验。恢复后请运行 `family-cfo db check`。

## 定时任务

```bash
family-cfo automation list
family-cfo automation install --yes --dest data/automation
```

生成 `family-cfo.yaml`（Hermes blueprint 形状）与 `family-cfo.crontab`。
默认 `enabled: false`，不会自动注册到任何调度器。

| 任务 | 默认计划 | 内容 |
| --- | --- | --- |
| weekly_scan | 周一 09:00 | 扫描导入 |
| weekly_summary | 周日 19:00 | 7 天摘要 |
| monthly_collect | 每月 1 日 08:00 | 上月归集 |
| monthly_close_reminder | 每月 28 日 09:00 | 结算检查 |
| monthly_report | 每月 2 日 09:00 | 月报 |
| quarterly_review | 每季首日 10:00 | 估值盘点 |
| annual_planning | 每年 1 月 1 日 10:00 | 年度规划 |

## 故障排查

| 症状 | 排查 |
| --- | --- |
| `数据库不存在` | 先 `family-cfo init` |
| `mapping_required` | 按 `suggestion` 提供 `--mapping` |
| `duplicate_file` / `duplicate` | 文件已导入；用返回的 `batch_id` 查历史 |
| 结算被阻塞 | `settle --status` 查看 blockers，逐项处理 |
| 金额差 100 倍 | 输入是“元”，库内是“分”；不要混用 |
| 月报某月为 0 | 看 `missing_months`，缺渠道不是 0 |
| 飞书 403 | 应用未加为表格协作者 |
| 飞书 1254291 | 同表并发写；串行重跑 |
| `database is locked` | 关闭其他写入进程；WAL + busy_timeout 已启用 |
| 备份校验失败 | 用 `backup list` 找更早的完整备份 |

## 日志

默认输出到 stderr；`--verbose` 打开 DEBUG。日志过滤器会遮蔽 token、secret、卡号、
11 位以上长数字。不要把 `.env` 内容粘进 issue。
