"""CLI smoke tests, Hermes Skill packaging and redaction."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from family_cfo.automation import schedule
from family_cfo.cli import app
from family_cfo.core.config import load_config, redact_config
from family_cfo.core.logging import redact

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = REPO_ROOT / "skills" / "family-cfo"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture()
def cli(tmp_path: Path):
    runner = CliRunner()

    def invoke(*args: str):
        base = ["--root", str(tmp_path), "--db", str(tmp_path / "data" / "family_cfo.db")]
        return runner.invoke(app, [*base, *args])

    return invoke


def test_cli_init_import_analyze(cli):
    init = cli("init")
    assert init.exit_code == 0, init.output
    assert "初始化完成" in init.output

    imported = cli("import", str(FIXTURES / "wechat_sample.csv"))
    assert imported.exit_code == 0, imported.output
    assert "imported" in imported.output

    analyzed = cli("--json", "analyze", "--month", "2026-10")
    assert analyzed.exit_code == 0, analyzed.output
    payload = json.loads(analyzed.output)
    assert payload["summary"]["income_minor"] == 0
    assert payload["summary"]["expense_minor"] == 37830


def test_cli_settle_status_and_blocked_close(cli):
    cli("init")
    cli("import", str(FIXTURES / "wechat_sample.csv"))
    status = cli("--json", "settle", "--month", "2026-10", "--status")
    assert status.exit_code == 0, status.output
    payload = json.loads(status.output)
    assert payload["status"] == "REVIEW_REQUIRED"
    assert payload["ready"] is False

    blocked = cli("settle", "--month", "2026-10", "--close")
    assert blocked.exit_code != 0
    assert "尚不满足结算条件" in blocked.output


def test_cli_backup_create(cli):
    cli("init")
    result = cli("--json", "backup", "create")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert Path(payload["path"]).exists()
    assert payload["sha256"]


def test_cli_doctor(cli):
    cli("init")
    result = cli("--json", "doctor")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["database_exists"] is True
    assert payload["integrity_ok"] is True
    assert payload["feishu_configured"] is False


def test_automation_install_requires_consent(tmp_path: Path):
    with pytest.raises(Exception):
        schedule.install_schedule(tmp_path / "auto", confirmed=False)
    result = schedule.install_schedule(tmp_path / "auto", confirmed=True)
    assert Path(result["blueprint"]).exists()
    assert Path(result["crontab"]).exists()
    payload = yaml.safe_load(Path(result["blueprint"]).read_text(encoding="utf-8"))
    assert len(payload["jobs"]) == 7
    assert all(job["enabled"] is False for job in payload["jobs"])


def test_automation_jobs_cover_required_cadences():
    ids = {job.id for job in schedule.JOBS}
    assert {
        "weekly_scan",
        "weekly_summary",
        "monthly_collect",
        "monthly_close_reminder",
        "monthly_report",
        "quarterly_review",
        "annual_planning",
    } == ids


def test_redaction_masks_secrets_and_accounts():
    assert "very-secret" not in redact("app_secret=very-secret")
    assert "t-abc123456789" not in redact("Authorization: Bearer t-abc123456789")
    masked = redact("card 6222021234567890123")
    assert "6222021234567890123" not in masked
    assert "****" in masked
    assert "13800138000" not in redact("phone 13800138000")


def test_config_output_is_redacted(tmp_path: Path):
    settings = load_config(
        root=REPO_ROOT,
        env={
            "FAMILY_CFO_FEISHU_APP_ID": "cli_abc",
            "FAMILY_CFO_FEISHU_APP_SECRET": "topsecret",
            "FAMILY_CFO_FEISHU_APP_TOKEN": "apptoken",
        },
        overrides={"data_dir": tmp_path, "db_path": tmp_path / "x.db"},
    )
    payload = redact_config(settings)
    assert "topsecret" not in json.dumps(payload)
    assert payload["feishu"]["app_secret"] == "***"


def test_skill_frontmatter_and_relative_paths():
    text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---")
    frontmatter = yaml.safe_load(text.split("---", 2)[1])
    assert frontmatter["name"] == "family-cfo"
    assert len(frontmatter["description"]) > 50
    assert frontmatter["version"]
    assert frontmatter["metadata"]["hermes"]["category"] == "finance"
    assert "/Users/" not in text
    assert "scripts/fcfo.py" in text


def test_skill_support_files_present():
    assert (SKILL_DIR / "scripts" / "fcfo.py").exists()
    for name in ("workflows.md", "data-model.md", "feishu-setup.md", "metrics.md", "security.md"):
        path = SKILL_DIR / "references" / name
        assert path.exists() and path.stat().st_size > 100
    for name in ("budget.example.yaml", "mapping.example.yaml", "monthly-close-checklist.md"):
        assert (SKILL_DIR / "templates" / name).exists()


def test_skill_script_runs_from_copied_directory(tmp_path: Path):
    target = tmp_path / "installed-skill"
    shutil.copytree(SKILL_DIR, target)
    env = dict(os.environ)
    env["FAMILY_CFO_HOME"] = str(REPO_ROOT)
    env.pop("FAMILY_CFO_DB_PATH", None)
    result = subprocess.run(
        [sys.executable, str(target / "scripts" / "fcfo.py"), "--help"],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "Family CFO" in result.stdout


def test_cli_help_lists_required_commands():
    runner = CliRunner()
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in (
        "init",
        "import",
        "analyze",
        "reconcile",
        "report",
        "settle",
        "feishu",
        "backup",
        "automation",
    ):
        assert command in result.stdout


def test_every_cli_command_exposes_help(tmp_path):
    runner = CliRunner()
    base = ["--root", str(tmp_path), "--db", str(tmp_path / "data" / "family_cfo.db")]
    commands = [
        "init", "doctor", "demo", "import", "scan", "reconcile", "settle",
        "analyze", "health", "ask", "report", "record", "plan",
    ]
    groups = {
        "tx": ["list", "fix", "retype", "delete", "restore", "resolve"],
        "category": ["add", "list"],
        "account": ["list", "add", "balance"],
        "asset": ["list", "add", "value", "history", "review"],
        "liability": ["list", "add", "pay", "schedule", "balance"],
        "budget": ["set", "show", "history"],
        "goal": ["add", "list", "progress"],
        "invest": ["product", "buy", "sell", "dividend", "value", "portfolio"],
        "feishu": ["status", "ensure-tables", "sync", "pull", "conflicts", "resolve"],
        "backup": ["create", "list", "verify", "restore", "prune"],
        "automation": ["list", "install", "run"],
        "db": ["check", "config", "mapping"],
    }
    for command in commands:
        result = runner.invoke(app, [*base, command, "--help"])
        assert result.exit_code == 0, (command, result.output)
    for group, subcommands in groups.items():
        result = runner.invoke(app, [*base, group, "--help"])
        assert result.exit_code == 0, (group, result.output)
        for sub in subcommands:
            result = runner.invoke(app, [*base, group, sub, "--help"])
            assert result.exit_code == 0, (group, sub, result.output)


def test_cli_config_never_prints_secrets(tmp_path):
    runner = CliRunner()
    base = ["--root", str(tmp_path), "--db", str(tmp_path / "data" / "family_cfo.db")]
    env = {
        "FAMILY_CFO_FEISHU_APP_ID": "cli_secret",
        "FAMILY_CFO_FEISHU_APP_SECRET": "super-secret-value",
        "FAMILY_CFO_FEISHU_APP_TOKEN": "app-token-value",
    }
    result = runner.invoke(app, [*base, "--json", "db", "config"], env=env)
    assert result.exit_code == 0, result.output
    assert "super-secret-value" not in result.output
    assert "app-token-value" not in result.output
    payload = json.loads(result.output)
    assert payload["feishu"]["app_secret"] == "***"


def test_no_absolute_paths_in_skill_support_files():
    for path in SKILL_DIR.rglob("*"):
        if path.is_file() and path.suffix in {".md", ".py", ".yaml", ".yml"}:
            text = path.read_text(encoding="utf-8")
            assert "/Users/" not in text, path
            assert "/home/" not in text, path


def test_cli_backup_restore_roundtrip(cli, tmp_path):
    cli("init")
    cli("import", str(FIXTURES / "wechat_sample.csv"))
    created = cli("--json", "backup", "create")
    assert created.exit_code == 0, created.output
    backup_path = json.loads(created.output)["path"]
    cli("import", str(FIXTURES / "alipay_sample.csv"))
    before = json.loads(cli("--json", "tx", "list").output)
    assert len(before) == 21
    restored = cli("backup", "restore", backup_path, "--yes")
    assert restored.exit_code == 0, restored.output
    after = json.loads(cli("--json", "tx", "list").output)
    assert len(after) == 11
    verified = cli("--json", "backup", "verify", backup_path)
    assert verified.exit_code == 0
    assert json.loads(verified.output)["integrity"] == "ok"


def test_cli_report_month_and_year(cli, tmp_path):
    cli("init")
    cli("import", str(FIXTURES / "wechat_sample.csv"))
    monthly = cli("--json", "report", "--month", "2026-10", "--format", "md,xlsx")
    assert monthly.exit_code == 0, monthly.output
    monthly_payload = json.loads(monthly.output)
    assert Path(monthly_payload["markdown"]).exists()
    assert Path(monthly_payload["excel"]).exists()
    annual = cli("--json", "report", "--year", "2026", "--format", "md,xlsx")
    assert annual.exit_code == 0, annual.output
    annual_payload = json.loads(annual.output)
    assert Path(annual_payload["markdown"]).exists()
    assert Path(annual_payload["excel"]).exists()


def test_cli_end_to_end_mutations(cli):
    assert cli("init").exit_code == 0
    assert cli("category", "add", "宠物", "--keyword", "猫粮").exit_code == 0
    assert cli("account", "balance", "银行卡", "--amount", "50000").exit_code == 0
    assert cli("asset", "add", "自住房", "--value", "2000000", "--residence").exit_code == 0
    assert cli("asset", "value", "自住房", "--value", "2100000").exit_code == 0
    assert cli("asset", "history", "自住房").exit_code == 0
    assert cli("liability", "add", "房贷", "--principal", "1000000", "--balance", "800000",
               "--rate", "4.9", "--months", "240", "--payment", "6500").exit_code == 0
    assert cli("liability", "pay", "房贷", "--principal", "2000", "--interest", "3266").exit_code == 0
    assert cli("liability", "schedule", "房贷", "--months", "3").exit_code == 0
    assert cli("budget", "set", "--month", "2026-10", "--item", "餐饮=1500").exit_code == 0
    assert cli("budget", "show", "--month", "2026-10").exit_code == 0
    assert cli("budget", "history", "--month", "2026-10").exit_code == 0
    assert cli("budget", "set", "--year", "2026", "--item", "餐饮=18000").exit_code == 0
    assert cli("budget", "show", "--year", "2026").exit_code == 0
    assert cli("goal", "add", "教育金", "--target", "200000", "--date", "2030-09-01").exit_code == 0
    assert cli("goal", "progress", "教育金", "--current", "50000").exit_code == 0
    assert cli("invest", "product", "000001", "--name", "测试基金").exit_code == 0
    assert cli("invest", "buy", "000001", "--quantity", "100", "--price", "1.5").exit_code == 0
    assert cli("invest", "sell", "000001", "--quantity", "10", "--price", "1.8").exit_code == 0
    assert cli("invest", "dividend", "000001", "--amount", "5").exit_code == 0
    assert cli("invest", "value", "000001", "--market-value", "180").exit_code == 0
    assert cli("invest", "portfolio").exit_code == 0
    assert cli("plan").exit_code == 0
    assert cli("health", "--month", "2026-10").exit_code == 0
    assert cli("ask", "2026-10 支出多少").exit_code == 0
    assert cli("scan").exit_code == 0
    assert cli("db", "check").exit_code == 0
    assert cli("db", "mapping").exit_code == 0
    assert cli("feishu", "status").exit_code == 0
    created = cli("--json", "backup", "create")
    assert created.exit_code == 0, created.output
    backup_path = json.loads(created.output)["path"]
    assert cli("backup", "list").exit_code == 0
    assert cli("backup", "verify", backup_path).exit_code == 0
    assert cli("backup", "prune", "--keep", "5").exit_code == 0
    assert cli("automation", "list").exit_code == 0
    assert cli("automation", "run", "weekly_summary").exit_code == 0
