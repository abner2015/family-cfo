"""File readers for CSV / XLSX / XLS bills.

Bills from payment platforms are not clean tables: they carry a preamble, a
variable header row and a footer.  The reader normalises that into
:class:`RawTable`.
"""

from __future__ import annotations

import csv
import io
import re
from pathlib import Path
from typing import Any

import pandas as pd

from ..core.errors import ImportError_, UnknownFormatError
from ..core.logging import get_logger
from .base import RawTable

log = get_logger("importers.readers")

ENCODINGS = ("utf-8-sig", "gb18030", "utf-16", "utf-8", "latin-1")

# A header row must contain at least one date-ish and one amount-ish token.
DATE_TOKENS = ("交易时间", "交易日期", "记账日期", "创建时间", "付款时间", "日期", "time", "date")
AMOUNT_TOKENS = ("金额", "发生额", "amount", "收入", "支出", "借方", "贷方")


def detect_kind(path: str | Path) -> str:
    suffix = Path(path).suffix.lower()
    if suffix in {".csv", ".txt"}:
        return "csv"
    if suffix in {".xlsx", ".xlsm"}:
        return "xlsx"
    if suffix == ".xls":
        return "xls"
    raise UnknownFormatError(f"不支持的文件类型: {suffix or '(无扩展名)'}，仅支持 csv/xlsx/xls")


def _decode_csv(path: Path) -> tuple[str, str]:
    raw = path.read_bytes()
    for enc in ENCODINGS:
        try:
            text = raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
        if "\x00" in text[:2000] and enc != "utf-16":
            continue
        return text, enc
    raise ImportError_(f"无法解码 CSV 文件: {path.name}")


def _rows_from_frame(frame: pd.DataFrame) -> list[list[Any]]:
    rows: list[list[Any]] = []
    for record in frame.itertuples(index=False, name=None):
        rows.append(["" if pd.isna(v) else v for v in record])
    return rows


def _find_header(rows: list[list[Any]], *, max_scan: int = 40) -> int:
    for idx, row in enumerate(rows[:max_scan]):
        cells = [str(c).strip() for c in row if str(c).strip() != ""]
        if len(cells) < 3:
            continue
        joined = "|".join(cells).lower()
        has_date = any(tok.lower() in joined for tok in DATE_TOKENS)
        has_amount = any(tok.lower() in joined for tok in AMOUNT_TOKENS)
        if has_date and has_amount:
            return idx
    return -1


def _build_table(
    path: Path,
    kind: str,
    rows: list[list[Any]],
    *,
    encoding: str | None = None,
    sheet: str | None = None,
) -> RawTable:
    header_idx = _find_header(rows)
    if header_idx < 0:
        preview = ["|".join(str(c) for c in row[:8]) for row in rows[:8]]
        raise ImportError_(
            f"未能在 {path.name} 中识别表头（需要日期列和金额列）。",
            detail="\n".join(preview),
        )
    header_cells = [str(c).strip() for c in rows[header_idx]]
    # Deduplicate empty/duplicate header names deterministically.
    seen: dict[str, int] = {}
    headers: list[str] = []
    for i, cell in enumerate(header_cells):
        name = cell or f"col_{i + 1}"
        if name in seen:
            seen[name] += 1
            name = f"{name}_{seen[name]}"
        else:
            seen[name] = 0
        headers.append(name)
    data_rows: list[dict[str, Any]] = []
    for row in rows[header_idx + 1 :]:
        if not any(str(c).strip() for c in row):
            continue
        values = list(row) + [""] * (len(headers) - len(row))
        record = {headers[i]: values[i] for i in range(len(headers))}
        if _is_footer(record, headers):
            break
        data_rows.append(record)
    preamble = [
        "|".join(str(c) for c in row if str(c).strip() != "")
        for row in rows[:header_idx]
        if any(str(c).strip() for c in row)
    ]
    return RawTable(
        path=path,
        kind=kind,
        headers=headers,
        rows=data_rows,
        preamble=preamble,
        encoding=encoding,
        sheet=sheet,
        header_row=header_idx + 1,
    )


_FOOTER_RE = re.compile(r"^(共|合计|总计|—+|-{3,}|end of|total)")


def _is_footer(record: dict[str, Any], headers: list[str]) -> bool:
    first = str(next(iter(record.values()), "")).strip()
    if _FOOTER_RE.match(first):
        return True
    # Platform exports end with a one-cell note in the first column.
    non_empty = [str(v).strip() for v in record.values() if str(v).strip()]
    if len(non_empty) <= 1 and first:
        return True
    return False


def read_csv_table(path: str | Path) -> RawTable:
    path = Path(path)
    text, encoding = _decode_csv(path)
    # sniffer handles both , and ; and tab separated exports
    sample = text[:8192]
    delimiter = ","
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        delimiter = dialect.delimiter
    except csv.Error:
        pass
    rows = [list(row) for row in csv.reader(io.StringIO(text), delimiter=delimiter)]
    return _build_table(path, "csv", rows, encoding=encoding)


def read_excel_table(path: str | Path, *, sheet: str | int | None = None) -> RawTable:
    path = Path(path)
    kind = detect_kind(path)
    engine = "xlrd" if kind == "xls" else "openpyxl"
    try:
        book = pd.ExcelFile(path, engine=engine)
    except Exception as exc:
        raise ImportError_(f"无法打开表格文件: {path.name}", detail=str(exc)) from exc
    sheet_name = sheet if sheet is not None else book.sheet_names[0]
    frame = pd.read_excel(
        book,
        sheet_name=sheet_name,
        header=None,
        dtype=str,
        keep_default_na=False,
    )
    return _build_table(
        path, kind, _rows_from_frame(frame), sheet=str(sheet_name)
    )


def read_any_table(path: str | Path, *, sheet: str | int | None = None) -> RawTable:
    path = Path(path)
    kind = detect_kind(path)
    if kind == "csv":
        return read_csv_table(path)
    return read_excel_table(path, sheet=sheet)


def list_sheets(path: str | Path) -> list[str]:
    kind = detect_kind(path)
    if kind == "csv":
        return ["csv"]
    engine = "xlrd" if kind == "xls" else "openpyxl"
    return list(pd.ExcelFile(path, engine=engine).sheet_names)
