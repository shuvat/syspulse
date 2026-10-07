"""TCP ingest: receives newline-delimited JSON samples from agents and stores them."""

import asyncio
import contextlib
import logging
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.dialects.postgresql import insert
from sqlmodel import Session

from app.db import engine
from app.models import Host, Metric

logger = logging.getLogger(__name__)

INGEST_HOST = "0.0.0.0"
INGEST_PORT = 9000
# A real sample is ~150 bytes; anything this long is not a valid message.
MAX_LINE_BYTES = 64 * 1024

# Largest value a PostgreSQL BIGINT can hold; the agent sends uint64 counters.
BIGINT_MAX = 2**63 - 1

# Open agent connections, so shutdown can close them (see stop_ingest_server).
_clients: set[asyncio.StreamWriter] = set()


class MetricMessage(BaseModel):
    """One sample as sent by the agent. Unknown extra fields are ignored."""

    # strict: reject type coercion such as "42" -> 42 or 2048.5 -> int.
    model_config = ConfigDict(strict=True)

    host: str = Field(min_length=1, max_length=255)
    # Upper bound is year 2286; it also keeps the datetime conversion from overflowing.
    ts: int = Field(gt=0, lt=10**10)
    cpu_percent: float = Field(ge=0, le=100)
    mem_used_mb: int = Field(ge=0, le=BIGINT_MAX)
    mem_total_mb: int = Field(ge=0, le=BIGINT_MAX)
    net_rx_bytes: int = Field(ge=0, le=BIGINT_MAX)
    net_tx_bytes: int = Field(ge=0, le=BIGINT_MAX)


def parse_line(line: bytes) -> MetricMessage | None:
    """Parse one line into a message, or return None if it is malformed."""
    try:
        return MetricMessage.model_validate_json(line)
    except ValidationError:
        return None


def store_message(msg: MetricMessage) -> None:
    """Upsert the host and insert the sample in one transaction (blocking)."""
    # last_seen uses the server clock: it answers "when did we last hear from
    # this host", and must not depend on the agent's clock being correct.
    now = datetime.now(UTC)
    with Session(engine) as session:
        # One atomic statement instead of SELECT + INSERT, so two connections
        # announcing the same new host cannot race on the UNIQUE name.
        upsert = (
            insert(Host)
            .values(name=msg.host, first_seen=now, last_seen=now)
            .on_conflict_do_update(index_elements=["name"], set_={"last_seen": now})
            .returning(Host.id)
        )
        host_id = session.execute(upsert).scalar_one()

        session.add(
            Metric(
                host_id=host_id,
                ts=datetime.fromtimestamp(msg.ts, UTC),
                cpu_percent=msg.cpu_percent,
                mem_used_mb=msg.mem_used_mb,
                mem_total_mb=msg.mem_total_mb,
                net_rx_bytes=msg.net_rx_bytes,
                net_tx_bytes=msg.net_tx_bytes,
            )
        )
        session.commit()


async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Read lines from one agent until it disconnects. Never raises."""
    peer = writer.get_extra_info("peername")
    logger.info("agent connected: %s", peer)
    _clients.add(writer)
    try:
        while True:
            line = await reader.readline()
            if not line:  # EOF: the agent closed the connection.
                break

            msg = parse_line(line)
            if msg is None:
                logger.warning("bad line from %s: %.200r", peer, line)
                continue

            # The DB call blocks, so run it in a worker thread to keep the event
            # loop serving other agents. Awaiting it keeps samples in order.
            try:
                await asyncio.to_thread(store_message, msg)
            except Exception:
                # E.g. the database is down: drop this sample, keep the connection.
                logger.exception("failed to store sample from %s", peer)
    except ValueError:
        # readline() raises ValueError when a line exceeds MAX_LINE_BYTES.
        logger.warning("line too long from %s, closing connection", peer)
    except ConnectionError:
        logger.warning("connection reset by %s", peer)
    finally:
        _clients.discard(writer)
        writer.close()
        with contextlib.suppress(ConnectionError):
            await writer.wait_closed()
        logger.info("agent disconnected: %s", peer)


async def start_ingest_server(
    host: str = INGEST_HOST, port: int = INGEST_PORT
) -> asyncio.Server:
    """Start listening; each connection gets its own handle_client() task."""
    return await asyncio.start_server(handle_client, host, port, limit=MAX_LINE_BYTES)


async def stop_ingest_server(server: asyncio.Server) -> None:
    """Stop accepting connections and disconnect all agents."""
    server.close()
    # Since Python 3.12, wait_closed() also waits for every open connection.
    # Agents never disconnect on their own, so close them first; each
    # handle_client() then sees EOF and finishes normally.
    for writer in list(_clients):
        writer.close()
    await server.wait_closed()
