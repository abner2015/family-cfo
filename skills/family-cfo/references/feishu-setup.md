# 飞书多维表格配置指南

> 本文件描述真实接入步骤。没有凭证时，程序会明确报告 `feishu_not_configured`；
> 测试使用内存 Mock，不会假装连接成功。

## 1. 创建自建应用

1. 打开 <https://open.feishu.cn/app>，创建“企业自建应用”。
2. 记录 `App ID` 与 `App Secret`。
3. 在“权限管理”添加：
   - `bitable:app`（读写多维表格；只读场景可用 `bitable:app:readonly`）
   - 如需读取记录级细粒度权限，再按需添加 `base:record:retrieve` / `base:record:create`
4. 发布应用（企业内部应用需管理员审核）。

## 2. 创建多维表格并授权

1. 在飞书中新建多维表格，URL 形如
   `https://xxx.feishu.cn/base/<app_token>?table=<table_id>`。
2. 把 `app_token` 写入环境变量 `FAMILY_CFO_FEISHU_APP_TOKEN`。
3. **关键**：把应用添加为该多维表格的协作者（文档右上角“分享”→“添加协作者”，
   搜索应用名称）。否则所有 API 调用都会失败（403 / 1254302）。

## 3. 配置环境变量

```bash
export FAMILY_CFO_FEISHU_APP_ID=cli_xxx
export FAMILY_CFO_FEISHU_APP_SECRET=xxx
export FAMILY_CFO_FEISHU_APP_TOKEN=bascnxxxx
```

推荐写入项目根的 `.env`（已被 `.gitignore` 排除），不要提交。

## 4. 建表

表名与字段定义在 `config/feishu_tables.yaml`，默认创建 6 张表：

| key | 默认表名 | 用途 | 方向 |
| --- | --- | --- | --- |
| transactions | FamilyCFO_交易明细 | 交易流水 | 只推 |
| assets | FamilyCFO_资产 | 资产与估值 | 只推 |
| liabilities | FamilyCFO_负债 | 负债与月供 | 只推 |
| budgets | FamilyCFO_预算 | 预算项 | 双向（回写生成新版本） |
| goals | FamilyCFO_目标 | 家庭目标 | 双向 |
| monthly_reports | FamilyCFO_月度报告 | 月度指标 | 只推 |

```bash
family-cfo feishu status          # 先确认 connected=true
family-cfo feishu ensure-tables   # 建缺失表/字段，已存在则跳过
```

字段类型映射：text=1, number=2, date=5, checkbox=7。金额字段单位为**元**（展示层），
权威精度仍以 SQLite 的“分”为准。日期字段为毫秒时间戳。

## 5. 同步

```bash
family-cfo feishu sync --all
family-cfo feishu sync --entity goals --dry-run
family-cfo feishu pull --entity goals
family-cfo feishu pull --entity budgets
family-cfo feishu conflicts
family-cfo feishu resolve goals <goal_id> --strategy local|remote
```

- **幂等**：同一 payload 再次推送直接 `skipped`。
- **增量**：以 `payload_hash` 判断内容变化，只更新变化记录。
- **重试**：`1254290`（限频）、`1254291`（写冲突）、`1254607`（数据未就绪）、
  `1255040`（超时）指数退避重试；token 失效自动刷新一次。
- **限流**：默认 10 请求/秒，可配 `FAMILY_CFO_FEISHU_RATE_LIMIT`。
- **冲突**：远端 `last_modified_time` 晚于本地 `last_synced_at` 且内容哈希不同 →
  `conflict`，不覆盖；必须人工选择策略。
- **失败隔离**：单条失败写 `sync_records.last_error`，不影响其他记录与本地账本。

## 6. 回写校验

只允许 `goals` 与 `budgets` 回写，且经过 Pydantic：

- 目标金额 > 0；当前金额 ≥ 0；每月储蓄 ≥ 0；日期可空。
- 预算项金额 > 0、分类必须存在、版本必须大于本地当前版本。
- 回写会写审计日志（`action=sync`）；预算回写生成新版本，不覆盖历史。

## 7. 常见错误

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `feishu_not_configured` | 环境变量缺失 | 配齐 3 个变量 |
| 403 / 1254302 | 应用不是表协作者 | 在表格分享中添加应用 |
| 1254003 | app_token 错误 | 检查 URL 中的 base token |
| 1254041 | table_id 不存在 | 重跑 `ensure-tables` |
| 1254290 | 触发限频 | 降低 `FAMILY_CFO_FEISHU_RATE_LIMIT` |
| 1254291 | 并发写同一张表 | 串行执行同步，勿并行多个进程 |
| 1254015 | 字段类型不匹配 | 对照 YAML 重建字段 |

## 8. Mock 测试

`tests/test_feishu.py` 使用 `MockFeishuServer`（httpx.MockTransport）走真实客户端代码路径，
覆盖建表、推送、幂等、重试、冲突、回写校验。运行：

```bash
pytest tests/test_feishu.py -q
```
