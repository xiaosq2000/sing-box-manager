"""Connection lifecycle, replay deduplication, and acknowledged usage batches.

All counters here include pending usage. Only an acknowledged StreamBatch is
known to be durable. Transport and database failures do not mutate this state.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from uuid import uuid4

from sing_box_manager.proto import sing_box_api_pb2 as pb
from sing_box_manager.traffic_models import (
    NO_DOMAIN_KEY,
    STREAM_ATTRIBUTED_RETENTION,
    ConnectionCheckpoint,
    DatedDomainDelta,
    DomainDelta,
    LiveConnection,
    StreamBatch,
    UserTrafficCounters,
    local_date,
)

# Connections held in the live snapshot per service. A server under load can hold
# far more; the ops view is for eyeballing, not for exhaustive listing.
LIVE_SNAPSHOT_LIMIT = 500


def _domain_of(connection: pb.Connection) -> str:
    """Best available hostname for a connection.

    ``Connection.domain`` is only populated by an explicit sniff action, which
    the server configs do not use. It does not need to be: trojan, hysteria2 and
    naive all carry the target hostname in the proxy handshake, so sing-box puts
    it in ``destination`` as ``example.com:443``. Verified against a real 1.14.0
    server, where ``domain`` came through empty and ``destination`` held the name.
    """
    host = _host_part(connection.domain) if connection.domain else ""
    if not host:
        host = _host_part(connection.destination)
    if not host or _is_ip_literal(host):
        return NO_DOMAIN_KEY

    return _clean_host(host)


# Long enough for any real hostname (DNS caps a name at 253 octets) and short
# enough to bound a table cell.
MAX_DOMAIN_LENGTH = 253


def _clean_host(host: str) -> str:
    """Normalise a hostname, and refuse to trust it as printable text.

    The hostname arrives from the proxy handshake, so it is whatever the client
    chose to send rather than anything sing-box validated. Control characters in
    it would reach lxml when the ranking chart is rendered, and lxml refuses to
    serialise them -- one malformed name would 500 every admin page that charts
    the row.
    """
    cleaned = "".join(
        character
        for character in host.strip().rstrip(".").lower()
        if character.isprintable()
    )
    return cleaned[:MAX_DOMAIN_LENGTH] or NO_DOMAIN_KEY


def _host_part(address: str) -> str:
    """Strip the port from a ``host:port``, tolerating bracketed IPv6."""
    if not address:
        return ""

    if address.startswith("["):
        closing = address.find("]")
        return address[1:closing] if closing != -1 else address[1:]

    host, separator, _ = address.rpartition(":")
    if not separator:
        return address

    # A bare IPv6 literal has many colons and no port; rpartition would eat a
    # hextet instead of a port.
    return address if host.count(":") else host


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _destination_ip(connection: pb.Connection) -> str:
    host = _host_part(connection.destination)
    return host if host and _is_ip_literal(host) else ""


def _timestamp(milliseconds: int) -> datetime | None:
    if not milliseconds:
        return None
    return datetime.fromtimestamp(milliseconds / 1000, tz=UTC)


@dataclass(slots=True)
class _ConnectionRecord:
    """Live metadata and bytes accounted for, including uncommitted batches."""

    username: str
    domain: str
    destination_ip: str
    accounted: UserTrafficCounters = field(default_factory=UserTrafficCounters)
    network: str = ""
    protocol: str = ""
    source: str = ""
    destination: str = ""
    outbound: str = ""
    created_at: datetime | None = None
    total: UserTrafficCounters = field(default_factory=UserTrafficCounters)


class ConnectionAggregator:
    """Turns one service's event stream into per-domain daily deltas.

    Deliberately free of gRPC and threading so the attribution rules -- which are
    where the double-counting hazards live -- can be exercised directly.
    """

    def __init__(
        self,
        *,
        service_name: str,
        timezone_name: str,
        prior: dict[str, ConnectionCheckpoint] | None = None,
    ) -> None:
        self.service_name = service_name
        self._timezone_name = timezone_name
        self._checkpoints = dict(prior or {})
        self._records: dict[str, _ConnectionRecord] = {}
        self._pending: dict[tuple[date, str, str, str], list[int]] = {}
        self._dirty: dict[str, ConnectionCheckpoint] = {}
        self._removed: set[str] = set()
        self._inflight: StreamBatch | None = None
        self._available = False

    def apply_frame(self, frame: pb.ConnectionEvents, *, now: datetime) -> None:
        if frame.reset:
            # The complete opening frame includes all live connections and the
            # closed replay ring. Anything absent can no longer be replayed.
            replayed = {event.id for event in frame.events}
            for connection_id in self._checkpoints.keys() - replayed:
                self._checkpoints.pop(connection_id)
                self._dirty.pop(connection_id, None)
                self._removed.add(connection_id)
            self._records.clear()
        self._available = True
        for event in frame.events:
            self.apply_event(event, now=now)
        self._prune_retired()

    def disconnected(self) -> None:
        """Hide stale live records while retaining accounting and replay state."""
        self._available = False

    def apply_event(self, event: pb.ConnectionEvent, *, now: datetime) -> None:
        self._available = True
        if event.type == pb.CONNECTION_EVENT_UPDATE:
            self._apply_update(event, now=now)
        elif event.type == pb.CONNECTION_EVENT_CLOSED:
            self._apply_closed(event, now=now)
        elif event.type == pb.CONNECTION_EVENT_NEW:
            self._apply_new(event, now=now)

    def _apply_new(self, event: pb.ConnectionEvent, *, now: datetime) -> None:
        connection = event.connection
        if not connection.user:
            # DNS lookups and health checks arrive with no user. Bucketing them
            # under a placeholder would put traffic nobody owns in the table.
            return

        record, counted = self._ensure_record(event.id, connection)
        self._attribute_to_total(
            event.id,
            record,
            total=UserTrafficCounters(connection.uplinkTotal, connection.downlinkTotal),
            now=now,
            counted=counted,
        )
        if connection.closedAt:
            # A replayed already-closed connection: never becomes live.
            self._retire(event.id, closed_at=_timestamp(connection.closedAt) or now)

    def _apply_update(self, event: pb.ConnectionEvent, *, now: datetime) -> None:
        record = self._records.get(event.id)
        if record is None:
            # UPDATE carries no Connection at all -- verified against a real
            # server -- so with no prior NEW there is nothing to attribute it to.
            return

        if not event.uplinkDelta and not event.downlinkDelta:
            # sing-box emits a zero-delta UPDATE whenever a connection stops
            # moving bytes. Nothing changed, so rewriting the attribution row
            # would only churn the table and re-run its prune every flush.
            return

        self._add_pending(
            record, upload=event.uplinkDelta, download=event.downlinkDelta, now=now
        )
        record.accounted = UserTrafficCounters(
            record.accounted.upload_bytes + event.uplinkDelta,
            record.accounted.download_bytes + event.downlinkDelta,
        )
        record.total = UserTrafficCounters(
            record.total.upload_bytes + event.uplinkDelta,
            record.total.download_bytes + event.downlinkDelta,
        )
        self._checkpoint(event.id, record.accounted)

    def _apply_closed(self, event: pb.ConnectionEvent, *, now: datetime) -> None:
        connection = event.connection
        record = self._records.get(event.id)
        # An already-tracked connection was counted when it opened; only one
        # arriving straight to CLOSED still needs counting.
        counted = True
        if record is None:
            if not connection.user:
                return
            record, counted = self._ensure_record(event.id, connection)

        if connection.id or not counted:
            # CLOSED carries the full Connection with final totals, so this
            # settles whatever the UPDATE deltas did not cover.
            self._attribute_to_total(
                event.id,
                record,
                total=UserTrafficCounters(
                    connection.uplinkTotal, connection.downlinkTotal
                ),
                now=now,
                counted=counted,
            )
        self._retire(
            event.id, closed_at=_timestamp(event.closedAt or connection.closedAt) or now
        )

    def _ensure_record(
        self, connection_id: str, connection: pb.Connection
    ) -> tuple[_ConnectionRecord, bool]:
        """Return the tracked record, and whether it was already counted once.

        A connection seen before -- replayed after a reconnect -- must not add to
        connection_count again, or the ops view would multiply every reconnect.
        """
        record = self._records.get(connection_id)
        if record is not None:
            return record, True

        prior = self._checkpoints.get(connection_id)
        record = _ConnectionRecord(
            username=connection.user,
            domain=_domain_of(connection),
            destination_ip=_destination_ip(connection),
            accounted=prior.counters if prior else UserTrafficCounters(),
            network=connection.network,
            protocol=connection.protocol,
            source=connection.source,
            destination=connection.destination,
            outbound=connection.outbound,
            created_at=_timestamp(connection.createdAt),
            total=UserTrafficCounters(connection.uplinkTotal, connection.downlinkTotal),
        )
        self._records[connection_id] = record
        return record, prior.counted if prior else False

    def _attribute_to_total(
        self,
        connection_id: str,
        record: _ConnectionRecord,
        *,
        total: UserTrafficCounters,
        now: datetime,
        counted: bool,
    ) -> None:
        upload, download = total.upload_bytes, total.download_bytes
        owed_upload = max(0, upload - record.accounted.upload_bytes)
        owed_download = max(0, download - record.accounted.download_bytes)
        record.total = total
        if owed_upload or owed_download or not counted:
            self._add_pending(
                record,
                upload=owed_upload,
                download=owed_download,
                now=now,
                connections=0 if counted else 1,
            )
        record.accounted = UserTrafficCounters(
            max(record.accounted.upload_bytes, upload),
            max(record.accounted.download_bytes, download),
        )
        self._checkpoint(connection_id, record.accounted)

    def _add_pending(
        self,
        record: _ConnectionRecord,
        *,
        upload: int,
        download: int,
        now: datetime,
        connections: int = 0,
    ) -> None:
        if not upload and not download and not connections:
            return

        # Keyed by the local day at the moment the bytes are seen, so a flush
        # spanning midnight splits instead of dumping everything on one date.
        usage_date = local_date(self._timezone_name, now)
        key = (usage_date, record.username, record.domain, record.destination_ip)
        bucket = self._pending.setdefault(key, [0, 0, 0])
        bucket[0] += upload
        bucket[1] += download
        bucket[2] += connections

    def _checkpoint(self, connection_id: str, counters: UserTrafficCounters) -> None:
        checkpoint = ConnectionCheckpoint(connection_id, counters)
        if self._checkpoints.get(connection_id) != checkpoint:
            self._checkpoints[connection_id] = checkpoint
            self._dirty[connection_id] = checkpoint
        self._removed.discard(connection_id)

    def _retire(self, connection_id: str, *, closed_at: datetime) -> None:
        self._records.pop(connection_id, None)
        checkpoint = self._checkpoints[connection_id]
        checkpoint = replace(checkpoint, retired_at=closed_at)
        self._checkpoints[connection_id] = checkpoint
        self._dirty[connection_id] = checkpoint

    def _prune_retired(self) -> None:
        retired = sorted(
            (cp for cp in self._checkpoints.values() if cp.retired_at is not None),
            key=lambda cp: (cp.retired_at, cp.connection_id),
            reverse=True,
        )
        for checkpoint in retired[STREAM_ATTRIBUTED_RETENTION:]:
            self._checkpoints.pop(checkpoint.connection_id)
            self._dirty.pop(checkpoint.connection_id, None)
            self._removed.add(checkpoint.connection_id)

    def prepare_batch(self) -> StreamBatch | None:
        """Return the same batch until acknowledged; collect new events apart."""
        if self._inflight is not None:
            return self._inflight
        self._prune_retired()
        if not self._pending and not self._dirty and not self._removed:
            return None
        self._inflight = StreamBatch(
            service_name=self.service_name,
            batch_id=str(uuid4()),
            deltas=tuple(
                DatedDomainDelta(usage_date, DomainDelta(username, domain, ip, *bucket))
                for (usage_date, username, domain, ip), bucket in sorted(
                    self._pending.items()
                )
            ),
            checkpoints=tuple(self._dirty.values()),
            removed_connections=tuple(sorted(self._removed)),
        )
        self._pending.clear()
        self._dirty.clear()
        self._removed.clear()
        return self._inflight

    def acknowledge(self, batch: StreamBatch) -> None:
        if batch is not self._inflight:
            raise ValueError("Cannot acknowledge a different stream batch")
        self._inflight = None

    def oldest_pending_date(self) -> date | None:
        dates = [key[0] for key in self._pending]
        if self._inflight is not None:
            dates.extend(item.usage_date for item in self._inflight.deltas)
        return min(dates) if dates else None

    def live_connections(self, *, now: datetime) -> list[LiveConnection]:
        if not self._available:
            return []
        ordered = sorted(
            self._records.items(),
            key=lambda item: item[1].total.upload_bytes + item[1].total.download_bytes,
            reverse=True,
        )
        return [
            LiveConnection(
                connection_id=connection_id,
                service_name=self.service_name,
                username=record.username,
                network=record.network,
                protocol=record.protocol,
                source=record.source,
                destination=record.destination,
                domain=record.domain,
                outbound=record.outbound,
                created_at=record.created_at,
                upload_bytes=record.total.upload_bytes,
                download_bytes=record.total.download_bytes,
                updated_at=now,
            )
            for connection_id, record in ordered[:LIVE_SNAPSHOT_LIMIT]
        ]
