"""The committed migration must create the schema the ORM actually uses."""

from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from mathagent.persistence.database import Database
from mathagent.persistence.models import Base
from sqlalchemy import inspect, text


def test_upgraded_sqlite_matches_orm_metadata_and_repeated_upgrade_is_safe(tmp_path: Path):
    database = Database(tmp_path / "migration.sqlite3")
    try:
        database.migrate()
        with database.engine.connect() as connection:
            context = MigrationContext.configure(connection, opts={"compare_type": True})
            assert compare_metadata(context, Base.metadata) == []
            version = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            assert version == "0008_bounded_search"
            tables = set(inspect(connection).get_table_names())
            assert tables == set(Base.metadata.tables) | {"alembic_version"}
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
            assert connection.exec_driver_sql("PRAGMA journal_mode").scalar_one() == "wal"
        database.migrate()
        with database.engine.connect() as connection:
            assert set(inspect(connection).get_table_names()) == tables
            assert (
                connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                == version
            )
    finally:
        database.close()


def test_legacy_runtime_schema_upgrades_with_output_reservations(tmp_path: Path):
    database = Database(tmp_path / "legacy-runtime.sqlite3")
    try:
        config = Config()
        config.set_main_option(
            "script_location", str(Path(__file__).parents[2] / "services/api/src/mathagent/persistence/migrations")
        )
        with database.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "0006_call_retention")
        with database.engine.connect() as connection:
            assert "output_token_reservation" not in {
                column["name"] for column in inspect(connection).get_columns("provider_requests")
            }
        database.migrate()
        with database.engine.connect() as connection:
            assert "output_token_reservation" in {
                column["name"] for column in inspect(connection).get_columns("provider_requests")
            }
            assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0008_bounded_search"
    finally:
        database.close()
