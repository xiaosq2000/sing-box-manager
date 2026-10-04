"""Subscribe to sing-box connections and persist acknowledged usage batches.

Opening frames replay live connections and a bounded history of closed ones.
Checkpoints deduplicate those replays. An outage can still lose attribution once
closed connections leave that history; the separate V2Ray poller owns quota.
"""

from __future__ import annotations

import logging
import signal
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import grpc

from sing_box_manager import traffic_stats
from sing_box_manager.connection_tracking import ConnectionAggregator
from sing_box_manager.proto import sing_box_api_pb2 as pb
from sing_box_manager.proto import sing_box_api_pb2_grpc as pb_grpc
from sing_box_manager.settings import SUPPORTED_PROTOCOLS, Settings
from sing_box_manager.traffic_models import (
    TrafficStatsError,
    _utc_now,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

logger = logging.getLogger(__name__)

# Nanoseconds. The server does time.Duration(interval), so seconds here would be
# a busy loop rather than a slow tick, and its `<= 0` guard would not catch it.
# Five seconds rather than the one-second default: UPDATE events only matter for
# connections outliving a tick, and CLOSED reconciles to the authoritative total
# regardless, so a slower tick cuts event volume without costing accuracy.
STREAM_INTERVAL_NANOSECONDS = 5_000_000_000

FLUSH_INTERVAL_SECONDS = 60.0
INITIAL_BACKOFF_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 30.0
# How often the per-call canceller re-checks the shared stop flag. Only shutdown
# latency depends on it, so it is kept coarse enough to cost nothing.
CANCEL_POLL_SECONDS = 0.5


def open_stream(
    *, listen_address: str, secret: str, stop: threading.Event
) -> Iterator[pb.ConnectionEvents]:
    """Subscribe to one sing-box API service, yielding frames until stopped."""
    channel = grpc.insecure_channel(listen_address)
    metadata = (("authorization", f"Bearer {secret}"),) if secret else ()
    stub = pb_grpc.StartedServiceStub(channel)
    call = stub.SubscribeConnections(
        pb.SubscribeConnectionsRequest(interval=STREAM_INTERVAL_NANOSECONDS),
        metadata=metadata,
    )

    # Signals the canceller that this call is over, so it exits with the
    # subscription instead of outliving it. Without it every reconnect would
    # leave a thread parked on `stop` forever, each pinning a dead call and
    # channel, and a flapping API service would accumulate them until the
    # process ran out of threads.
    finished = threading.Event()
    canceller = threading.Thread(
        target=_cancel_when_stopped,
        args=(call, stop, finished),
        name="stream-canceller",
        daemon=True,
    )
    canceller.start()
    try:
        yield from call
    finally:
        finished.set()
        call.cancel()
        channel.close()


def _cancel_when_stopped(
    call: grpc.Call, stop: threading.Event, finished: threading.Event
) -> None:
    """Unblock the reading thread on shutdown.

    Iterating a gRPC stream ignores the stop flag until the next frame arrives,
    and on an idle server that may never happen, so shutdown has to reach in and
    cancel the call itself.

    ``stop`` outlives any single subscription, so this polls rather than
    blocking on it: the wait has to end when either the daemon stops or this
    particular call does.
    """
    while not finished.is_set():
        if stop.wait(CANCEL_POLL_SECONDS):
            call.cancel()
            return


@dataclass(slots=True, frozen=True)
class ServiceEndpoint:
    protocol: str
    listen_address: str


def build_endpoints(settings: Settings) -> tuple[ServiceEndpoint, ...]:
    stats = settings.traffic_stats
    return (ServiceEndpoint("server", stats.connection_api_listen),)


class ConnectionStreamCollector:
    """One node subscription, partitioned by inbound into protocol accounting.

    Explicit protocol endpoints remain available to fixture collectors. The
    default node stream feeds three aggregators under one lock and flush loop.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        database_path: Path,
        endpoints: Sequence[ServiceEndpoint] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._clock = clock or _utc_now
        self._settings = settings
        self._store = traffic_stats.open_store(database_path)
        self.endpoints = tuple(endpoints or build_endpoints(settings))
        self._secret = settings.traffic_stats.connection_api_secret
        self._timezone = settings.traffic_stats.timezone
        self._stop = threading.Event()
        self._lock = threading.Lock()
        names = (
            SUPPORTED_PROTOCOLS
            if any(endpoint.protocol == "server" for endpoint in self.endpoints)
            else tuple(endpoint.protocol for endpoint in self.endpoints)
        )
        self._aggregators = {
            name: ConnectionAggregator(
                service_name=name,
                timezone_name=self._timezone,
                prior=self._store.read_connection_checkpoints(name),
            )
            for name in names
        }
        self._threads: list[threading.Thread] = []

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        for name in self._aggregators:
            self._store.clear_live_connections(name)
        for endpoint in self.endpoints:
            thread = threading.Thread(
                target=self._run_service,
                args=(endpoint,),
                name=f"stream-{endpoint.protocol}",
                daemon=True,
            )
            thread.start()
            self._threads.append(thread)

        try:
            while not self._stop.wait(FLUSH_INTERVAL_SECONDS):
                self._flush()
        finally:
            self._stop.set()
            for thread in self._threads:
                thread.join(timeout=5)
            # A final flush so a clean shutdown does not discard the last
            # minute, and a clear so the ops view does not show a dead snapshot.
            self._flush(final=True)

    def _run_service(self, endpoint: ServiceEndpoint) -> None:
        backoff = INITIAL_BACKOFF_SECONDS
        while not self._stop.is_set():
            try:
                self._consume(endpoint)
            except grpc.RpcError as exc:
                if self._stop.is_set():
                    return
                logger.warning(
                    "Connection stream for %s failed (%s); retrying in %.0fs",
                    endpoint.protocol,
                    getattr(exc, "code", lambda: "unknown")(),
                    backoff,
                )
            except Exception:
                if self._stop.is_set():
                    return
                logger.warning(
                    "Connection stream for %s raised; retrying in %.0fs",
                    endpoint.protocol,
                    backoff,
                    exc_info=True,
                )
            else:
                backoff = INITIAL_BACKOFF_SECONDS

            if self._stop.wait(backoff):
                return
            backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)

    def _consume(self, endpoint: ServiceEndpoint) -> None:
        try:
            for frame in open_stream(
                listen_address=endpoint.listen_address,
                secret=self._secret,
                stop=self._stop,
            ):
                if self._stop.is_set():
                    return
                with self._lock:
                    if endpoint.protocol == "server":
                        self._apply_node_frame(frame, now=self._clock())
                    else:
                        self._aggregators[endpoint.protocol].apply_frame(
                            frame, now=self._clock()
                        )
        finally:
            # Normal EOF, errors, and cancellation all invalidate visibility.
            # The checkpoint map remains intact for the next complete replay.
            with self._lock:
                names = (
                    tuple(self._aggregators)
                    if endpoint.protocol == "server"
                    else (endpoint.protocol,)
                )
                for name in names:
                    self._aggregators[name].disconnected()
                    self._publish_live(name, final=True)

    def _apply_node_frame(self, frame: pb.ConnectionEvents, *, now: datetime) -> None:
        for protocol, aggregator in self._aggregators.items():
            partition = pb.ConnectionEvents(reset=frame.reset)
            for event in frame.events:
                connection = event.connection
                # UPDATE has no metadata. Unknown IDs are ignored by each
                # aggregator; the one that saw NEW owns its deltas.
                if event.type == pb.CONNECTION_EVENT_UPDATE:
                    partition.events.add().CopyFrom(event)
                elif (connection.inboundType or connection.inbound) == protocol:
                    copied = partition.events.add()
                    copied.CopyFrom(event)
                    prefix = f"{protocol}:"
                    if protocol != "naive" and copied.connection.user.startswith(
                        prefix
                    ):
                        copied.connection.user = copied.connection.user[len(prefix) :]
            aggregator.apply_frame(partition, now=now)

    def _flush(self, *, final: bool = False) -> None:
        now = self._clock()
        for name, aggregator in self._aggregators.items():
            # One retry plus the events received while that batch was pending.
            # Bound work so a busy stream cannot starve the other services.
            for _ in range(2):
                with self._lock:
                    batch = aggregator.prepare_batch()
                if batch is None:
                    break
                try:
                    self._store.commit_stream_batch(batch, collected_at=now)
                except Exception:
                    logger.warning(
                        "Failed to commit connection stats for %s; batch retained",
                        name,
                        exc_info=True,
                    )
                    break
                with self._lock:
                    aggregator.acknowledge(batch)
            with self._lock:
                self._publish_live(name, final=final)

        self._compact_completed_days(now)

    def _publish_live(self, service_name: str, *, final: bool) -> None:
        """Publish under the state lock so a disconnect cannot race a stale write."""
        try:
            if final:
                self._store.clear_live_connections(service_name)
            else:
                now = self._clock()
                self._store.replace_live_connections(
                    service_name=service_name,
                    connections=self._aggregators[service_name].live_connections(
                        now=now
                    ),
                    collected_at=now,
                )
        except Exception:
            logger.warning(
                "Failed to publish live connections for %s", service_name, exc_info=True
            )

    def _compact_completed_days(self, now: datetime) -> None:
        today = traffic_stats.local_date(self._timezone, now)
        with self._lock:
            pending_dates = [
                day
                for aggregator in self._aggregators.values()
                if (day := aggregator.oldest_pending_date()) is not None
            ]
            before_date = min([today, *pending_dates])
        try:
            folded = self._store.compact_completed_days(
                before_date=before_date,
                top_n=self._settings.traffic_stats.domain_top_n,
                collected_at=now,
            )
            if folded:
                logger.info(
                    "Folded %d completed-day domains into %s",
                    folded,
                    traffic_stats.OTHER_DOMAIN_KEY,
                )
        except Exception:
            logger.warning("Failed to compact domain usage", exc_info=True)


def collect_connection_stream(settings: Settings) -> None:
    """Entry point for ``sbm stats-stream``: runs until signalled."""
    if not settings.traffic_stats.enabled:
        raise TrafficStatsError("traffic_stats is disabled in config")

    database_path = traffic_stats.resolve_database_path(settings)
    collector = ConnectionStreamCollector(
        settings=settings, database_path=database_path
    )

    def handle_signal(signum: int, frame: object) -> None:
        logger.info("Received signal %s; shutting down", signum)
        collector.stop()

    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, handle_signal)

    logger.info(
        "Subscribing to %s",
        ", ".join(
            f"{endpoint.protocol}={endpoint.listen_address}"
            for endpoint in collector.endpoints
        ),
    )
    collector.run()
