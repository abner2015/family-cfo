# 数据安全与隐私

## 原则

1. **本地优先**：SQLite 是唯一权威账本，默认不上传任何数据。
2. **最小权限**：飞书应用只申请 `bitable:app`（或只读）；不申请通讯录、消息、支付权限。
3. **只读金融能力**：本项目不提供转账、支付、证券交易、信贷申请能力。
4. **不采集凭证**：不保存支付密码、银行卡密码、短信验证码、Cookie。

## 落盘

- 原始账单复制到 `data/archive/`，权限 `0444`，不修改、不删除；
- 数据库在 `data/family_cfo.db`，`data/` 已在 `.gitignore` 中；
- `.env` 不入库；`.env.example` 只含空值；
- 备份在 `data/backups/`，带 SHA-256 清单与完整性校验。

## 日志与错误

- 日志过滤器会遮蔽 `Bearer ...`、`app_secret=...`、13–19 位卡号、11 位以上长数字；
- 飞书错误只保留 `code/msg`，不打印 token；
- CLI 的 `db config` 输出脱敏配置（secret 显示为 `***`）；
- 账单错误报告只含原始行与错误原因，不含凭证。

## 审计

以下操作写入 `audit_logs`（含 before/after）：

- 交易创建（手工/自然语言）、分类修正、软删除、恢复；
- 账户创建与余额调整；
- 资产估值、负债余额调整、还款；
- 预算版本、目标进度；
- 导入批次、对账、结算关账/重开；
- 飞书回写。

删除交易必须提供原因；关账覆盖与重开必须提供原因。

## 备份与恢复

```bash
family-cfo backup create
family-cfo backup verify data/backups/<file>.db
family-cfo backup restore data/backups/<file>.db --yes
```

- 使用 SQLite Online Backup API，兼容 WAL；
- 校验 `PRAGMA integrity_check` 与外键；
- 恢复前自动生成 `pre-restore` 安全备份；
- 恢复会 dispose 连接并清理 `-wal/-shm`，避免半写状态。

## 建议

- 把 `data/` 放在 FileVault/加密磁盘；
- 定期把备份复制到离线介质；
- 多人共用电脑时，为 `.env` 与 `data/` 设置仅本人可读权限（`chmod 700`）。
