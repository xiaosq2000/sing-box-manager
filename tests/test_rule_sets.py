"""Snapshot consistency, validation, and failure handling without network access."""

import subprocess
from pathlib import Path

import httpx
import pytest

from sing_box_manager.release import rule_sets


def rule(name: str = "one", repository: str = "owner/rules") -> dict:
    return {
        "type": "remote",
        "tag": name,
        "format": "binary",
        "url": f"https://raw.githubusercontent.com/{repository}/rule-set/{name}.srs",
    }


def test_snapshots_resolve_each_source_once_and_never_send_token_to_raw(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []
    validated: list[bytes] = []
    commit = "a" * 40

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "api.github.com":
            assert request.headers["authorization"] == "Bearer fixture-token"
            return httpx.Response(200, json={"sha": commit})
        assert "authorization" not in request.headers
        assert f"/{commit}/" in request.url.path
        return httpx.Response(200, content=b"srs fixture")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(rule_sets.httpx, "Client", lambda **_: client)
    monkeypatch.setattr(
        rule_sets,
        "_validate_snapshot",
        lambda binary, path: validated.append(path.read_bytes()),
    )
    first, second = rule(), rule("two")
    rules = rule_sets.desktop_rule_sets(
        [
            {
                "route": {
                    "rule_set": [first, second, first, {"type": "inline", "rules": []}]
                }
            }
        ]
    )
    files = rule_sets.prepare_rule_snapshots(
        rules, tmp_path, Path("binary"), "fixture-token"
    )

    assert len(requests) == 3
    assert validated == [b"srs fixture", b"srs fixture"]
    assert files[-1].name == "files.txt"
    assert files[-1].read_text().splitlines() == sorted(rules)
    assert all(path.parent == tmp_path for path in files)


@pytest.mark.parametrize(
    "failure", ["revision", "download", "invalid_commit", "invalid_binary"]
)
def test_snapshot_failure_does_not_write_complete_inventory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.github.com":
            if failure == "revision":
                return httpx.Response(503)
            return httpx.Response(
                200,
                json={"sha": "../escape" if failure == "invalid_commit" else "a" * 40},
            )
        return httpx.Response(503 if failure == "download" else 200, content=b"invalid")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(rule_sets.httpx, "Client", lambda **_: client)

    def validate(binary: Path, path: Path) -> None:
        raise ValueError("Invalid SRS fixture")

    monkeypatch.setattr(rule_sets, "_validate_snapshot", validate)
    (tmp_path / "files.txt").write_text("previous inventory\n")
    with pytest.raises((ValueError, httpx.HTTPStatusError)):
        rule_sets.prepare_rule_snapshots(
            {rule_sets.snapshot_filename(rule()): rule()}, tmp_path, Path("binary")
        )
    assert not (tmp_path / "files.txt").exists()


@pytest.mark.parametrize(
    "url",
    [
        "http://raw.githubusercontent.com/owner/repo/main/a.srs",
        "https://evil.example/owner/repo/main/a.srs",
        "https://raw.githubusercontent.com/owner/repo/main/../a.srs",
        "https://raw.githubusercontent.com/owner/repo/main/a.srs?token=secret",
    ],
)
def test_invalid_snapshot_sources_are_rejected(url: str) -> None:
    with pytest.raises(ValueError):
        rule_sets._github_source(url)


def test_validation_runs_target_binary_with_local_rule_and_removes_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "rules.srs"
    source.write_bytes(b"invalid srs")
    calls = []

    def run(argv: list[str], **kwargs):
        calls.append(argv)
        import json

        config = json.loads(Path(argv[-1]).read_text())
        assert config["route"]["rule_set"][0]["type"] == "local"
        assert config["route"]["rule_set"][0]["path"] == str(source)
        return subprocess.CompletedProcess(argv, 1)

    monkeypatch.setattr(rule_sets.subprocess, "run", run)
    with pytest.raises(ValueError, match="Invalid bundled rule-set"):
        rule_sets._validate_snapshot(Path("/fixture/sing-box"), source)
    assert calls[0][:3] == ["/fixture/sing-box", "check", "-c"]
    assert not source.with_suffix(".check.json").exists()
