"""Idempotent, additive SQLite migration for databases created before web onboarding.

``Base.metadata.create_all`` creates missing tables but never alters existing ones, so
columns added to ``owners``/``sites`` later are applied here.
"""

from sqlalchemy import Connection, Engine, MetaData, text
from sqlalchemy.schema import CreateIndex, CreateTable

_OWNER_COLUMNS = {
    "email": "VARCHAR",
    "phone": "VARCHAR",
    "signup_source": "VARCHAR NOT NULL DEFAULT 'bot'",
    "link_token": "VARCHAR",
    "subscription_status": "VARCHAR NOT NULL DEFAULT 'active'",
}
_SITE_COLUMNS = {
    "monitored": "BOOLEAN NOT NULL DEFAULT 1",
    "call_plan": "VARCHAR NOT NULL DEFAULT 'standard'",
}
_LINK_INDEX = "ix_owners_link_token"
_TMP_OWNERS = "owners__new"


def _columns(conn: Connection, table: str) -> dict[str, bool]:
    """Column name -> NOT NULL flag."""
    rows = conn.exec_driver_sql(f"PRAGMA table_info({table})").all()
    return {r[1]: bool(r[3]) for r in rows}


def _index_exists(conn: Connection, name: str) -> bool:
    return (
        conn.execute(
            text("SELECT 1 FROM sqlite_master WHERE type='index' AND name=:n"), {"n": name}
        ).first()
        is not None
    )


def _rebuild_owners(conn: Connection, run) -> None:
    """SQLite can't drop NOT NULL, so recreate owners from the current model and copy rows."""
    from solya.models import Owner

    old_cols = list(_columns(conn, "owners"))
    run(f"DROP TABLE IF EXISTS {_TMP_OWNERS}")
    run(f"DROP INDEX IF EXISTS {_LINK_INDEX}")
    tmp = Owner.__table__.to_metadata(MetaData(), name=_TMP_OWNERS)
    run(str(CreateTable(tmp).compile(conn)).strip())
    cols = ", ".join(old_cols)
    run(f"INSERT INTO {_TMP_OWNERS} ({cols}) SELECT {cols} FROM owners")
    run("DROP TABLE owners")
    run(f"ALTER TABLE {_TMP_OWNERS} RENAME TO owners")
    for idx in tmp.indexes:
        run(str(CreateIndex(idx).compile(conn)).strip().replace(_TMP_OWNERS, "owners"))


def apply(engine: Engine) -> list[str]:
    """Bring an existing SQLite database up to the current schema; returns statements run."""
    if engine.dialect.name != "sqlite":
        return []
    ran: list[str] = []
    with engine.begin() as conn:
        # pysqlite only opens a transaction before DML; begin explicitly so DDL is atomic.
        conn.exec_driver_sql("BEGIN")

        def run(stmt: str) -> None:
            conn.exec_driver_sql(stmt)
            ran.append(stmt)

        for table, wanted in (("owners", _OWNER_COLUMNS), ("sites", _SITE_COLUMNS)):
            have = _columns(conn, table)
            if not have:  # table absent: create_all owns it
                continue
            for name, ddl in wanted.items():
                if name not in have:
                    run(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
        owners = _columns(conn, "owners")
        if owners.get("telegram_chat_id"):
            _rebuild_owners(conn, run)
        if owners and not _index_exists(conn, _LINK_INDEX):
            run(f"CREATE UNIQUE INDEX IF NOT EXISTS {_LINK_INDEX} ON owners (link_token)")
    return ran
