"""Synthetic connection events and a controllable collector clock."""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from sing_box_manager import connection_stats
from sing_box_manager.proto import sing_box_api_pb2 as pb
from sing_box_manager.settings import Settings
from sing_box_manager.traffic_models import (
    DatedDomainDelta,
    DomainDelta,
    StreamBatch,
    UserTrafficCounters,
    _ServiceSnapshot,
    local_date,
)

NOW = datetime(2026, 4, 1, 12, tzinfo=UTC)


@dataclass
class Clock:
    now: datetime = NOW

    def __call__(self):
        return self.now


def collector(path, clock, *, top_n=50, timezone="UTC", protocols=("trojan",)):
    settings = Settings.from_dict(
        {
            "sing_box_version": "1.14.0",
            "traffic_stats": {
                "enabled": True,
                "timezone": timezone,
                "domain_top_n": top_n,
            },
        }
    )
    return connection_stats.ConnectionStreamCollector(
        settings=settings,
        database_path=path,
        clock=clock,
        endpoints=[
            connection_stats.ServiceEndpoint(protocol, "127.0.0.1:1")
            for protocol in protocols
        ],
    )


def new(
    connection_id="c1",
    *,
    up=0,
    down=0,
    user="alice",
    domain="example.com",
    ip="",
    closed_at=None,
):
    return pb.ConnectionEvent(
        type=pb.CONNECTION_EVENT_NEW,
        id=connection_id,
        connection=pb.Connection(
            id=connection_id,
            user=user,
            domain=domain,
            destination=f"{ip or domain}:443",
            network="tcp",
            uplinkTotal=up,
            downlinkTotal=down,
            closedAt=int(closed_at.timestamp() * 1000) if closed_at else 0,
        ),
    )


def update(connection_id="c1", *, up=0, down=0):
    return pb.ConnectionEvent(
        type=pb.CONNECTION_EVENT_UPDATE,
        id=connection_id,
        uplinkDelta=up,
        downlinkDelta=down,
    )


def apply(collector, clock, *events, reset=False, protocol="trojan"):
    collector._aggregators[protocol].apply_frame(
        pb.ConnectionEvents(reset=reset, events=events), now=clock()
    )


def poll(
    store,
    now,
    up,
    down=0,
    *,
    user="alice",
    protocol="trojan",
    uptime=100,
    timezone="UTC",
):
    store.record_snapshot(
        service_name=protocol,
        usage_date=local_date(timezone, now),
        collected_at=now,
        snapshot=_ServiceSnapshot(uptime, {user: UserTrafficCounters(up, down)}),
        billing_day=1,
    )


def write_usage(
    store,
    day,
    up,
    down=0,
    *,
    user="alice",
    protocol="trojan",
    domain="example.com",
    now=NOW,
):
    store.commit_stream_batch(
        StreamBatch(
            protocol,
            str(uuid4()),
            (DatedDomainDelta(day, DomainDelta(user, domain, "", up, down, 1)),),
        ),
        collected_at=now,
    )


def usage_rows(store, *, table="daily_domain_usage"):
    with store._connect() as db:
        return [dict(row) for row in db.execute(f"SELECT * FROM {table}")]


def totals(store, *, table="daily_domain_usage"):
    rows = usage_rows(store, table=table)
    return tuple(
        sum(row[key] for row in rows)
        for key in ("upload_bytes", "download_bytes", "connection_count")
    )
