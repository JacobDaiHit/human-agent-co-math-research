from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(
            "sqlite:///" + self.path.as_posix(),
            connect_args={"check_same_thread": False, "timeout": 15},
        )

        @event.listens_for(self.engine, "connect")
        def configure(connection, _):
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=FULL")
            cursor.execute("PRAGMA busy_timeout=15000")
            cursor.close()

        self.sessions = sessionmaker(self.engine, expire_on_commit=False)

    def migrate(self):
        from alembic import command
        from alembic.config import Config

        cfg = Config()
        cfg.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
        with self.engine.begin() as connection:
            cfg.attributes["connection"] = connection
            command.upgrade(cfg, "head")

    def close(self):
        self.engine.dispose()
