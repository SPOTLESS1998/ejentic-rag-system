"""Audit log + token accounting for the RAG pipeline.

PER-TENANT BY PATH, TENANT-STAMPED BY ROW
-----------------------------------------
The DB used to be a single hardcoded `./ejentic_audit.db` with no tenant column,
so two clients deployed on one host shared one audit trail and GET /metrics
reported their COMBINED totals — a client's query text visible in another
client's dashboard. Now:

  * the file lives at `data/<client>/audit.db` (per-tenant path), and
  * every row carries the `client` it belongs to, and every read filters on it.

Both, deliberately: the path keeps tenants apart on disk, and the column means a
shared or migrated file can still never mix their rows. See MULTITENANCY.md.
"""
import datetime
import os
from pathlib import Path

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy import Column, Integer, String, Text, DateTime, select, func

import client_registry as registry

# The tenant this process serves. Same resolution order as main.py/ingest so the
# server, ingestion and the eval harness all write to one place.
ACTIVE_CLIENT = os.environ.get("RAG_CLIENT", "").strip() or registry.active_client_id()

_HERE = Path(__file__).resolve().parent
# An explicit override wins (tests point this at a temp dir); otherwise
# data/<client>/audit.db beneath the backend directory.
DB_PATH = Path(os.environ.get("RAG_AUDIT_DB") or
               (_HERE / "data" / ACTIVE_CLIENT / "audit.db"))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

DATABASE_URL = f"sqlite+aiosqlite:///{DB_PATH}"

engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

Base = declarative_base()

class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, index=True)
    timestamp = Column(DateTime, default=datetime.datetime.utcnow)
    # Which tenant this row belongs to. Indexed because every read filters on it.
    client = Column(String(64), index=True)
    clearance_level = Column(String(50), index=True)
    query_text = Column(Text)
    response_snippet = Column(Text)

    # --- Token accounting (a measurable architecture) -----------------------
    # Nullable so historical rows and error rows stay valid. `gated` = 1 means
    # the confidence gate short-circuited synthesis (≈0 answer tokens spent);
    # `estimated_saved_tokens` is the input we AVOIDED by not calling the LLM.
    prompt_tokens = Column(Integer, nullable=True)
    completion_tokens = Column(Integer, nullable=True)
    total_tokens = Column(Integer, nullable=True)
    estimated_saved_tokens = Column(Integer, nullable=True)
    token_source = Column(String(20), nullable=True)   # provider | estimate | mixed | gate
    gated = Column(Integer, default=0)


# The token columns were added after the first version of this table shipped.
# create_all() won't ALTER an existing table, so on a pre-existing DB we add any
# missing columns by hand. Additive only — never drops or rewrites data.
_TOKEN_COLUMNS = {
    "prompt_tokens": "INTEGER",
    "completion_tokens": "INTEGER",
    "total_tokens": "INTEGER",
    "estimated_saved_tokens": "INTEGER",
    "token_source": "VARCHAR(20)",
    "gated": "INTEGER DEFAULT 0",
    "client": "VARCHAR(64)",
}


def _migrate(sync_conn):
    existing = {
        row[1]
        for row in sync_conn.exec_driver_sql("PRAGMA table_info(audit_logs)").fetchall()
    }
    for col, ddl in _TOKEN_COLUMNS.items():
        if col not in existing:
            sync_conn.exec_driver_sql(f"ALTER TABLE audit_logs ADD COLUMN {col} {ddl}")
    # Pre-existing rows predate the tenant column. This DB file belongs to one
    # tenant (it lives under data/<client>/), so stamping NULLs with the active
    # client is correct, and it keeps historical metrics visible instead of
    # orphaning them behind the new filter.
    if "client" not in existing:
        sync_conn.exec_driver_sql(
            "UPDATE audit_logs SET client = ? WHERE client IS NULL", (ACTIVE_CLIENT,)
        )
    sync_conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_audit_logs_client ON audit_logs (client)"
    )


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_migrate)


async def log_query(
    clearance: str,
    query: str,
    response: str,
    *,
    prompt_tokens: int = None,
    completion_tokens: int = None,
    total_tokens: int = None,
    estimated_saved_tokens: int = None,
    token_source: str = None,
    gated: bool = False,
    client: str = None,
):
    # Audit logging is a side-effect, never the point of the request. It runs as
    # a fire-and-forget task, so if it ever fails (e.g. a schema drift on an old
    # DB) we swallow the error with a one-line warning instead of letting an
    # unretrieved task exception spam the logs mid-demo. The query already
    # succeeded by the time we get here.
    try:
        async with AsyncSessionLocal() as session:
            log_entry = AuditLog(
                client=client or ACTIVE_CLIENT,
                clearance_level=clearance,
                query_text=query,
                response_snippet=response[:1000],  # store up to 1000 chars of response
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                estimated_saved_tokens=estimated_saved_tokens,
                token_source=token_source,
                gated=1 if gated else 0,
            )
            session.add(log_entry)
            await session.commit()
    except Exception as exc:  # noqa: BLE001 - audit logging must never break a query
        print(f"[audit] WARNING: failed to persist audit log: {exc}")


async def get_token_metrics(limit: int = 20, client: str = None):
    """Aggregate token accounting + the most recent per-query rows for ONE tenant.

    Every aggregate and the recent-rows list are scoped to `client` (defaulting to
    this process's active tenant). Without that filter, a host running two clients
    would show each of them the other's query text and combined token spend.
    """
    cid = client or ACTIVE_CLIENT
    mine = AuditLog.client == cid

    async with AsyncSessionLocal() as session:
        total_queries = await session.scalar(
            select(func.count(AuditLog.id)).where(mine)
        ) or 0
        gated = await session.scalar(
            select(func.count(AuditLog.id)).where(mine, AuditLog.gated == 1)
        ) or 0
        answered = await session.scalar(
            select(func.count(AuditLog.id)).where(mine, AuditLog.gated == 0)
        ) or 0
        sum_prompt = await session.scalar(
            select(func.coalesce(func.sum(AuditLog.prompt_tokens), 0)).where(mine)
        ) or 0
        sum_completion = await session.scalar(
            select(func.coalesce(func.sum(AuditLog.completion_tokens), 0)).where(mine)
        ) or 0
        sum_total = await session.scalar(
            select(func.coalesce(func.sum(AuditLog.total_tokens), 0)).where(mine)
        ) or 0
        sum_saved = await session.scalar(
            select(func.coalesce(func.sum(AuditLog.estimated_saved_tokens), 0)).where(mine)
        ) or 0

        result = await session.execute(
            select(AuditLog).where(mine).order_by(AuditLog.id.desc()).limit(limit)
        )
        rows = result.scalars().all()
        recent = [
            {
                "id": r.id,
                "timestamp": r.timestamp.isoformat() if r.timestamp else None,
                "clearance_level": r.clearance_level,
                "query": (r.query_text or "")[:160],
                "gated": bool(r.gated),
                "prompt_tokens": r.prompt_tokens,
                "completion_tokens": r.completion_tokens,
                "total_tokens": r.total_tokens,
                "estimated_saved_tokens": r.estimated_saved_tokens,
                "token_source": r.token_source,
            }
            for r in rows
        ]

    avg_total = round(sum_total / answered, 1) if answered else 0
    return {
        "client": cid,
        "totals": {
            "queries": total_queries,
            "answered": answered,
            "gated": gated,
            "prompt_tokens": sum_prompt,
            "completion_tokens": sum_completion,
            "total_tokens": sum_total,
            "estimated_saved_tokens": sum_saved,
            "avg_tokens_per_answer": avg_total,
        },
        "recent": recent,
    }
