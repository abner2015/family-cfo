"""Family CFO command line interface.

Every mutation goes through the service layer; the CLI only parses input,
prints results and maps errors to exit codes.
"""

from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

import typer
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from .assets.accounts import AccountService, format_account
from .assets.service import AssetService
from .automation import schedule as automation_schedule
from .automation import tasks as automation_tasks
from .budgets.service import BudgetService
from .core.config import load_config, redact_config
from .core.dates import parse_datetime, parse_period, previous_period, today_local
from .core.errors import FamilyCFOError, ValidationError, exit_code_for
from .core.logging import setup_logging
from .core.money import format_money, to_minor
from .database.backup import (
    backup_database,
    prune_backups,
    restore_database,
    verify_backup,
)
from .database.base import Database
from .database.init import initialize
from .database.integrity import run_integrity_checks
from .demo import seed_demo
from .database.models import (
    FamilyMember,
    Transaction,
    TransactionCategory,
)
from .finance.metrics import build_metrics, monthly_trend
from .finance.health import diagnose
from .finance.query import answer_question
from .finance.settlement import SettlementService
from .importers.mapping import load_profiles
from .importers.service import ImportService
from .integrations.feishu.client import FeishuClient
from .integrations.feishu.sync import FeishuSyncService
from .investments.service import InvestmentService
from .liabilities.service import LiabilityService
from .planning.service import PlanningService
from .reports.annual import save_annual
from .reports.excel import export_annual, export_monthly
from .reports.monthly import save_monthly
from .transactions.nlp import parse_entry
from .transactions.reconcile import Reconciler
from .transactions.service import TransactionService

app = typer.Typer(
    name="family-cfo",
    help="Family CFO — 本地家庭财务账本、月度结算、飞书同步与报告。",
    no_args_is_help=True,
    add_completion=False,
)
tx_app = typer.Typer(help="交易查询与修正", no_args_is_help=True)
category_app = typer.Typer(help="分类管理", no_args_is_help=True)
account_app = typer.Typer(help="账户管理", no_args_is_help=True)
asset_app = typer.Typer(help="资产管理", no_args_is_help=True)
liability_app = typer.Typer(help="负债管理", no_args_is_help=True)
budget_app = typer.Typer(help="预算管理", no_args_is_help=True)
goal_app = typer.Typer(help="财务目标", no_args_is_help=True)
invest_app = typer.Typer(help="投资管理", no_args_is_help=True)
feishu_app = typer.Typer(help="飞书多维表格同步", no_args_is_help=True)
backup_app = typer.Typer(help="备份与恢复", no_args_is_help=True)
automation_app = typer.Typer(help="自动化任务", no_args_is_help=True)
db_app = typer.Typer(help="数据库与配置", no_args_is_help=True)

for sub, name in (
    (tx_app, "tx"),
    (category_app, "category"),
    (account_app, "account"),
    (asset_app, "asset"),
    (liability_app, "liability"),
    (budget_app, "budget"),
    (goal_app, "goal"),
    (invest_app, "invest"),
    (feishu_app, "feishu"),
    (backup_app, "backup"),
    (automation_app, "automation"),
    (db_app, "db"),
):
    app.add_typer(sub, name=name)


@dataclass
class CliState:
    root: Path
    db_path: Path | None = None
    as_json: bool = False
    verbose: bool = False


@contextmanager
def friendly_errors():
    try:
        yield
    except FamilyCFOError as exc:
        typer.secho(f"错误[{exc.code}] {exc.message}", fg=typer.colors.RED, err=True)
        if exc.detail:
            typer.secho(exc.detail, fg=typer.colors.YELLOW, err=True)
        suggestion = getattr(exc, "suggestion", None)
        if suggestion:
            typer.secho(
                "建议字段映射：\n" + json.dumps(suggestion, ensure_ascii=False, indent=2),
                fg=typer.colors.YELLOW,
                err=True,
            )
        raise typer.Exit(exit_code_for(exc))
    except SQLAlchemyError as exc:
        typer.secho(f"数据库错误: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    except ValueError as exc:
        typer.secho(f"错误: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(2)


@app.callback()
def main_callback(
    ctx: typer.Context,
    root: Optional[Path] = typer.Option(None, "--root", help="项目根目录（默认自动查找）"),
    db: Optional[Path] = typer.Option(None, "--db", help="SQLite 数据库路径"),
    json_output: bool = typer.Option(False, "--json", help="以 JSON 输出"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="输出调试日志"),
) -> None:
    ctx.obj = CliState(
        root=(root or Path.cwd()).resolve(),
        db_path=db.resolve() if db else None,
        as_json=json_output,
        verbose=verbose,
    )
    setup_logging("DEBUG" if verbose else "WARNING")


def _settings(state: CliState):
    overrides = {}
    if state.db_path is not None:
        overrides["db_path"] = state.db_path
    return load_config(root=state.root, overrides=overrides or None)


def _db(state: CliState, *, require: bool = True) -> Database:
    settings = _settings(state)
    if require and not settings.db_path.exists():
        raise ValidationError(
            f"数据库不存在: {settings.db_path}。请先运行 `family-cfo init`。"
        )
    return Database.open(settings.db_path)


def _emit(state: CliState, data: Any, *, title: str | None = None) -> None:
    if state.as_json:
        typer.echo(json.dumps(data, ensure_ascii=False, indent=2, default=str))
        return
    if title:
        typer.secho(title, bold=True)
    if isinstance(data, list) and data and isinstance(data[0], dict):
        headers = list(data[0].keys())
        scalar_only = all(
            not isinstance(value, (dict, list))
            for row in data
            for value in row.values()
        )
        if scalar_only and len(headers) <= 10:
            widths = {
                h: min(max(len(str(h)), *(len(str(row.get(h, ""))) for row in data)), 40)
                for h in headers
            }
            typer.echo("  ".join(h.ljust(widths[h]) for h in headers))
            for row in data:
                typer.echo(
                    "  ".join(str(row.get(h, ""))[: widths[h]].ljust(widths[h]) for h in headers)
                )
        else:
            for row in data:
                typer.echo(json.dumps(row, ensure_ascii=False, default=str))
    elif isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, (dict, list)):
                typer.echo(f"{key}: {json.dumps(value, ensure_ascii=False, default=str)}")
            else:
                typer.echo(f"{key}: {value}")
    else:
        typer.echo(str(data))


def _period_or_current(value: str | None, settings) -> str:
    if value is None:
        return today_local(settings.timezone).strftime("%Y-%m")
    text = str(value).strip().lower()
    if text in {"current", "this", "本月"}:
        return today_local(settings.timezone).strftime("%Y-%m")
    if text in {"last", "上月"}:
        return previous_period(today_local(settings.timezone).strftime("%Y-%m"))
    return parse_period(value)


def _currency(settings) -> str:
    return settings.currency


# ---------------------------------------------------------------------------
# init / doctor
# ---------------------------------------------------------------------------
@app.command()
def init(
    ctx: typer.Context,
    household: Optional[str] = typer.Option(None, "--household", help="家庭名称"),
    member: str = typer.Option("我", "--member", help="主要成员名称"),
    reset: bool = typer.Option(False, "--reset", help="重建数据库（危险，会删除现有文件）"),
    force: bool = typer.Option(False, "--force", help="与 --reset 配合确认删除"),
) -> None:
    """初始化目录、SQLite 数据库、默认分类与账户。"""
    state: CliState = ctx.obj
    with friendly_errors():
        settings = _settings(state)
        settings.ensure_dirs()
        if reset and not force:
            raise ValidationError("--reset 会删除现有数据库；请同时传入 --force。")
        if reset and settings.db_path.exists():
            if settings.auto_backup:
                backup_database(settings.db_path, settings.backups_dir, label="pre-reset")
            settings.db_path.unlink()
        db = Database.open(settings.db_path)
        result = initialize(db, settings, household_name=household, member_name=member, reset=reset)
        payload = {
            "database": str(settings.db_path),
            "household_id": result.household_id,
            "member_id": result.member_id,
            "created": result.created,
            "categories": result.category_count,
            "accounts": result.account_count,
        }
        _emit(state, payload, title="初始化完成")
        if not state.as_json:
            typer.echo(
                "\n下一步：把账单放入 data/inbox 后运行 `family-cfo scan`；"
                "或在飞书中配置多维表格后运行 `family-cfo feishu ensure-tables`。"
            )


@app.command()
def demo(
    ctx: typer.Context,
    yes: bool = typer.Option(False, "--yes", help="确认写入合成示例数据"),
) -> None:
    """写入合成示例数据（仅空账本，必须显式确认）。"""
    state: CliState = ctx.obj
    with friendly_errors():
        settings = _settings(state)
        if not yes:
            raise ValidationError(
                "demo 会写入合成示例交易/资产/负债，必须使用 --yes 明确确认。"
            )
        payload = seed_demo(_db(state), settings)
        _emit(state, payload, title="示例数据已写入")


@app.command()
def doctor(ctx: typer.Context) -> None:
    """检查环境、配置与数据库状态。"""
    state: CliState = ctx.obj
    settings = _settings(state)
    info: dict[str, Any] = {
        "python": f"{sys.version.split()[0]}",
        "project_root": str(settings.project_root),
        "data_dir": str(settings.data_dir),
        "database": str(settings.db_path),
        "database_exists": settings.db_path.exists(),
        "currency": settings.currency,
        "timezone": settings.timezone,
        "feishu_configured": settings.feishu_configured,
    }
    if settings.db_path.exists():
        db = Database.open(settings.db_path)
        try:
            with db.session() as session:
                report = run_integrity_checks(session, _household_id(session))
                info["integrity_ok"] = report.ok
                info["integrity_warnings"] = [c.name for c in report.warnings]
                info["transactions"] = sum(1 for _ in session.scalars(select(Transaction)))
        finally:
            db.close()
    else:
        info["hint"] = "运行 `family-cfo init` 创建数据库"
    _emit(state, info, title="Family CFO 环境检查")


def _household_id(session) -> str:
    from .database.init import get_household

    return get_household(session).id


# ---------------------------------------------------------------------------
# import / scan / reconcile / settle
# ---------------------------------------------------------------------------
@app.command("import")
def import_cmd(
    ctx: typer.Context,
    path: Path = typer.Argument(..., help="账单文件或目录"),
    platform: str = typer.Option("auto", "--platform", help="auto/wechat/alipay/bank"),
    account: Optional[str] = typer.Option(None, "--account", help="账户名称或 ID"),
    mapping_name: Optional[str] = typer.Option(None, "--mapping-name", help="config/mappings.yaml 中的映射名"),
    mapping_json: Optional[str] = typer.Option(None, "--mapping", help='列映射 JSON，如 {"date":"交易日期","amount":"金额"}'),
    dry_run: bool = typer.Option(False, "--dry-run", help="只解析不写库"),
) -> None:
    """导入单个账单文件（或目录中的全部账单）。"""
    state: CliState = ctx.obj
    with friendly_errors():
        settings = _settings(state)
        db = _db(state)
        mapping = json.loads(mapping_json) if mapping_json else None
        service = ImportService(db, settings)
        if path.is_dir():
            reports = []
            for file in sorted(path.iterdir()):
                if file.suffix.lower() in {".csv", ".txt", ".xlsx", ".xls"}:
                    reports.append(
                        service.import_file(
                            file,
                            platform=platform,
                            account=account,
                            mapping=mapping,
                            mapping_name=mapping_name,
                            dry_run=dry_run,
                        )
                    )
            payload = [r.as_dict() for r in reports]
        else:
            payload = service.import_file(
                path,
                platform=platform,
                account=account,
                mapping=mapping,
                mapping_name=mapping_name,
                dry_run=dry_run,
            ).as_dict()
        _emit(state, payload, title="导入结果")


@app.command()
def scan(
    ctx: typer.Context,
    directory: Optional[Path] = typer.Option(None, "--dir", help="收件目录（默认 data/inbox）"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只解析不写库"),
) -> None:
    """扫描收件目录并导入所有新账单。"""
    state: CliState = ctx.obj
    with friendly_errors():
        settings = _settings(state)
        service = ImportService(_db(state), settings)
        reports = service.scan_inbox(dry_run=dry_run, directory=directory)
        _emit(state, [r.as_dict() for r in reports], title=f"扫描 {len(reports)} 个文件")


@app.command()
def reconcile(
    ctx: typer.Context,
    month: Optional[str] = typer.Option(None, "--month", help="YYYY-MM，默认本月"),
    all_time: bool = typer.Option(False, "--all", help="对全部历史交易做一次对账"),
) -> None:
    """识别重复交易、内部转账、退款与信用卡还款。"""
    state: CliState = ctx.obj
    with friendly_errors():
        settings = _settings(state)
        db = _db(state)
        period = None if all_time else _period_or_current(month, settings)
        with db.session() as session:
            result = Reconciler(session, settings, period=period).run()
        _emit(state, result.as_dict(), title=f"对账完成 {period or '全部历史'}")


@app.command()
def settle(
    ctx: typer.Context,
    month: Optional[str] = typer.Option(None, "--month", help="YYYY-MM，默认本月"),
    status: bool = typer.Option(False, "--status", help="只查看结算状态"),
    collect: bool = typer.Option(False, "--collect", help="归集账单：扫描 + 对账"),
    close: bool = typer.Option(False, "--close", help="执行结算关账"),
    reopen: bool = typer.Option(False, "--reopen", help="重新打开已结算月份"),
    reason: Optional[str] = typer.Option(None, "--reason", help="覆盖/重开原因（必填）"),
    pending_policy: Optional[str] = typer.Option(
        None, "--pending-policy", help="待确认交易策略：suspend（暂挂，不计入统计）"
    ),
    mark_missing: Optional[str] = typer.Option(None, "--mark-missing", help="标记缺失渠道，如 bank"),
) -> None:
    """月度结算状态机：归集 → 对账 → 审核 → 关账。"""
    state: CliState = ctx.obj
    with friendly_errors():
        settings = _settings(state)
        db = _db(state)
        period = _period_or_current(month, settings)
        if collect:
            payload = automation_tasks.monthly_collect(db, settings, period)
            _emit(state, payload, title=f"{period} 账单归集")
            return
        with db.session() as session:
            service = SettlementService(session, settings)
            if mark_missing:
                if not reason:
                    raise ValidationError("--mark-missing 需要 --reason 说明缺失原因")
                service.mark_source(period, mark_missing, status="missing", reason=reason)
            if reopen:
                if not reason:
                    raise ValidationError("--reopen 需要 --reason")
                record = service.reopen(period, reason=reason)
                _emit(state, {"period": record.period, "status": record.status, "reopened": record.reopened_count})
                return
            if close:
                record = service.close(
                    period, override_reason=reason, pending_policy=pending_policy
                )
                evaluation = service.evaluate(period)
                _emit(
                    state,
                    {
                        "period": record.period,
                        "status": record.status,
                        "closed_at": str(record.closed_at),
                        "override": bool(record.override_reason),
                        "totals": evaluation.totals,
                    },
                    title=f"{period} 已结算",
                )
                return
            evaluation = service.evaluate(period)
        _emit(state, evaluation.as_dict(), title=f"{period} 结算状态")


# ---------------------------------------------------------------------------
# analyze / report
# ---------------------------------------------------------------------------
@app.command()
def analyze(
    ctx: typer.Context,
    month: Optional[str] = typer.Option(None, "--month", help="YYYY-MM，默认本月"),
    week: bool = typer.Option(False, "--week", help="输出最近 7 天摘要"),
) -> None:
    """查看月度指标、分类结构与趋势。"""
    state: CliState = ctx.obj
    with friendly_errors():
        settings = _settings(state)
        db = _db(state)
        if week:
            payload = automation_tasks.weekly_digest(db, settings)
            _emit(state, payload, title=payload["start"] + " 周报")
            return
        with db.session() as session:
            period = _period_or_current(month, settings)
            payload = build_metrics(session, settings, period=period)
            payload["trend"] = [s.as_dict() for s in monthly_trend(session, _household_id(session), end_period=period, months=6)]
        if state.as_json:
            _emit(state, payload)
            return
        summary = payload["summary"]
        typer.secho(f"{period} 家庭财务概览", bold=True)
        for key, label in (("income", "收入"), ("expense", "支出"), ("net", "结余")):
            typer.echo(f"{label}: {summary[key]}")
        typer.echo(f"储蓄率: {summary['savings_rate'] or '—'}")
        typer.echo("")
        for metric in payload["metrics"]:
            typer.echo(f"- {metric['label']}: {metric['value'] or '—'} [{metric['status']}]")
        typer.echo("")
        typer.secho("支出分类：", bold=True)
        for row in payload["categories"]:
            if row["direction"] == "outflow":
                typer.echo(f"- {row['category']}: {row['amount']} ({row['count']} 笔)")
        typer.echo("")
        typer.secho("近六个月趋势：", bold=True)
        for row in payload["trend"]:
            typer.echo(f"- {row['period']}: 收入 {row['income']} / 支出 {row['expense']} / 结余 {row['net']}")


@app.command()
def health(
    ctx: typer.Context,
    month: Optional[str] = typer.Option(None, "--month", help="YYYY-MM，默认本月"),
) -> None:
    """家庭财务健康诊断（规则评分，数据不足时明确标注）。"""
    state: CliState = ctx.obj
    with friendly_errors():
        settings = _settings(state)
        db = _db(state)
        period = _period_or_current(month, settings)
        with db.session() as session:
            report = diagnose(session, settings, period=period)
        payload = report.as_dict()
        if state.as_json:
            _emit(state, payload)
            return
        score = payload["score"]
        typer.secho(
            f"{period} 家庭财务健康：{payload['grade']}"
            + (f"（{score}/100）" if score is not None else ""),
            bold=True,
        )
        typer.echo("")
        for finding in payload["findings"]:
            typer.echo(
                f"- {finding['label']}: {finding['value'] or '—'} "
                f"[{finding['status_label']}] 阈值 {finding['threshold']}"
            )
            if finding["suggestion"]:
                typer.echo(f"    建议：{finding['suggestion']}")
        if payload["unavailable"]:
            typer.echo("")
            typer.echo("数据不足：" + "、".join(payload["unavailable"]))
        typer.echo("")
        typer.echo(payload["disclaimer"])


@app.command()
def report(
    ctx: typer.Context,
    month: Optional[str] = typer.Option(None, "--month", help="YYYY-MM 月报"),
    year: Optional[int] = typer.Option(None, "--year", help="年度报告年份"),
    fmt: str = typer.Option("md", "--format", help="md / xlsx / md,xlsx"),
    out: Optional[Path] = typer.Option(None, "--out", help="输出目录"),
) -> None:
    """生成月度或年度报告（Markdown / Excel）。"""
    state: CliState = ctx.obj
    with friendly_errors():
        settings = _settings(state)
        db = _db(state)
        formats = {f.strip().lower() for f in fmt.split(",") if f.strip()}
        if not formats <= {"md", "xlsx"}:
            raise ValidationError("--format 只支持 md 与 xlsx")
        with db.session() as session:
            if year is not None:
                payload, markdown, md_path = save_annual(session, settings, year, out_dir=out)
                xlsx = None
                if "xlsx" in formats:
                    target = (Path(out) if out else settings.reports_dir) / f"{year}-annual-report.xlsx"
                    xlsx = export_annual(session, settings, payload, target)
                result = {"year": year, "markdown": str(md_path), "excel": str(xlsx) if xlsx else None}
            else:
                period = _period_or_current(month, settings)
                payload, markdown, md_path = save_monthly(session, settings, period, out_dir=out)
                xlsx = None
                if "xlsx" in formats:
                    target = (Path(out) if out else settings.reports_dir) / f"{period}-monthly-report.xlsx"
                    xlsx = export_monthly(session, settings, payload, target)
                result = {"period": period, "markdown": str(md_path), "excel": str(xlsx) if xlsx else None}
        if state.as_json:
            _emit(state, result)
        else:
            _emit(state, result, title="报告已生成")
            typer.echo("\n" + markdown[:1500] + ("\n..." if len(markdown) > 1500 else ""))


# ---------------------------------------------------------------------------
# record / tx
# ---------------------------------------------------------------------------
@app.command()
def ask(
    ctx: typer.Context,
    question: str = typer.Argument(..., help="自然语言财务问题，如：这个月教育支出多少"),
) -> None:
    """用自然语言查询账本（确定性解析，不做心算）。"""
    state: CliState = ctx.obj
    with friendly_errors():
        settings = _settings(state)
        db = _db(state)
        with db.session() as session:
            payload = answer_question(question, session, settings).as_dict()
        if state.as_json:
            _emit(state, payload)
            return
        typer.secho(payload["answer"], bold=True)
        if payload["data_status"] == "unavailable":
            typer.secho("（数据不足，未给出估算值）", fg=typer.colors.YELLOW)
        if payload["suggestions"] and payload["intent"] == "unknown":
            typer.echo("")
            typer.echo("可以这样问：")
            for item in payload["suggestions"]:
                typer.echo(f"  - {item}")


@app.command()
def record(
    ctx: typer.Context,
    text: str = typer.Argument(..., help="自然语言，如：午餐 35 元"),
    account: Optional[str] = typer.Option(None, "--account", help="账户名称"),
    yes: bool = typer.Option(False, "--yes", "-y", help="跳过确认"),
    when: Optional[str] = typer.Option(None, "--date", help="覆盖日期 YYYY-MM-DD"),
) -> None:
    """自然语言记账（补充入口，默认需要确认）。"""
    state: CliState = ctx.obj
    with friendly_errors():
        settings = _settings(state)
        db = _db(state)
        with db.session() as session:
            parsed = parse_entry(text, session, settings)
            if when:
                parsed.occurred_at = parse_datetime(when)
            _emit(state, parsed.as_dict(settings.currency), title="记账预览")
            if not yes:
                typer.confirm("确认写入账本？", abort=True)
            accounts = AccountService(session, settings)
            target = accounts.resolve(account, platform="manual")
            tsvc = TransactionService(session, settings)
            created = tsvc.create(
                account_id=target.id,
                occurred_at=parsed.occurred_at,
                direction=parsed.direction,
                amount_minor=parsed.amount_minor,
                txn_type=parsed.txn_type,
                counterparty=parsed.description,
                description=parsed.description,
                source_platform="manual",
                category_id=parsed.category_id,
                status="confirmed" if parsed.category_id else "pending_review",
                review_reason=None if parsed.category_id else "uncategorized",
                is_shared=True,
                audit=True,
            )
            classification = "已分类" if parsed.category_id else "待确认"
            _emit(
                state,
                {"id": created.transaction.id, "amount": format_money(created.transaction.amount_minor), "status": classification},
                title="已写入",
            )


@tx_app.command("list")
def tx_list(
    ctx: typer.Context,
    month: Optional[str] = typer.Option(None, "--month"),
    status: Optional[str] = typer.Option(None, "--status", help="confirmed/pending_review/void"),
    review: bool = typer.Option(False, "--review", help="只看待确认"),
    limit: int = typer.Option(50, "--limit"),
) -> None:
    """列出交易。"""
    state: CliState = ctx.obj
    with friendly_errors():
        settings = _settings(state)
        db = _db(state)
        period = _period_or_current(month, settings) if month else None
        with db.session() as session:
            service = TransactionService(session, settings)
            rows = service.list(period=period, status=status, only_review=review, limit=limit)
            payload = [
                {
                    "id": t.id,
                    "date": t.occurred_at.strftime("%Y-%m-%d"),
                    "amount": format_money(t.amount_minor, t.currency),
                    "type": t.txn_type,
                    "counterparty": t.counterparty or "",
                    "description": (t.description or "")[:30],
                    "status": t.status,
                    "reason": t.review_reason or "",
                }
                for t in rows
            ]
        _emit(state, payload, title=f"{len(payload)} 笔交易")


@tx_app.command("fix")
def tx_fix(
    ctx: typer.Context,
    txn_id: str = typer.Argument(...),
    category: str = typer.Option(..., "--category", help="分类名称/ID/slug"),
    rule: bool = typer.Option(False, "--rule", help="沉淀关键词规则"),
) -> None:
    """修正交易分类。"""
    state: CliState = ctx.obj
    with friendly_errors():
        settings = _settings(state)
        db = _db(state)
        with db.session() as session:
            category_row = _find_category(session, category)
            txn = TransactionService(session, settings).categorize(
                txn_id, category_row.id, create_rule=rule
            )
            _emit(state, {"id": txn.id, "category": category_row.name, "status": txn.status})


@tx_app.command("retype")
def tx_retype(
    ctx: typer.Context,
    txn_id: str = typer.Argument(...),
    txn_type: str = typer.Option(..., "--type", help="expense/income/transfer/loan_repay/..."),
    direction: Optional[str] = typer.Option(None, "--direction", help="inflow/outflow"),
    include: bool = typer.Option(False, "--include", help="强制计入收支统计"),
    exclude: bool = typer.Option(False, "--exclude", help="强制排除出收支统计"),
    reason: str = typer.Option(..., "--reason", help="修正原因（审计必填）"),
) -> None:
    """修正交易类型/方向（用于把误判的转账改回支出等）。"""
    if include and exclude:
        raise typer.BadParameter("--include 与 --exclude 不能同时使用")
    state: CliState = ctx.obj
    with friendly_errors():
        settings = _settings(state)
        db = _db(state)
        stats = True if include else False if exclude else None
        with db.session() as session:
            txn = TransactionService(session, settings).retype(
                txn_id,
                txn_type=txn_type,
                direction=direction,
                stats_included=stats,
                reason=reason,
            )
            _emit(
                state,
                {
                    "id": txn.id,
                    "type": txn.txn_type,
                    "direction": txn.direction,
                    "stats_included": txn.stats_included,
                    "status": txn.status,
                },
            )


@tx_app.command("delete")
def tx_delete(
    ctx: typer.Context,
    txn_id: str = typer.Argument(...),
    reason: str = typer.Option(..., "--reason", help="删除原因（审计必填）"),
) -> None:
    """软删除交易（保留审计与冲正记录）。"""
    state: CliState = ctx.obj
    with friendly_errors():
        settings = _settings(state)
        db = _db(state)
        with db.session() as session:
            txn = TransactionService(session, settings).soft_delete(txn_id, reason=reason)
            _emit(state, {"id": txn.id, "status": txn.status, "deleted": txn.is_deleted})


@tx_app.command("restore")
def tx_restore(ctx: typer.Context, txn_id: str = typer.Argument(...)) -> None:
    """恢复被软删除的交易。"""
    state: CliState = ctx.obj
    with friendly_errors():
        settings = _settings(state)
        db = _db(state)
        with db.session() as session:
            txn = TransactionService(session, settings).restore(txn_id)
            _emit(state, {"id": txn.id, "status": txn.status, "deleted": txn.is_deleted})


@tx_app.command("resolve")
def tx_resolve(
    ctx: typer.Context,
    txn_id: str = typer.Argument(...),
    distinct: bool = typer.Option(False, "--distinct", help="确认是两笔独立交易"),
    duplicate: bool = typer.Option(False, "--duplicate", help="确认是重复交易，作废该笔"),
) -> None:
    """处理疑似重复交易。"""
    if distinct == duplicate:
        raise typer.BadParameter("必须且只能指定 --distinct 或 --duplicate")
    state: CliState = ctx.obj
    with friendly_errors():
        settings = _settings(state)
        db = _db(state)
        with db.session() as session:
            txn = Reconciler(session, settings).confirm_duplicate(txn_id, distinct=distinct)
            _emit(state, {"id": txn.id, "status": txn.status, "stats_included": txn.stats_included})


# ---------------------------------------------------------------------------
# categories
# ---------------------------------------------------------------------------
@category_app.command("add")
def category_add(
    ctx: typer.Context,
    name: str = typer.Argument(...),
    kind: str = typer.Option("expense", "--kind", help="expense/income/transfer/excluded"),
    essential: bool = typer.Option(False, "--essential", help="是否必要支出"),
    keyword: list[str] = typer.Option([], "--keyword", help="关键词，可重复"),
    parent: Optional[str] = typer.Option(None, "--parent", help="父分类 ID"),
) -> None:
    """新增自定义分类。"""
    state: CliState = ctx.obj
    with friendly_errors():
        settings = _settings(state)
        db = _db(state)
        with db.session() as session:
            category = TransactionService(session, settings).create_category(
                name=name,
                kind=kind,
                parent_id=parent,
                is_essential=essential,
                keywords=list(keyword) or None,
            )
            _emit(
                state,
                {"id": category.id, "name": category.name, "kind": category.kind,
                 "slug": category.slug, "essential": category.is_essential},
            )


@category_app.command("list")
def category_list(
    ctx: typer.Context,
    kind: Optional[str] = typer.Option(None, "--kind"),
) -> None:
    """列出分类。"""
    state: CliState = ctx.obj
    with friendly_errors():
        settings = _settings(state)
        db = _db(state)
        with db.session() as session:
            rows = [
                {
                    "name": c.name,
                    "kind": c.kind,
                    "slug": c.slug,
                    "essential": c.is_essential,
                    "system": c.is_system,
                    "id": c.id,
                }
                for c in TransactionService(session, settings).list_categories(kind=kind)
            ]
        _emit(state, rows, title="分类")


# ---------------------------------------------------------------------------
# accounts
# ---------------------------------------------------------------------------
@account_app.command("list")
def account_list(ctx: typer.Context) -> None:
    """列出账户与余额。"""
    state: CliState = ctx.obj
    with friendly_errors():
        settings = _settings(state)
        db = _db(state)
        with db.session() as session:
            rows = [format_account(a) for a in AccountService(session, settings).list()]
        _emit(state, rows, title="账户")


@account_app.command("add")
def account_add(
    ctx: typer.Context,
    name: str = typer.Argument(...),
    account_type: str = typer.Option("bank", "--type", help="bank/cash/credit_card/payment_platform/investment/other"),
    institution: Optional[str] = typer.Option(None, "--institution"),
    platform: Optional[str] = typer.Option(None, "--platform"),
    currency: Optional[str] = typer.Option(None, "--currency"),
    opening: Optional[str] = typer.Option(None, "--opening", help="期初余额"),
) -> None:
    """新增账户。"""
    state: CliState = ctx.obj
    with friendly_errors():
        settings = _settings(state)
        db = _db(state)
        with db.session() as session:
            opening_minor = to_minor(opening, currency or settings.currency) if opening else 0
            account = AccountService(session, settings).create(
                name=name,
                account_type=account_type,
                institution=institution,
                platform=platform,
                currency=currency,
                opening_balance_minor=opening_minor,
                is_liability=account_type == "credit_card",
                is_asset=account_type != "credit_card",
            )
            _emit(state, format_account(account))


@account_app.command("balance")
def account_balance(
    ctx: typer.Context,
    name: str = typer.Argument(...),
    amount: str = typer.Option(..., "--amount"),
    as_of: Optional[str] = typer.Option(None, "--as-of", help="YYYY-MM-DD"),
) -> None:
    """设置账户当前余额（用于对账）。"""
    state: CliState = ctx.obj
    with friendly_errors():
        settings = _settings(state)
        db = _db(state)
        with db.session() as session:
            account = AccountService(session, settings).get(name)
            value = to_minor(amount, account.currency)
            day = date.fromisoformat(as_of) if as_of else today_local(settings.timezone)
            account = AccountService(session, settings).set_balance(name, value, as_of=day)
            _emit(state, format_account(account))


# ---------------------------------------------------------------------------
# assets
# ---------------------------------------------------------------------------
@asset_app.command("list")
def asset_list(ctx: typer.Context) -> None:
    """列出资产与估值。"""
    state: CliState = ctx.obj
    with friendly_errors():
        settings = _settings(state)
        db = _db(state)
        with db.session() as session:
            summary = AssetService(session, settings).summary()
        _emit(state, summary, title="资产")


@asset_app.command("add")
def asset_add(
    ctx: typer.Context,
    name: str = typer.Argument(...),
    asset_type: str = typer.Option("property", "--type", help="deposit/cash/property/vehicle/stock/fund/bond/wealth_mgmt/other"),
    value: str = typer.Option(..., "--value", help="当前估值"),
    cost: Optional[str] = typer.Option(None, "--cost", help="原始成本"),
    liquid: bool = typer.Option(False, "--liquid", help="是否可快速变现"),
    residence: bool = typer.Option(False, "--residence", help="是否自住房"),
    valued_on: Optional[str] = typer.Option(None, "--valued-on", help="估值日期 YYYY-MM-DD"),
    member: Optional[str] = typer.Option(None, "--member"),
) -> None:
    """新增资产。"""
    state: CliState = ctx.obj
    with friendly_errors():
        settings = _settings(state)
        db = _db(state)
        with db.session() as session:
            member_id = _find_member(session, member).id if member else None
            asset = AssetService(session, settings).add(
                name=name,
                asset_type=asset_type,
                value=value,
                acquisition_cost=cost,
                is_liquid=liquid,
                is_primary_residence=residence,
                member_id=member_id,
                valued_on=date.fromisoformat(valued_on) if valued_on else None,
            )
            _emit(state, {"id": asset.id, "name": asset.name, "value": format_money(asset.current_value_minor, asset.currency)})


@asset_app.command("value")
def asset_value(
    ctx: typer.Context,
    name: str = typer.Argument(...),
    value: str = typer.Option(..., "--value"),
    valued_on: Optional[str] = typer.Option(None, "--valued-on"),
    source: str = typer.Option("manual", "--source"),
) -> None:
    """更新资产估值（保留历史）。"""
    state: CliState = ctx.obj
    with friendly_errors():
        settings = _settings(state)
        db = _db(state)
        with db.session() as session:
            asset = AssetService(session, settings).value(
                name,
                value=value,
                valued_on=date.fromisoformat(valued_on) if valued_on else None,
                source=source,
            )
            _emit(state, {"id": asset.id, "value": format_money(asset.current_value_minor, asset.currency), "date": str(asset.valuation_date)})


@asset_app.command("history")
def asset_history(ctx: typer.Context, name: str = typer.Argument(...)) -> None:
    """查看估值历史。"""
    state: CliState = ctx.obj
    with friendly_errors():
        settings = _settings(state)
        db = _db(state)
        with db.session() as session:
            rows = AssetService(session, settings).history(name)
            payload = [
                {"date": v.valued_on.isoformat(), "value": format_money(v.value_minor, v.currency), "source": v.source, "note": v.note}
                for v in rows
            ]
        _emit(state, payload, title="估值历史")


@asset_app.command("review")
def asset_review(ctx: typer.Context) -> None:
    """季度盘点：列出过期估值与近期还款。"""
    state: CliState = ctx.obj
    with friendly_errors():
        settings = _settings(state)
        payload = automation_tasks.quarterly_review(_db(state), settings)
        _emit(state, payload, title="季度盘点")


# ---------------------------------------------------------------------------
# liabilities
# ---------------------------------------------------------------------------
@liability_app.command("list")
def liability_list(ctx: typer.Context) -> None:
    """列出负债。"""
    state: CliState = ctx.obj
    with friendly_errors():
        settings = _settings(state)
        db = _db(state)
        with db.session() as session:
            summary = LiabilityService(session, settings).summary()
        _emit(state, summary, title="负债")


@liability_app.command("add")
def liability_add(
    ctx: typer.Context,
    name: str = typer.Argument(...),
    liability_type: str = typer.Option("mortgage", "--type", help="mortgage/auto_loan/consumer_loan/credit_card/other"),
    principal: str = typer.Option(..., "--principal", help="初始本金"),
    balance: Optional[str] = typer.Option(None, "--balance", help="当前余额"),
    rate: str = typer.Option("0", "--rate", help="年利率，如 4.9 或 0.049"),
    months: Optional[int] = typer.Option(None, "--months", help="剩余月数"),
    payment: Optional[str] = typer.Option(None, "--payment", help="月供"),
    next_date: Optional[str] = typer.Option(None, "--next-date", help="下次还款日 YYYY-MM-DD"),
) -> None:
    """新增负债。"""
    state: CliState = ctx.obj
    with friendly_errors():
        settings = _settings(state)
        db = _db(state)
        with db.session() as session:
            item = LiabilityService(session, settings).add(
                name=name,
                liability_type=liability_type,
                principal=principal,
                balance=balance,
                annual_rate=rate,
                remaining_months=months,
                monthly_payment=payment,
                next_payment_date=date.fromisoformat(next_date) if next_date else None,
            )
            _emit(state, {"id": item.id, "name": item.name, "balance": format_money(item.balance_minor), "monthly_payment": format_money(item.monthly_payment_minor)})


@liability_app.command("pay")
def liability_pay(
    ctx: typer.Context,
    name: str = typer.Argument(...),
    principal: str = typer.Option(..., "--principal"),
    interest: str = typer.Option("0", "--interest"),
    paid_on: Optional[str] = typer.Option(None, "--date"),
    note: Optional[str] = typer.Option(None, "--note"),
) -> None:
    """记录一笔还款（本金 + 利息）。"""
    state: CliState = ctx.obj
    with friendly_errors():
        settings = _settings(state)
        db = _db(state)
        with db.session() as session:
            payment = LiabilityService(session, settings).record_payment(
                name,
                paid_on=date.fromisoformat(paid_on) if paid_on else today_local(settings.timezone),
                principal=principal,
                interest=interest,
                note=note,
            )
            _emit(state, {"id": payment.id, "total": format_money(payment.total_minor), "principal": format_money(payment.principal_minor), "interest": format_money(payment.interest_minor)})


@liability_app.command("schedule")
def liability_schedule(
    ctx: typer.Context,
    name: str = typer.Argument(...),
    months: Optional[int] = typer.Option(None, "--months"),
) -> None:
    """输出还款计划（等额本息/等额本金）。"""
    state: CliState = ctx.obj
    with friendly_errors():
        settings = _settings(state)
        db = _db(state)
        with db.session() as session:
            rows = LiabilityService(session, settings).amortization(name, months=months)
            payload = [r.as_dict(settings.currency) for r in rows]
        _emit(state, payload, title="还款计划")


@liability_app.command("balance")
def liability_balance(
    ctx: typer.Context,
    name: str = typer.Argument(...),
    amount: str = typer.Option(..., "--amount"),
    reason: str = typer.Option(..., "--reason", help="调整原因（审计必填）"),
) -> None:
    """手工调整负债余额。"""
    state: CliState = ctx.obj
    with friendly_errors():
        settings = _settings(state)
        db = _db(state)
        with db.session() as session:
            item = LiabilityService(session, settings).update_balance(name, balance=amount, reason=reason)
            _emit(state, {"id": item.id, "balance": format_money(item.balance_minor)})


# ---------------------------------------------------------------------------
# budgets
# ---------------------------------------------------------------------------
@budget_app.command("set")
def budget_set(
    ctx: typer.Context,
    month: Optional[str] = typer.Option(None, "--month", help="YYYY-MM"),
    year: Optional[int] = typer.Option(None, "--year", help="年度预算"),
    item: list[str] = typer.Option(..., "--item", help="分类=金额，可重复，如 餐饮=1500"),
    name: Optional[str] = typer.Option(None, "--name"),
) -> None:
    """设置预算（自动生成新版本，旧版本保留）。"""
    state: CliState = ctx.obj
    with friendly_errors():
        settings = _settings(state)
        db = _db(state)
        items = [_parse_item(value) for value in item]
        with db.session() as session:
            budget = BudgetService(session, settings).set_budget(
                period=month, year=year, items=items, name=name
            )
            _emit(state, {"id": budget.id, "version": budget.version, "total": format_money(budget.total_amount_minor)})


@budget_app.command("show")
def budget_show(
    ctx: typer.Context,
    month: Optional[str] = typer.Option(None, "--month"),
    year: Optional[int] = typer.Option(None, "--year", help="查看年度预算执行"),
) -> None:
    """查看预算执行情况（月度或年度）。"""
    state: CliState = ctx.obj
    with friendly_errors():
        settings = _settings(state)
        db = _db(state)
        with db.session() as session:
            service = BudgetService(session, settings)
            if year is not None:
                result = service.execution_year(year)
                period = str(year)
            else:
                period = _period_or_current(month, settings)
                result = service.execution(period)
            payload = result.as_dict() if result else {"period": period, "message": "该期间未设置预算"}
        _emit(state, payload, title=f"{period} 预算")


@budget_app.command("history")
def budget_history(
    ctx: typer.Context,
    month: Optional[str] = typer.Option(None, "--month"),
    year: Optional[int] = typer.Option(None, "--year"),
) -> None:
    """查看预算版本历史。"""
    state: CliState = ctx.obj
    with friendly_errors():
        settings = _settings(state)
        db = _db(state)
        with db.session() as session:
            service = BudgetService(session, settings)
            if year is not None:
                rows = service.history_year(year)
                label = str(year)
            else:
                label = _period_or_current(month, settings)
                rows = service.history(label)
            payload = [
                {"id": b.id, "version": b.version, "status": b.status, "total": format_money(b.total_amount_minor)}
                for b in rows
            ]
        _emit(state, payload, title=f"{label} 预算历史")


# ---------------------------------------------------------------------------
# goals / planning
# ---------------------------------------------------------------------------
@goal_app.command("add")
def goal_add(
    ctx: typer.Context,
    name: str = typer.Argument(...),
    target: str = typer.Option(..., "--target"),
    goal_type: str = typer.Option("wealth", "--type", help="emergency/education/retirement/housing/major_purchase/wealth"),
    current: str = typer.Option("0", "--current"),
    target_date: Optional[str] = typer.Option(None, "--date", help="YYYY-MM-DD"),
    monthly: Optional[str] = typer.Option(None, "--monthly", help="每月计划储蓄"),
    risk: str = typer.Option("moderate", "--risk", help="conservative/moderate/aggressive"),
) -> None:
    """新增财务目标。"""
    state: CliState = ctx.obj
    with friendly_errors():
        settings = _settings(state)
        db = _db(state)
        with db.session() as session:
            goal = PlanningService(session, settings).add_goal(
                name=name,
                target=target,
                goal_type=goal_type,
                current=current,
                target_date=date.fromisoformat(target_date) if target_date else None,
                monthly_saving=monthly,
                risk_tolerance=risk,
            )
            _emit(state, {"id": goal.id, "name": goal.name, "target": format_money(goal.target_minor)})


@goal_app.command("list")
def goal_list(ctx: typer.Context) -> None:
    """列出目标与规划。"""
    state: CliState = ctx.obj
    with friendly_errors():
        settings = _settings(state)
        db = _db(state)
        with db.session() as session:
            payload = PlanningService(session, settings).plan()
        _emit(state, payload, title="财务规划")


@goal_app.command("progress")
def goal_progress(
    ctx: typer.Context,
    name: str = typer.Argument(...),
    current: str = typer.Option(..., "--current"),
) -> None:
    """更新目标当前金额。"""
    state: CliState = ctx.obj
    with friendly_errors():
        settings = _settings(state)
        db = _db(state)
        with db.session() as session:
            goal = PlanningService(session, settings).set_progress(name, current=current)
            _emit(state, {"id": goal.id, "status": goal.status, "progress": f"{goal.current_minor / goal.target_minor:.1%}"})


@app.command()
def plan(
    ctx: typer.Context,
    annual: bool = typer.Option(False, "--annual", help="输出年度规划（含下一年度）"),
    annual_return: Optional[str] = typer.Option(None, "--return", help="假设年化收益率，如 0.03"),
) -> None:
    """输出家庭财务规划（实际/假设分开标注）。"""
    state: CliState = ctx.obj
    with friendly_errors():
        settings = _settings(state)
        db = _db(state)
        with db.session() as session:
            payload = PlanningService(session, settings).plan(
                annual_return=Decimal(annual_return) if annual_return else None
            )
        _emit(state, payload, title="年度规划" if annual else "家庭财务规划")


# ---------------------------------------------------------------------------
# investments
# ---------------------------------------------------------------------------
@invest_app.command("product")
def invest_product(
    ctx: typer.Context,
    symbol: str = typer.Argument(...),
    name: str = typer.Option(..., "--name"),
    product_type: str = typer.Option("fund", "--type", help="stock/fund/bond/other"),
) -> None:
    """新增投资产品。"""
    state: CliState = ctx.obj
    with friendly_errors():
        settings = _settings(state)
        db = _db(state)
        with db.session() as session:
            product = InvestmentService(session, settings).add_product(
                symbol=symbol, name=name, product_type=product_type
            )
            _emit(state, {"id": product.id, "symbol": product.symbol, "name": product.name})


def _invest_trade(ctx, kind: str, symbol: str, quantity: str, price: str, fee: str, trade_date: str | None, amount: str | None) -> None:
    state: CliState = ctx.obj
    with friendly_errors():
        settings = _settings(state)
        db = _db(state)
        with db.session() as session:
            txn = InvestmentService(session, settings).record(
                product_ref=symbol,
                txn_type=kind,
                trade_date=date.fromisoformat(trade_date) if trade_date else today_local(settings.timezone),
                quantity=quantity,
                price=price,
                fee=fee,
                amount=amount,
            )
            _emit(state, {"id": txn.id, "type": txn.txn_type, "gross": format_money(txn.gross_minor), "fee": format_money(txn.fee_minor)})


@invest_app.command("buy")
def invest_buy(
    ctx: typer.Context,
    symbol: str = typer.Argument(...),
    quantity: str = typer.Option(..., "--quantity"),
    price: str = typer.Option(..., "--price"),
    fee: str = typer.Option("0", "--fee"),
    trade_date: Optional[str] = typer.Option(None, "--date"),
) -> None:
    """记录买入。"""
    _invest_trade(ctx, "buy", symbol, quantity, price, fee, trade_date, None)


@invest_app.command("sell")
def invest_sell(
    ctx: typer.Context,
    symbol: str = typer.Argument(...),
    quantity: str = typer.Option(..., "--quantity"),
    price: str = typer.Option(..., "--price"),
    fee: str = typer.Option("0", "--fee"),
    trade_date: Optional[str] = typer.Option(None, "--date"),
) -> None:
    """记录卖出。"""
    _invest_trade(ctx, "sell", symbol, quantity, price, fee, trade_date, None)


@invest_app.command("dividend")
def invest_dividend(
    ctx: typer.Context,
    symbol: str = typer.Argument(...),
    amount: str = typer.Option(..., "--amount"),
    trade_date: Optional[str] = typer.Option(None, "--date"),
) -> None:
    """记录分红（现金流，不计入持仓成本）。"""
    _invest_trade(ctx, "dividend", symbol, "0", "0", "0", trade_date, amount)


@invest_app.command("value")
def invest_value(
    ctx: typer.Context,
    symbol: str = typer.Argument(...),
    market_value: str = typer.Option(..., "--market-value"),
    valued_on: Optional[str] = typer.Option(None, "--date"),
    source: str = typer.Option("manual", "--source"),
) -> None:
    """更新持仓市值（无可靠行情时必须手工提供）。"""
    state: CliState = ctx.obj
    with friendly_errors():
        settings = _settings(state)
        db = _db(state)
        with db.session() as session:
            position = InvestmentService(session, settings).revalue(
                symbol,
                market_value=market_value,
                valued_on=date.fromisoformat(valued_on) if valued_on else None,
                source=source,
            )
            _emit(state, {"id": position.id, "market_value": format_money(position.market_value_minor or 0), "unrealized": format_money(position.unrealized_pnl_minor or 0)})


@invest_app.command("portfolio")
def invest_portfolio(ctx: typer.Context) -> None:
    """查看投资组合与收益。"""
    state: CliState = ctx.obj
    with friendly_errors():
        settings = _settings(state)
        db = _db(state)
        with db.session() as session:
            payload = InvestmentService(session, settings).portfolio().as_dict()
        _emit(state, payload, title="投资组合")


# ---------------------------------------------------------------------------
# feishu
# ---------------------------------------------------------------------------
def _feishu_service(state: CliState, settings):
    db = _db(state)
    return FeishuSyncService(db, settings), db


@feishu_app.command("status")
def feishu_status(ctx: typer.Context) -> None:
    """检查飞书配置与连接状态（不会假装成功）。"""
    state: CliState = ctx.obj
    with friendly_errors():
        settings = _settings(state)
        client = FeishuClient(settings)
        payload = client.status()
        if settings.feishu_configured:
            try:
                payload.update(client.healthcheck())
            except FamilyCFOError as exc:
                payload["connected"] = False
                payload["error"] = exc.message
        else:
            payload["error"] = "未配置 FAMILY_CFO_FEISHU_APP_ID / APP_SECRET / APP_TOKEN"
        client.close()
        _emit(state, payload, title="飞书状态")


@feishu_app.command("ensure-tables")
def feishu_ensure_tables(ctx: typer.Context) -> None:
    """在目标多维表格中创建缺失的数据表与字段。"""
    state: CliState = ctx.obj
    with friendly_errors():
        settings = _settings(state)
        service, db = _feishu_service(state, settings)
        try:
            mapping = service.ensure_tables()
        finally:
            service.close()
            db.close()
        _emit(state, mapping, title="已确保数据表")


@feishu_app.command("sync")
def feishu_sync(
    ctx: typer.Context,
    entity: Optional[str] = typer.Option(None, "--entity", help="transactions/assets/liabilities/budgets/goals/monthly_reports"),
    all_entities: bool = typer.Option(False, "--all", help="推送全部实体"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """把本地数据推送到飞书（失败不影响本地账本）。"""
    state: CliState = ctx.obj
    with friendly_errors():
        settings = _settings(state)
        if not settings.feishu_configured:
            raise ValidationError(
                "飞书未配置：请设置 FAMILY_CFO_FEISHU_APP_ID / APP_SECRET / APP_TOKEN"
            )
        service, db = _feishu_service(state, settings)
        entities = list(service.specs) if all_entities else [entity]
        if not entities or entities == [None]:
            raise ValidationError("请指定 --entity 或 --all")
        reports = []
        try:
            for name in entities:
                reports.append(service.push(name, dry_run=dry_run).as_dict())
        finally:
            service.close()
            db.close()
        _emit(state, reports, title="同步结果")


@feishu_app.command("pull")
def feishu_pull(
    ctx: typer.Context,
    entity: str = typer.Option("goals", "--entity", help="goals 或 budgets"),
) -> None:
    """从飞书回写用户可编辑的目标/预算（校验后写入）。"""
    state: CliState = ctx.obj
    with friendly_errors():
        settings = _settings(state)
        if not settings.feishu_configured:
            raise ValidationError("飞书未配置")
        service, db = _feishu_service(state, settings)
        try:
            report = service.pull(entity).as_dict()
        finally:
            service.close()
            db.close()
        _emit(state, report, title=f"回写 {entity}")


@feishu_app.command("conflicts")
def feishu_conflicts(ctx: typer.Context) -> None:
    """列出同步冲突。"""
    state: CliState = ctx.obj
    with friendly_errors():
        settings = _settings(state)
        service, db = _feishu_service(state, settings)
        try:
            payload = service.conflicts()
        finally:
            service.close()
            db.close()
        _emit(state, payload, title="同步冲突")


@feishu_app.command("resolve")
def feishu_resolve(
    ctx: typer.Context,
    entity: str = typer.Argument(...),
    entity_id: str = typer.Argument(...),
    strategy: str = typer.Option("local", "--strategy", help="local 或 remote"),
) -> None:
    """解决单条同步冲突。"""
    state: CliState = ctx.obj
    with friendly_errors():
        settings = _settings(state)
        service, db = _feishu_service(state, settings)
        try:
            payload = service.resolve(entity, entity_id, strategy=strategy).as_dict()
        finally:
            service.close()
            db.close()
        _emit(state, payload, title="冲突处理结果")


# ---------------------------------------------------------------------------
# backup
# ---------------------------------------------------------------------------
@backup_app.command("create")
def backup_create(ctx: typer.Context, label: Optional[str] = typer.Option(None, "--label")) -> None:
    """创建经校验的数据库备份。"""
    state: CliState = ctx.obj
    with friendly_errors():
        settings = _settings(state)
        info = backup_database(settings.db_path, settings.backups_dir, label=label)
        _emit(state, {"path": str(info.path), "sha256": info.sha256, "size": info.size, "tables": info.table_counts})


@backup_app.command("list")
def backup_list(ctx: typer.Context) -> None:
    """列出备份。"""
    state: CliState = ctx.obj
    with friendly_errors():
        settings = _settings(state)
        rows = []
        if settings.backups_dir.exists():
            for path in sorted(settings.backups_dir.glob("family_cfo-*.db"), reverse=True):
                rows.append({"file": path.name, "size": path.stat().st_size, "modified": date.fromtimestamp(path.stat().st_mtime).isoformat()})
        _emit(state, rows, title="备份")


@backup_app.command("verify")
def backup_verify(ctx: typer.Context, path: Path = typer.Argument(...)) -> None:
    """校验备份完整性与外键。"""
    state: CliState = ctx.obj
    with friendly_errors():
        info = verify_backup(path)
        _emit(state, {"path": str(info.path), "integrity": info.integrity, "tables": info.table_counts, "sha256": info.sha256})


@backup_app.command("restore")
def backup_restore(
    ctx: typer.Context,
    path: Path = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes", help="跳过确认"),
) -> None:
    """从备份恢复（默认先做安全备份）。"""
    state: CliState = ctx.obj
    with friendly_errors():
        settings = _settings(state)
        if not yes:
            typer.confirm(f"将用 {path.name} 覆盖当前数据库，继续？", abort=True)
        result = restore_database(path, settings.db_path, backup_dir=settings.backups_dir)
        _emit(state, result, title="恢复完成")


@backup_app.command("prune")
def backup_prune(
    ctx: typer.Context,
    keep: int = typer.Option(20, "--keep"),
) -> None:
    """保留最近 N 份备份。"""
    state: CliState = ctx.obj
    with friendly_errors():
        settings = _settings(state)
        removed = prune_backups(settings.backups_dir, keep=keep)
        _emit(state, {"removed": [str(p) for p in removed]})


# ---------------------------------------------------------------------------
# automation
# ---------------------------------------------------------------------------
@automation_app.command("list")
def automation_list(ctx: typer.Context) -> None:
    """列出可配置的定时任务（默认全部关闭）。"""
    state: CliState = ctx.obj
    _emit(state, automation_schedule.jobs_as_dicts(), title="自动化任务")


@automation_app.command("install")
def automation_install(
    ctx: typer.Context,
    yes: bool = typer.Option(False, "--yes", help="确认启用（必须显式指定）"),
    dest: Optional[Path] = typer.Option(None, "--dest", help="输出目录，默认 data/automation"),
) -> None:
    """生成 Hermes blueprint 与 crontab 文件（需用户确认）。"""
    state: CliState = ctx.obj
    with friendly_errors():
        settings = _settings(state)
        target = dest or (settings.data_dir / "automation")
        payload = automation_schedule.install_schedule(target, confirmed=yes)
        _emit(state, payload, title="定时任务配置已生成（请人工核对后安装）")


@automation_app.command("run")
def automation_run(
    ctx: typer.Context,
    job: str = typer.Argument(..., help="job id，见 `family-cfo automation list`"),
    month: Optional[str] = typer.Option(None, "--month"),
) -> None:
    """立即执行一次自动化任务。"""
    state: CliState = ctx.obj
    with friendly_errors():
        settings = _settings(state)
        payload = automation_tasks.run_job(job, _db(state), settings, period=month)
        _emit(state, payload, title=f"任务 {job}")


# ---------------------------------------------------------------------------
# db
# ---------------------------------------------------------------------------
@db_app.command("check")
def db_check(ctx: typer.Context) -> None:
    """运行数据完整性检查。"""
    state: CliState = ctx.obj
    with friendly_errors():
        db = _db(state)
        with db.session() as session:
            report = run_integrity_checks(session, _household_id(session))
            payload = report.as_dict()
        _emit(state, payload, title="数据完整性")
        raise typer.Exit(0 if report.ok else 1)


@db_app.command("config")
def db_config(ctx: typer.Context) -> None:
    """显示脱敏后的配置。"""
    state: CliState = ctx.obj
    with friendly_errors():
        settings = _settings(state)
        _emit(state, redact_config(settings), title="配置")


@db_app.command("mapping")
def db_mapping(ctx: typer.Context) -> None:
    """列出已保存的账单字段映射。"""
    state: CliState = ctx.obj
    with friendly_errors():
        settings = _settings(state)
        _emit(state, load_profiles(settings.project_root), title="字段映射")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _find_category(session, ref: str) -> TransactionCategory:
    from sqlalchemy import or_

    category = session.get(TransactionCategory, ref)
    if category is None:
        category = session.scalar(
            select(TransactionCategory).where(
                or_(
                    TransactionCategory.slug == ref,
                    TransactionCategory.name == ref,
                )
            )
        )
    if category is None:
        raise ValidationError(f"分类不存在: {ref}")
    return category


def _find_member(session, ref: str) -> FamilyMember:
    member = session.get(FamilyMember, ref)
    if member is None:
        member = session.scalar(select(FamilyMember).where(FamilyMember.name == ref))
    if member is None:
        raise ValidationError(f"家庭成员不存在: {ref}")
    return member


def _parse_item(value: str) -> dict:
    if "=" not in value:
        raise ValidationError(f"--item 需要 分类=金额 格式: {value}")
    name, amount = value.split("=", 1)
    return {"category": name.strip(), "amount": amount.strip()}


def main() -> None:
    """Console entry point.

    ``--json`` is a global flag, but accepting it after the subcommand is far
    more natural; normalise it before Typer parses the argv.
    """
    argv = sys.argv[1:]
    if "--json" in argv:
        argv = ["--json", *[item for item in argv if item != "--json"]]
    app(args=argv)


if __name__ == "__main__":
    main()
