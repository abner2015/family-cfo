# data/

运行时数据目录，默认不入库（见 `.gitignore`）。

| 目录 | 用途 |
| --- | --- |
| `inbox/` | 待处理账单，`family-cfo scan` 读取 |
| `processed/` | 成功导入后的原始文件 |
| `archive/` | 只读归档（SHA-256 命名，权限 0444） |
| `failed/` | 失败文件与 `<batch>.errors.json` |
| `reports/` | Markdown / JSON / Excel 报告 |
| `backups/` | 数据库备份 + `.json` 清单 |
| `automation/` | `automation install` 生成的 blueprint 与 crontab |

真实账单与数据库只应存在于本机，不要提交到 Git。
