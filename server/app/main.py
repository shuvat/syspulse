"""FastAPI app: the REST API (port 8000) and the TCP ingest server (port 9000)."""

import asyncio
import contextlib
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

from app.anomalies import check_silence, watcher_was_paused
from app.db import create_db_and_tables, engine, get_session
from app.ingest import INGEST_PORT, start_ingest_server, stop_ingest_server
from app.models import Anomaly, Host, Metric

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


SILENCE_CHECK_INTERVAL_S = 5


def record_silent_hosts(now: datetime | None = None) -> None:
    """Store a host_silent anomaly for each newly silent host (blocking).

    `now` can be passed by tests to control the clock.
    """
    now = now or datetime.now(UTC)
    with Session(engine) as session:
        for host in session.exec(select(Host)).all():
            finding = check_silence(host.last_seen, now)
            if finding is None:
                continue
            # One anomaly per silence: skip if this silence (i.e. one that
            # started at host.last_seen) was already recorded.
            already_recorded = session.exec(
                select(Anomaly.id).where(
                    Anomaly.host_id == host.id,
                    Anomaly.type == finding.type,
                    Anomaly.ts > host.last_seen,
                )
            ).first()
            if already_recorded is not None:
                continue
            logger.warning("anomaly on %s: %s", host.name, finding.message)
            session.add(
                Anomaly(
                    host_id=host.id,
                    ts=now,
                    type=finding.type,
                    value=finding.value,
                    message=finding.message,
                )
            )
        session.commit()


async def watch_silent_hosts() -> None:
    """Background task: silence produces no samples, so nothing else would notice it."""
    previous_run: datetime | None = None
    while True:
        now = datetime.now(UTC)
        if watcher_was_paused(previous_run, now):
            # The server itself was not running (e.g. the machine slept), so the missing
            # samples say nothing about the hosts. Skip one round: meanwhile the agents'
            # samples (sent or buffered) are stored and last_seen catches up.
            paused_s = (now - previous_run).total_seconds()
            logger.warning("silence watcher was paused for %.0f s, skipping one round", paused_s)
        else:
            try:
                await asyncio.to_thread(record_silent_hosts)
            except Exception:
                # E.g. the database is down: log and try again on the next round.
                logger.exception("silence check failed")
        previous_run = now
        await asyncio.sleep(SILENCE_CHECK_INTERVAL_S)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Code before `yield` runs at startup, code after it at shutdown."""
    create_db_and_tables()
    ingest = await start_ingest_server()
    logger.info("ingest listening on port %d", INGEST_PORT)
    watcher = asyncio.create_task(watch_silent_hosts())
    yield
    watcher.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await watcher
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
