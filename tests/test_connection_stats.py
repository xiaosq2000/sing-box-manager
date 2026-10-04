"""Tests for the connection-stream aggregator.

These drive synthetic protobuf frames rather than a live sing-box, so the
attribution rules can be exercised directly. The shapes here were taken from a
real sing-box 1.14.0 server: UPDATE events carry only an id and deltas with no
Connection at all, CLOSED carries the full Connection with final totals, and the
opening frame replays already-closed connections as NEW.
"""

from datetime import UTC, datetime

from sing_box_manager import connection_stats, connection_tracking
from sing_box_manager.proto import sing_box_api_pb2 as pb
from sing_box_manager.traffic_models import (
    NO_DOMAIN_KEY,
    ConnectionCheckpoint,
    UserTrafficCounters,
)

NOW = datetime(2026, 4, 1, 12, 0, tzinfo=UTC)


def _aggregator(**kwargs) -> connection_tracking.ConnectionAggregator:
    return connection_tracking.ConnectionAggregator(
        service_name="trojan", timezone_name="UTC", **kwargs
    )


def _connection(
    *,
    user: str = "alice",
    destination: str = "example.com:443",
    domain: str = "",
    up: int = 0,
    down: int = 0,
    closed_at: int = 0,
    connection_id: str = "c1",
) -> pb.Connection:
    return pb.Connection(
        id=connection_id,
        user=user,
        network="tcp",
        source="10.0.0.1:5000",
        destination=destination,
        domain=domain,
        outbound="direct",
        uplinkTotal=up,
        downlinkTotal=down,
        closedAt=closed_at,
    )


def _new(connection: pb.Connection, connection_id: str = "c1") -> pb.ConnectionEvent:
    return pb.ConnectionEvent(
        type=pb.CONNECTION_EVENT_NEW, id=connection_id, connection=connection
    )


def _update(connection_id: str, up: int, down: int) -> pb.ConnectionEvent:
    return pb.ConnectionEvent(
        type=pb.CONNECTION_EVENT_UPDATE,
        id=connection_id,
        uplinkDelta=up,
        downlinkDelta=down,
    )


def _closed(connection: pb.Connection, connection_id: str = "c1") -> pb.ConnectionEvent:
    return pb.ConnectionEvent(
        type=pb.CONNECTION_EVENT_CLOSED,
        id=connection_id,
        connection=connection,
        closedAt=1,
    )


def _take_batch(aggregator):
    batch = aggregator.prepare_batch()
    if batch is None:
        return {}, {}
    by_date = {}
    for item in batch.deltas:
        by_date.setdefault(item.usage_date, []).append(item.delta)
    aggregator.acknowledge(batch)
    return by_date, {cp.connection_id: cp.counters for cp in batch.checkpoints}


def _totals(aggregator: connection_tracking.ConnectionAggregator) -> dict[str, tuple]:
    by_date, _ = _take_batch(aggregator)
    result = {}
    for deltas in by_date.values():
        for delta in deltas:
            previous = result.get(delta.domain, (0, 0, 0))
            result[delta.domain] = tuple(
                a + b
                for a, b in zip(
                    previous,
                    (delta.upload_bytes, delta.download_bytes, delta.connection_count),
                )
            )
    return result


class TestDomainResolution:
    def test_hostname_comes_from_destination_when_domain_is_empty(self):
        """Verified against a real server: without sniffing, domain is empty.

        The proxy handshake still carries the hostname, and sing-box puts it in
        destination, so the breakdown must read it from there or every row would
        collapse into the no-hostname bucket.
        """
        assert connection_tracking._domain_of(_connection()) == "example.com"

    def test_sniffed_domain_wins_when_present(self):
        connection = _connection(destination="93.184.216.34:443", domain="Example.COM.")
        assert connection_tracking._domain_of(connection) == "example.com"

    def test_ip_destinations_fall_into_the_no_hostname_bucket(self):
        connection = _connection(destination="93.184.216.34:443")
        assert connection_tracking._domain_of(connection) == NO_DOMAIN_KEY
        assert connection_tracking._destination_ip(connection) == "93.184.216.34"

    def test_ipv6_destinations_are_parsed_without_eating_a_hextet(self):
        connection = _connection(destination="[2606:2800:220:1::]:443")
        assert connection_tracking._domain_of(connection) == NO_DOMAIN_KEY
        assert connection_tracking._destination_ip(connection) == "2606:2800:220:1::"

    def test_hostname_is_lowercased_and_stripped_of_the_root_dot(self):
        connection = _connection(destination="Example.COM.:443")
        assert connection_tracking._domain_of(connection) == "example.com"

    def test_a_sniffed_ip_is_not_recorded_as_a_domain(self):
        """The sniffed field gets the same treatment as the destination.

        Reading it raw would put a bare address in the domain column beside real
        hostnames instead of the no-hostname bucket.
        """
        connection = _connection(
            destination="93.184.216.34:443", domain="93.184.216.34"
        )
        assert connection_tracking._domain_of(connection) == NO_DOMAIN_KEY

    def test_unprintable_hostnames_are_scrubbed(self):
        """The hostname is whatever the client sent, not something sing-box vetted.

        A control character in it reaches lxml when the ranking chart renders,
        and lxml refuses to serialise one, so a single crafted name would 500
        every admin page that charts the row.
        """
        connection = _connection(destination="bad\x01host.example:443")
        assert connection_tracking._domain_of(connection) == "badhost.example"

    def test_hostnames_are_capped_at_a_dns_name_length(self):
        connection = _connection(destination=f"{'a' * 400}.example:443")
        domain = connection_tracking._domain_of(connection)
        assert len(domain) == connection_tracking.MAX_DOMAIN_LENGTH


class TestAttribution:
    def test_a_full_connection_lifecycle_bills_the_final_total_once(self):
        aggregator = _aggregator()
        aggregator.apply_event(_new(_connection()), now=NOW)
        aggregator.apply_event(_update("c1", 100, 200), now=NOW)
        aggregator.apply_event(
            _closed(_connection(up=150, down=260, closed_at=1)), now=NOW
        )

        # 100/200 from the delta plus the 50/60 remainder the final total shows.
        assert _totals(aggregator) == {"example.com": (150, 260, 1)}

    def test_update_without_a_preceding_new_is_ignored(self):
        """UPDATE carries no Connection, so there is nothing to attribute it to.

        Silently dropping is right: guessing a user or domain would put bytes on
        the wrong row.
        """
        aggregator = _aggregator()
        aggregator.apply_event(_update("unknown", 100, 200), now=NOW)

        assert _totals(aggregator) == {}

    def test_closed_without_a_preceding_new_still_bills_in_full(self):
        """A connection that opened before the daemon did still closes on it."""
        aggregator = _aggregator()
        aggregator.apply_event(
            _closed(_connection(up=10, down=20, closed_at=1)), now=NOW
        )

        assert _totals(aggregator) == {"example.com": (10, 20, 1)}

    def test_connections_without_a_user_are_skipped(self):
        """DNS and health-check connections belong to nobody."""
        aggregator = _aggregator()
        aggregator.apply_event(_new(_connection(user="", up=10, down=20)), now=NOW)

        assert _totals(aggregator) == {}

    def test_bytes_are_grouped_per_domain(self):
        aggregator = _aggregator()
        aggregator.apply_event(_new(_connection(connection_id="c1"), "c1"), now=NOW)
        aggregator.apply_event(
            _new(
                _connection(connection_id="c2", destination="wikipedia.org:443"),
                "c2",
            ),
            now=NOW,
        )
        aggregator.apply_event(_update("c1", 10, 20), now=NOW)
        aggregator.apply_event(_update("c2", 1, 2), now=NOW)

        assert _totals(aggregator) == {
            "example.com": (10, 20, 1),
            "wikipedia.org": (1, 2, 1),
        }


class TestReplayDeduplication:
    def test_replayed_closed_connections_are_not_billed_twice(self):
        """The opening frame replays up to 1000 closed connections as NEW.

        Without the prior map, every reconnect would re-bill all of them.
        """
        aggregator = _aggregator()
        closed = _connection(up=100, down=200, closed_at=1)
        aggregator.apply_frame(
            pb.ConnectionEvents(reset=True, events=[_new(closed)]), now=NOW
        )
        first = _totals(aggregator)

        aggregator.apply_frame(
            pb.ConnectionEvents(reset=True, events=[_new(closed)]), now=NOW
        )

        assert first == {"example.com": (100, 200, 1)}
        assert _totals(aggregator) == {}

    def test_a_live_connection_is_not_rebilled_across_a_reconnect(self):
        """A still-open connection is replayed with its running total.

        The checkpoint stays available across a reset, so only bytes accrued
        since the disconnect are owed.
        """
        aggregator = _aggregator()
        aggregator.apply_frame(
            pb.ConnectionEvents(
                reset=True, events=[_new(_connection(up=100, down=200))]
            ),
            now=NOW,
        )
        assert _totals(aggregator) == {"example.com": (100, 200, 1)}

        # Reconnect: same connection, now with more traffic on it.
        aggregator.apply_frame(
            pb.ConnectionEvents(
                reset=True, events=[_new(_connection(up=150, down=260))]
            ),
            now=NOW,
        )

        assert _totals(aggregator) == {"example.com": (50, 60, 0)}

    def test_a_partially_billed_connection_replayed_as_closed_owes_the_remainder(self):
        """The hazard that makes the prior map store counts, not just ids.

        Deltas bill part of a connection, then the reset replay shows its final
        total. Billing that total in full would double-count the delta portion.
        """
        aggregator = _aggregator()
        aggregator.apply_event(_new(_connection()), now=NOW)
        aggregator.apply_event(_update("c1", 100, 200), now=NOW)
        assert _totals(aggregator) == {"example.com": (100, 200, 1)}

        aggregator.apply_frame(
            pb.ConnectionEvents(
                reset=True,
                events=[_new(_connection(up=150, down=260, closed_at=1))],
            ),
            now=NOW,
        )

        assert _totals(aggregator) == {"example.com": (50, 60, 0)}

    def test_checkpoint_seed_subtracts_already_accounted_bytes(self):
        """A loaded checkpoint supplies the starting accounted total."""
        aggregator = _aggregator(
            prior={"c1": ConnectionCheckpoint("c1", UserTrafficCounters(100, 200))},
        )
        aggregator.apply_frame(
            pb.ConnectionEvents(
                reset=True,
                events=[_new(_connection(up=150, down=260, closed_at=1))],
            ),
            now=NOW,
        )

        assert _totals(aggregator) == {"example.com": (50, 60, 0)}

    def test_attributions_are_reported_for_persistence(self):
        aggregator = _aggregator()
        aggregator.apply_event(_new(_connection()), now=NOW)
        aggregator.apply_event(_update("c1", 100, 200), now=NOW)

        _, attributions = _take_batch(aggregator)

        assert attributions == {"c1": UserTrafficCounters(100, 200)}

    def test_retired_checkpoint_retention_is_bounded(self):
        aggregator = _aggregator()
        limit = connection_tracking.STREAM_ATTRIBUTED_RETENTION
        for index in range(limit + 100):
            connection = _connection(
                connection_id=f"c{index}", up=1, down=1, closed_at=1
            )
            aggregator.apply_event(_new(connection, f"c{index}"), now=NOW)

        aggregator.prepare_batch()
        assert len(aggregator._checkpoints) == limit


class TestDayRollover:
    def test_bytes_are_bucketed_by_the_day_they_arrive(self):
        """A flush spanning midnight must split, not dump everything on one day."""
        aggregator = _aggregator()
        before = datetime(2026, 4, 1, 23, 59, tzinfo=UTC)
        after = datetime(2026, 4, 2, 0, 1, tzinfo=UTC)

        aggregator.apply_event(_new(_connection()), now=before)
        aggregator.apply_event(_update("c1", 10, 20), now=before)
        aggregator.apply_event(_update("c1", 1, 2), now=after)

        by_date, _ = _take_batch(aggregator)

        assert sorted(by_date) == [
            before.date(),
            after.date(),
        ]
        assert by_date[after.date()][0].upload_bytes == 1


class TestLiveSnapshot:
    def test_open_connections_are_reported_and_closed_ones_are_not(self):
        aggregator = _aggregator()
        aggregator.apply_event(_new(_connection(connection_id="open"), "open"), now=NOW)
        aggregator.apply_event(
            _new(_connection(connection_id="gone", closed_at=1), "gone"), now=NOW
        )

        live = aggregator.live_connections(now=NOW)

        assert [item.connection_id for item in live] == ["open"]
        assert live[0].username == "alice"
        assert live[0].domain == "example.com"

    def test_live_totals_track_updates(self):
        aggregator = _aggregator()
        aggregator.apply_event(_new(_connection()), now=NOW)
        aggregator.apply_event(_update("c1", 10, 20), now=NOW)

        assert aggregator.live_connections(now=NOW)[0].total_bytes == 30


class TestEndpoints:
    def test_endpoints_use_the_connection_api_ports(self):
        from sing_box_manager.settings import Settings

        settings = Settings.from_dict(
            {"sing_box_version": "1.14.0", "traffic_stats": {"enabled": True}}
        )

        endpoints = connection_stats.build_endpoints(settings)

        assert [(item.protocol, item.listen_address) for item in endpoints] == [
            ("server", "127.0.0.1:19090"),
        ]

    def test_stream_interval_is_expressed_in_nanoseconds(self):
        """A value in seconds would be a busy loop the server's guard misses."""
        assert connection_stats.STREAM_INTERVAL_NANOSECONDS >= 1_000_000_000
