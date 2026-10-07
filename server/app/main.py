"""FastAPI app: the REST API (port 8000) and the TCP ingest server (port 9000)."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Response, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, col, select

from app.db import create_db_and_tables, get_session
from app.ingest import INGEST_PORT, start_ingest_server, stop_ingest_server
from app.models import Anomaly, Host, Metric

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Code before `yield` runs at startup, code after it at shutdown."""
    create_db_and_tables()
    ingest = await start_ingest_server()
    logger.info("ingest listening on port %d", INGEST_PORT)
    yield
    await stop_ingest_server(ingest)
    logger.info("ingest stopped")


app = FastAPI(title="SysPulse", lifespan=lifespan)

SessionDep = Annotated[Session, Depends(get_session)]


# Response models: the public API shape, independent of the table layout
# (no internal ids; anomalies carry the host name instead of host_id).
class HostOut(BaseModel):
    name: str
    first_seen: datetime
    last_seen: datetime


class MetricOut(BaseModel):
    ts: datetime
    cpu_percent: float
    mem_used_mb: int
    mem_total_mb: int
    net_rx_bytes: int
    net_tx_bytes: int


class AnomalyOut(BaseModel):
    host: str
    ts: datetime
    type: str
    value: float
    message: str


# Endpoints are plain `def`: FastAPI runs them in a thread pool, so the
# blocking database calls do not stall the event loop shared with ingest.


@app.get("/health")
def health(session: SessionDep, response: Response) -> dict[str, str]:
    """Liveness plus database reachability; 503 if the database is down."""
    try:
        session.execute(text("SELECT 1"))
    except OperationalError:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "error", "database": "unreachable"}
    return {"status": "ok", "database": "ok"}


@app.get("/hosts", response_model=list[HostOut])
def list_hosts(session: SessionDep):
    """All known hosts with the time they were first and last heard from."""
    return session.exec(select(Host).order_by(Host.name)).all()


@app.get("/hosts/{name}/metrics", response_model=list[MetricOut])
def host_metrics(
    name: str,
    session: SessionDep,
    minutes: Annotated[int, Query(ge=1, le=1440)] = 10,
):
    """Samples of one host from the last `minutes` minutes, oldest first."""
    host = session.exec(select(Host).where(Host.name == name)).first()
    if host is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"host {name!r} not found")

    since = datetime.now(UTC) - timedelta(minutes=minutes)
    query = (
        select(Metric)
        .where(Metric.host_id == host.id, Metric.ts >= since)
        .order_by(col(Metric.ts))
    )
    return session.exec(query).all()


@app.get("/anomalies", response_model=list[AnomalyOut])
def list_anomalies(
    session: SessionDep,
    minutes: Annotated[int, Query(ge=1, le=1440)] = 60,
):
    """Anomalies across all hosts from the last `minutes` minutes, newest first."""
    since = datetime.now(UTC) - timedelta(minutes=minutes)
    query = (
        select(Anomaly, Host.name)
        .join(Host)
        .where(Anomaly.ts >= since)
        .order_by(col(Anomaly.ts).desc())
    )
    return [
        AnomalyOut(host=name, ts=a.ts, type=a.type, value=a.value, message=a.message)
        for a, name in session.exec(query).all()
    ]
