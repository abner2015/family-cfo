#!/usr/bin/env python3
"""回填飞书交易明细的「月份」字段（批量，替代逐条更新）。

逐条 update 每笔要 2 次 API（先 get_record 做冲突检测再 update），
1638 笔要跑近两小时；改为批量接口后约 4 次调用即可完成。

用法:
    python3 backfill_month.py            # 只补空的
    python3 backfill_month.py --verify   # 只校验
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, "/opt/data/pylibs/family-cfo")

from family_cfo.core.config import load_config  # noqa: E402
from family_cfo.integrations.feishu.client import FeishuClient  # noqa: E402

TABLE_ID = "tblqVZIYBbLmK9kl"
BATCH = 500


def text_of(value) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return "".join(
            p.get("text", "") if isinstance(p, dict) else str(p) for p in value
        ).strip()
    return str(value).strip()


def month_of(ms) -> str | None:
    if not ms:
        return None
    try:
        return datetime.fromtimestamp(int(ms) / 1000).strftime("%Y-%m")
    except (ValueError, OSError, TypeError, OverflowError):
        return None


def fetch_all(client: FeishuClient) -> list[dict]:
    out: list[dict] = []
    token = None
    while True:
        res = client.list_records(TABLE_ID, page_token=token, page_size=BATCH)
        out.extend(res.get("items") or [])
        token = res.get("page_token")
        if not res.get("has_more"):
            break
    return out


def main() -> int:
    settings = load_config(root=Path("/opt/data/home/family-cfo"))
    client = FeishuClient(settings)
    try:
        records = fetch_all(client)
        print(f"远端共 {len(records)} 条")

        pending: list[dict] = []
        unknown = 0
        for rec in records:
            fields = rec.get("fields") or {}
            if text_of(fields.get("月份")):
                continue
            month = month_of(fields.get("日期"))
            if not month:
                unknown += 1
                continue
            pending.append({"record_id": rec["record_id"], "fields": {"月份": month}})

        print(f"待补月份: {len(pending)} 条（无日期无法判定: {unknown} 条）")

        if "--verify" in sys.argv:
            return 0

        done = 0
        for i in range(0, len(pending), BATCH):
            chunk = pending[i : i + BATCH]
            client.batch_update(TABLE_ID, chunk)
            done += len(chunk)
            print(f"  已更新 {done}/{len(pending)}")

        # 复核
        records = fetch_all(client)
        tagged = sum(1 for r in records if text_of((r.get("fields") or {}).get("月份")))
        print(f"\n复核: {tagged}/{len(records)} 条已带月份标记")
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
