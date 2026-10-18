"""Stable link format, log redaction, and read-only legacy counter import."""

import logging
import sqlite3

from sing_box_manager.subscription import (
    TOKEN_PATTERN,
    TokenRedactingFilter,
    minimum_sing_box_version,
    parse_version,
    read_legacy_reset_counters,
    subscription_token,
)


def test_tokens_keep_the_existing_format():
    token = subscription_token("subscription-secret", "alice")
    assert TOKEN_PATTERN.fullmatch(token)
    assert token == subscription_token("subscription-secret", "alice", 0)
    assert len({token,
        subscription_token("subscription-secret", "bob"),
        subscription_token("subscription-secret", "alice", 1),
        subscription_token("other-secret", "alice"),
    }) == 4


def test_versions_and_minimum():
    assert parse_version("1.14.2") == (1, 14, 2)
    assert parse_version("1.15.0-beta.3") == (1, 15, 0)
    assert parse_version("latest") is None
    assert minimum_sing_box_version("1.14.2") == "1.14.0"


def test_log_filter_redacts_request_path_and_arguments():
    record = logging.LogRecord(
        "django.request", logging.WARNING, __file__, 1,
        "Not Found: %s", ("/sub/abcdefghijklmnopqrstuv?format=sbc",), None,
    )
    assert TokenRedactingFilter().filter(record)
    assert record.getMessage() == "Not Found: /sub/[token]?format=sbc"


def test_legacy_counters_are_only_read(tmp_path):
    path = tmp_path / "counters.sqlite3"
    assert read_legacy_reset_counters(path) == {}
    assert not path.exists()
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE reset_counters (username TEXT, counter INTEGER)")
        connection.execute("INSERT INTO reset_counters VALUES ('alice', 7)")
    before = path.read_bytes()
    path.chmod(0o400)
    assert read_legacy_reset_counters(path) == {"alice": 7}
    assert path.read_bytes() == before
