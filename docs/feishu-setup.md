# 飞书多维表格接入

详细字段与错误码见
[`skills/family-cfo/references/feishu-setup.md`](../skills/family-cfo/references/feishu-setup.md)。

## 步骤

1. **创建自建应用**：<https://open.feishu.cn/app>，记录 App ID / App Secret。
2. **申请权限**：`bitable:app`（读写）；只读场景用 `bitable:app:readonly`。
3. **创建多维表格**：URL 中 `/base/<app_token>` 即所需 token。
4. **添加协作者**：把应用加入该表格协作者，否则必然 403。
5. **配置环境变量**（`.env`）：

   ```dotenv
   FAMILY_CFO_FEISHU_APP_ID=cli_xxx
   FAMILY_CFO_FEISHU_APP_SECRET=xxx
   FAMILY_CFO_FEISHU_APP_TOKEN=bascnxxxx
   ```

6. **验证与建表**：

   ```bash
   family-cfo feishu status          # connected 必须为 true
   family-cfo feishu ensure-tables
   ```

7. **同步**：

   ```bash
   family-cfo feishu sync --all
   family-cfo feishu sync --entity goals --dry-run
   family-cfo feishu pull --entity goals
   family-cfo feishu conflicts
   ```

## 表结构

`config/feishu_tables.yaml` 定义 6 张表：交易明细、资产、负债、预算、目标、月度报告。
金额单位为元（展示层），日期为毫秒时间戳；权威精度仍是 SQLite 的分。

## 同步语义

| 行为 | 规则 |
| --- | --- |
| 幂等 | `payload_hash` 未变 → `skipped` |
| 新增 | 无 `remote_record_id` → create → 记录 id |
| 更新 | 有 id 且本地变更 → update |
| 冲突 | 远端在上次同步后修改且哈希不同 → `conflict`，不覆盖 |
| 回写 | 仅 goals/budgets，Pydantic 校验，写审计 |
| 失败 | 写 `sync_records.last_error`，本地账本不变，可重跑 |
| 限流 | 默认 10 req/s；重试 1254290/1254291/1254607/1255040 |

## 没有真实凭证时

- `feishu status` 返回 `configured=false, connected=false` 与明确错误；
- `feishu sync` 直接拒绝并提示缺少变量；
- 自动化测试使用 `MockFeishuServer`，覆盖建表、推送、幂等、重试、冲突、回写，
  不访问网络。
