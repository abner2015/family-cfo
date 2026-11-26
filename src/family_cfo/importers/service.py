"""Import orchestration: archive → parse → normalise → dedupe → persist."""

from __future__ import annotations

import json
import shutil
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ..core.config import Settings
from ..core.constants import ImportStatus, TxnStatus
from ..core.errors import (
    ImportError_,
    MappingRequiredError,
    UnknownFormatError,
    ValidationError,
)
from ..core.ids import file_sha256, new_id
from ..core.logging import get_logger
from ..database.audit import record_audit
from ..database.base import Database
from ..database.models import ImportBatch, RawTransaction
from ..database.init import get_household
from sqlalchemy.exc import IntegrityError
from ..transactions.classify import RuleClassifier
from ..transactions.service import TransactionService
from ..assets.accounts import AccountService
from .alipay import AlipayAdapter
from .bank import BankStatementAdapter
from .base import AdapterResult, BillAdapter, RawTable, RowError
from .columns import suggest_mapping
from .generic import MappingAdapter
from .mapping import get_profile
from .readers import read_any_table
from .wechat import WeChatAdapter

log = get_logger("importers.service")

SUPPORTED_SUFFIXES = {".csv", ".txt", ".xlsx", ".xlsm", ".xls"}
ADAPTER_BY_PLATFORM = {
    "wechat": WeChatAdapter,
    "alipay": AlipayAdapter,
    "bank": BankStatementAdapter,
}


@dataclass
class ImportReport:
    file: str
    status: str  # imported | duplicate | dry_run | failed
    batch_id: str | None = None
    platform: str | None = None
    adapter: str | None = None
    total_rows: int = 0
    imported: int = 0
    duplicates: int = 0
    failed: int = 0
    errors: list[RowError] = field(default_factory=list)
    period: str | None = None
    period_start: str | None = None
    period_end: str | None = None
    archive_path: str | None = None
    account_id: str | None = None
    message: str | None = None
    warnings: list[str] = field(default_factory=list)
    items: list[dict] = field(default_factory=list)
    meta: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "file": self.file,
            "status": self.status,
            "batch_id": self.batch_id,
            "platform": self.platform,
            "adapter": self.adapter,
            "total_rows": self.total_rows,
            "imported": self.imported,
            "duplicates": self.duplicates,
            "failed": self.failed,
            "period": self.period,
            "period_start": self.period_start,
            "period_end": self.period_end,
            "archive_path": self.archive_path,
            "account_id": self.account_id,
            "message": self.message,
            "warnings": self.warnings,
            "errors": [
                {"row": e.row_number, "message": e.message} for e in self.errors[:50]
            ],
            "meta": self.meta,
        }


class ImportService:
    def __init__(self, db: Database, settings: Settings) -> None:
        self.db = db
        self.settings = settings
        settings.ensure_dirs()

    # ------------------------------------------------------------------ public
    def default_adapters(self) -> list[BillAdapter]:
        return [WeChatAdapter(), AlipayAdapter(), BankStatementAdapter()]

    def detect(self, table: RawTable, *, platform: str = "auto") -> tuple[BillAdapter, float]:
        if platform and platform != "auto":
            if platform in ADAPTER_BY_PLATFORM:
                adapter = ADAPTER_BY_PLATFORM[platform]()
                return adapter, max(adapter.score(table), 0.5)
            raise ValidationError(
                f"未知平台: {platform}。可用: auto, {', '.join(sorted(ADAPTER_BY_PLATFORM))}, "
                "或使用 --mapping 指定字段映射。"
            )
        scored = sorted(
            ((adapter.score(table), adapter) for adapter in self.default_adapters()),
            key=lambda pair: pair[0],
            reverse=True,
        )
        best_score, best = scored[0]
        if best_score < 0.45:
            raise MappingRequiredError(
                "无法识别账单格式，请确认字段映射后重试。",
                suggestion=suggest_mapping(table.headers),
            )
        return best, best_score

    def scan_inbox(self, *, dry_run: bool = False, directory: Path | None = None) -> list[ImportReport]:
        scan_dir = Path(directory) if directory is not None else self.settings.inbox_dir
        if not scan_dir.exists():
            raise ImportError_(f"扫描目录不存在: {scan_dir}")
        files = sorted(
            p
            for p in scan_dir.iterdir()
            if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES and not p.name.startswith(".")
        )
        reports: list[ImportReport] = []
        for path in files:
            try:
                reports.append(self.import_file(path, dry_run=dry_run))
            except Exception as exc:  # noqa: BLE001 - one bad file must not stop the scan
                log.warning("import failed for %s: %s", path.name, exc)
                self._relocate(path, "failed")
                reports.append(
                    ImportReport(file=str(path), status="failed", message=str(exc))
                )
        return reports

    def import_file(
        self,
        path: str | Path,
        *,
        platform: str = "auto",
        account: str | None = None,
        mapping: dict[str, str] | None = None,
        mapping_name: str | None = None,
        dry_run: bool = False,
        actor: str = "cli",
    ) -> ImportReport:
        path = Path(path).expanduser()
        if not path.exists():
            raise ImportError_(f"账单文件不存在: {path}")
        if path.suffix.lower() not in SUPPORTED_SUFFIXES:
            raise UnknownFormatError(f"不支持的文件类型: {path.suffix}")
        file_hash = file_sha256(path)
        size = path.stat().st_size

        if mapping_name:
            profile = get_profile(self.settings.project_root, mapping_name)
            mapping = profile["mapping"]
            platform = profile.get("platform", platform)

        existing = self._find_batch(file_hash)
        if existing is not None:
            self._relocate(path, "processed")
            return ImportReport(
                file=str(path),
                status="duplicate",
                batch_id=existing.id,
                platform=existing.detected_platform,
                adapter=existing.adapter,
                total_rows=existing.total_rows,
                imported=existing.imported_rows,
                duplicates=existing.duplicate_rows,
                failed=existing.failed_rows,
                period=existing.period,
                archive_path=existing.archive_path,
                message="文件内容与已导入批次相同（SHA-256 去重），未重复处理。",
            )

        table = read_any_table(path)
        adapter_result, adapter, score = self._parse(table, platform, mapping)

        if dry_run:
            return self._dry_run_report(path, table, adapter_result, adapter, score)

        archive_path = self._archive(path)
        batch_id = new_id("batch")
        started = datetime.now().replace(microsecond=0)
        try:
            with self.db.session() as session:
                household = get_household(session)
                batch = ImportBatch(
                    id=batch_id,
                    household_id=household.id,
                    file_name=path.name,
                    stored_path=str(path),
                    archive_path=str(archive_path),
                    file_hash=file_hash,
                    file_size=size,
                    adapter=adapter.name,
                    detected_platform=adapter.platform,
                    status=ImportStatus.IMPORTING,
                    total_rows=adapter_result.total_rows,
                    started_at=started,
                )
                session.add(batch)
                session.flush()

                report = self._persist(
                    session,
                    household_id=household.id,
                    batch=batch,
                    adapter_result=adapter_result,
                    adapter=adapter,
                    account_ref=account,
                    actor=actor,
                )
        except IntegrityError:
            # Another process inserted the same file between our hash check and
            # this insert.  The unique (household, file_hash) constraint is the
            # final guard; report it as a duplicate instead of crashing.
            existing = self._find_batch(file_hash)
            if existing is None:
                raise
            self._relocate(path, "processed")
            return ImportReport(
                file=str(path),
                status="duplicate",
                batch_id=existing.id,
                platform=existing.detected_platform,
                adapter=existing.adapter,
                total_rows=existing.total_rows,
                imported=existing.imported_rows,
                duplicates=existing.duplicate_rows,
                failed=existing.failed_rows,
                period=existing.period,
                archive_path=existing.archive_path,
                message="文件已被其他进程导入（唯一约束去重），未重复处理。",
            )

        if report.status in {"imported"}:
            self._relocate(path, "processed")
        elif report.status == "failed":
            self._relocate(path, "failed")
        return report

    # ----------------------------------------------------------------- internal
    def _find_batch(self, file_hash: str) -> ImportBatch | None:
        from sqlalchemy import select

        with self.db.session() as session:
            household = get_household(session)
            return session.scalar(
                select(ImportBatch).where(
                    ImportBatch.household_id == household.id,
                    ImportBatch.file_hash == file_hash,
                )
            )

    def _parse(
        self,
        table: RawTable,
        platform: str,
        mapping: dict[str, str] | None,
    ) -> tuple[AdapterResult, BillAdapter, float]:
        if mapping:
            adapter: BillAdapter = MappingAdapter(mapping, platform=platform if platform != "auto" else "other")
            score = 1.0
        else:
            adapter, score = self.detect(table, platform=platform)
        result = adapter.parse(table, default_currency=self.settings.currency)
        result.meta["detection_score"] = round(score, 3)
        result.meta["headers"] = table.headers
        result.meta["encoding"] = table.encoding
        result.meta["sheet"] = table.sheet
        return result, adapter, score

    def _dry_run_report(self, path, table, adapter_result, adapter, score) -> ImportReport:
        periods = Counter(d.occurred_at.strftime("%Y-%m") for d in adapter_result.drafts)
        return ImportReport(
            file=str(path),
            status="dry_run",
            platform=adapter.platform,
            adapter=adapter.name,
            total_rows=adapter_result.total_rows,
            imported=len(adapter_result.drafts),
            failed=len(adapter_result.errors),
            errors=adapter_result.errors,
            period=periods.most_common(1)[0][0] if periods else None,
            period_start=min((d.occurred_at for d in adapter_result.drafts), default=None).isoformat(sep=" ") if adapter_result.drafts else None,
            period_end=max((d.occurred_at for d in adapter_result.drafts), default=None).isoformat(sep=" ") if adapter_result.drafts else None,
            account_id=adapter_result.account_hint,
            warnings=adapter_result.warnings,
            items=[d.as_dict() for d in adapter_result.drafts[:20]],
            meta={**adapter_result.meta, "detection_score": round(score, 3), "headers": table.headers},
            message=f"预演模式：识别 {adapter.name} 格式，未写入数据库。",
        )

    def _archive(self, path: Path) -> Path:
        safe_name = path.name.replace("/", "_").replace("\\", "_")
        digest = file_sha256(path)[:12]
        target = self.settings.archive_dir / f"{digest}_{safe_name}"
        if not target.exists():
            shutil.copy2(path, target)
            try:
                target.chmod(0o444)
            except OSError:  # pragma: no cover - exotic filesystems
                pass
        return target

    def _relocate(self, path: Path, kind: str) -> None:
        """Move a processed inbox file; files outside the inbox are left alone."""
        try:
            path.resolve().relative_to(self.settings.inbox_dir.resolve())
        except ValueError:
            return
        target_dir = self.settings.processed_dir if kind == "processed" else self.settings.failed_dir
        target = target_dir / path.name
        if target.exists():
            target = target_dir / f"{datetime.now():%Y%m%d-%H%M%S}_{path.name}"
        shutil.move(str(path), target)

    def _persist(
        self,
        session,
        *,
        household_id: str,
        batch: ImportBatch,
        adapter_result: AdapterResult,
        adapter: BillAdapter,
        account_ref: str | None,
        actor: str,
    ) -> ImportReport:
        accounts = AccountService(session, self.settings)
        account = accounts.resolve(account_ref, platform=adapter.platform)
        batch.account_id = account.id
        tsvc = TransactionService(session, self.settings)
        classifier = RuleClassifier(session, household_id)
        report = ImportReport(
            file=batch.stored_path,
            status="imported",
            batch_id=batch.id,
            platform=adapter.platform,
            adapter=adapter.name,
            total_rows=adapter_result.total_rows,
            failed=len(adapter_result.errors),
            errors=list(adapter_result.errors),
            archive_path=batch.archive_path,
            account_id=account.id,
            warnings=list(adapter_result.warnings),
            meta=adapter_result.meta,
        )
        periods: Counter[str] = Counter()
        imported_txns: list = []

        for draft in adapter_result.drafts:
            try:
                created = tsvc.create(
                    account_id=account.id,
                    occurred_at=draft.occurred_at,
                    direction=draft.direction,
                    amount_minor=draft.amount_minor,
                    txn_type=draft.txn_type,
                    currency=draft.currency,
                    external_id=draft.external_id,
                    counterparty=draft.counterparty,
                    description=draft.description,
                    source_platform=draft.platform,
                    source_batch_id=batch.id,
                    status=draft.status,
                    review_reason=draft.review_reason,
                    notes=draft.account_hint,
                )
            except Exception as exc:  # noqa: BLE001 - row-level isolation
                report.errors.append(RowError(draft.row_number, str(exc), raw=draft.raw))
                report.failed += 1
                session.add(
                    RawTransaction(
                        id=new_id("raw"),
                        batch_id=batch.id,
                        row_number=draft.row_number,
                        raw_json=json.dumps(draft.raw, ensure_ascii=False, default=str),
                        parse_status="failed",
                        error_message=str(exc),
                    )
                )
                continue

            raw = RawTransaction(
                id=new_id("raw"),
                batch_id=batch.id,
                row_number=draft.row_number,
                raw_json=json.dumps(draft.raw, ensure_ascii=False, default=str),
                parse_status="parsed",
                transaction_id=created.transaction.id,
            )
            session.add(raw)

            if created.duplicate:
                report.duplicates += 1
                raw.parse_status = "duplicate"
                if not created.transaction.source_batch_id:
                    created.transaction.source_batch_id = batch.id
                continue
            report.imported += 1
            imported_txns.append(created.transaction)
            periods[created.transaction.occurred_at.strftime("%Y-%m")] += 1
            if created.transaction.status == TxnStatus.PENDING_REVIEW:
                pass
            else:
                classifier.apply(created.transaction)

        if periods:
            report.period = periods.most_common(1)[0][0]
            dates = [t.occurred_at for t in imported_txns]
            report.period_start = min(dates).isoformat(sep=" ")
            report.period_end = max(dates).isoformat(sep=" ")
            batch.period = report.period

        batch.imported_rows = report.imported
        batch.duplicate_rows = report.duplicates
        batch.failed_rows = report.failed
        batch.finished_at = datetime.now().replace(microsecond=0)
        batch.status = ImportStatus.IMPORTED if report.imported or report.duplicates else ImportStatus.FAILED
        if report.errors:
            batch.error_summary = "\n".join(
                f"row {e.row_number}: {e.message}" for e in report.errors[:20]
            )
            report.meta["error_report"] = str(self._write_error_report(batch, report))
            batch.error_report_path = report.meta["error_report"]
        if batch.status == ImportStatus.FAILED:
            report.status = "failed"
            report.message = "没有成功导入任何交易，请检查账单格式或字段映射。"

        record_audit(
            session,
            household_id=household_id,
            action="import",
            entity_type="import_batch",
            entity_id=batch.id,
            after={
                "file": batch.file_name,
                "platform": adapter.platform,
                "imported": report.imported,
                "duplicates": report.duplicates,
                "failed": report.failed,
            },
            actor=actor,
        )
        return report

    def _write_error_report(self, batch: ImportBatch, report: ImportReport) -> Path:
        path = self.settings.failed_dir / f"{batch.id}.errors.json"
        payload = {
            "batch_id": batch.id,
            "file": batch.file_name,
            "errors": [
                {"row": e.row_number, "message": e.message, "raw": e.raw}
                for e in report.errors
            ],
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        return path
