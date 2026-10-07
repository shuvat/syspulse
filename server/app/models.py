"""Database tables: hosts, metrics and anomalies."""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index
from sqlmodel import Field, SQLModel


class Host(SQLModel, table=True):
    __tablename__ = "hosts"

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(unique=True)
    first_seen: datetime = Field(sa_type=DateTime(timezone=True))
    last_seen: datetime = Field(sa_type=DateTime(timezone=True))


class Metric(SQLModel, table=True):
    __tablename__ = "metrics"
    # Matches the main query: samples of one host in a time window.
    __table_args__ = (Index("ix_metrics_host_id_ts", "host_id", "ts"),)

    id: int | None = Field(default=None, primary_key=True)
    host_id: int = Field(foreign_key="hosts.id")
    ts: datetime = Field(sa_type=DateTime(timezone=True))
    cpu_percent: float
    mem_used_mb: int
    mem_total_mb: int
    # Cumulative byte counters pass 2^31 (~2 GB) quickly, so INTEGER is too small.
    net_rx_bytes: int = Field(sa_type=BigInteger)
    net_tx_bytes: int = Field(sa_type=BigInteger)


class Anomaly(SQLModel, table=True):
    __tablename__ = "anomalies"

    id: int | None = Field(default=None, primary_key=True)
    host_id: int = Field(foreign_key="hosts.id")
    ts: datetime = Field(sa_type=DateTime(timezone=True))
    type: str
    value: float
    message: str
