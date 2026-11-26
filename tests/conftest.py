"""Shared pytest fixtures for Family CFO."""

from __future__ import annotations

from pathlib import Path

import pytest

from family_cfo.core.config import Settings, load_config
from family_cfo.database.base import Database
from family_cfo.database.init import initialize
from family_cfo.importers.service import ImportService
from family_cfo.integrations.feishu.client import FeishuClient
from family_cfo.integrations.feishu.mock import MockFeishuServer
from family_cfo.integrations.feishu.sync import FeishuSyncService

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return load_config(
        root=REPO_ROOT,
        env={},
        overrides={"data_dir": tmp_path / "data", "db_path": tmp_path / "data" / "family_cfo.db"},
    )


@pytest.fixture()
def db(settings: Settings) -> Database:
    settings.ensure_dirs()
    database = Database.open(settings.db_path)
    initialize(database, settings)
    yield database
    database.close()


@pytest.fixture()
def session(db: Database):
    with db.session() as db_session:
        yield db_session


@pytest.fixture()
def importer(db: Database, settings: Settings) -> ImportService:
    return ImportService(db, settings)


@pytest.fixture()
def import_fixture(db: Database, settings: Settings):
    def _import(name: str, **kwargs):
        return ImportService(db, settings).import_file(FIXTURES / name, **kwargs)

    return _import


@pytest.fixture()
def feishu_settings(tmp_path: Path, settings: Settings) -> Settings:
    return settings.with_overrides(
        feishu_app_id="cli_mock",
        feishu_app_secret="secret",
        feishu_app_token="appmock",
    )


@pytest.fixture()
def mock_feishu() -> MockFeishuServer:
    return MockFeishuServer()


@pytest.fixture()
def feishu_client(feishu_settings: Settings, mock_feishu: MockFeishuServer) -> FeishuClient:
    client = FeishuClient(feishu_settings, transport=mock_feishu.transport(), sleeper=lambda s: None)
    yield client
    client.close()


@pytest.fixture()
def sync_service(db, feishu_settings, feishu_client) -> FeishuSyncService:
    service = FeishuSyncService(db, feishu_settings, client=feishu_client)
    service.ensure_tables()
    return service
