"""One connection per deploy, and retries only where they are provably safe."""

from __future__ import annotations

import subprocess

import pytest

from sing_box_manager import ssh

HOST = "vpn-host"

# What the client actually printed when a middlebox reset the handshake
# mid-deploy, alongside the matching `[preauth]` line the server logged.
RESET_DURING_HANDSHAKE = (
    "kex_exchange_identification: read: Connection reset by peer\n"
    f"Connection reset by 203.0.113.10 port 27484\n"
)


def test_options_carry_liveness_probes_even_without_a_session() -> None:
    options = ssh.options_for(HOST)

    assert "ConnectTimeout=15" in options
    assert "ServerAliveInterval=15" in options
    assert "ControlMaster=auto" not in options


def test_a_session_puts_every_ssh_and_rsync_on_one_connection() -> None:
    with ssh.multiplexed_session(HOST):
        options = ssh.options_for(HOST)
        control_path = next(
            option.removeprefix("ControlPath=")
            for option in options
            if option.startswith("ControlPath=")
        )

        assert "ControlMaster=auto" in options
        # rsync splits `-e` on whitespace without honouring quotes.
        assert not any(char.isspace() for char in control_path)
        assert control_path in ssh.rsync_transport(HOST)
        assert control_path in " ".join(ssh.command(HOST, "true"))

    # Outside the session the socket is gone, so nothing may still point at it.
    assert "ControlMaster=auto" not in ssh.options_for(HOST)


def test_a_session_leaves_other_hosts_alone() -> None:
    with ssh.multiplexed_session(HOST):
        assert "ControlMaster=auto" not in ssh.options_for("other-host")


def test_the_teardown_survives_a_deploy_that_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[list[str]] = []
    monkeypatch.setattr(
        ssh.subprocess,
        "run",
        lambda command, **_: closed.append(command)
        or subprocess.CompletedProcess(command, 0),
    )

    with pytest.raises(RuntimeError), ssh.multiplexed_session(HOST):
        raise RuntimeError("deploy blew up")

    assert closed and closed[-1][-2:] == ["exit", HOST]
    assert HOST not in ssh._CONTROL_PATHS


def test_ssh_command_shape() -> None:
    assert ssh.command(HOST, "true", tty=True)[0] == "ssh"
    assert ssh.command(HOST, "true", tty=True)[-2:] == [HOST, "true"]
    assert "-t" in ssh.command(HOST, "true", tty=True)
    assert "-t" not in ssh.command(HOST, "true")
    assert ssh.command(HOST)[-1] == HOST


@pytest.mark.parametrize(
    "stderr",
    [
        RESET_DURING_HANDSHAKE,
        "ssh: connect to host vpn-host port 27484: Connection timed out\n",
        "Connection timed out during banner exchange\n",
        "mux_client_request_session: session request failed\n",
    ],
)
def test_failures_before_the_remote_shell_are_retryable(stderr: str) -> None:
    assert ssh.is_connection_failure(stderr)


@pytest.mark.parametrize(
    "stderr",
    [
        # Authentication is a settled answer; retrying only repeats it.
        "vpn-host: Permission denied (publickey).\n",
        # The command ran and failed on its own terms.
        "mv: cannot stat 'VERSION.tmp': No such file or directory\n",
        # Ambiguous: the remote may already have done the work.
        "Write failed: Broken pipe\n",
        "",
    ],
)
def test_failures_that_may_have_reached_the_remote_are_not_retryable(
    stderr: str,
) -> None:
    assert not ssh.is_connection_failure(stderr)


def test_a_lost_handshake_is_retried_until_it_lands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ssh.time, "sleep", lambda _: None)
    attempts = 0

    def attempt() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise subprocess.CalledProcessError(
                255, ["ssh"], stderr=RESET_DURING_HANDSHAKE
            )
        return "done"

    assert ssh.with_connection_retry(attempt) == "done"
    assert attempts == 3


def test_a_command_that_may_have_run_is_never_run_twice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `mv` that already happened must not be retried into a spurious failure."""
    monkeypatch.setattr(ssh.time, "sleep", lambda _: None)
    attempts = 0

    def attempt() -> None:
        nonlocal attempts
        attempts += 1
        raise subprocess.CalledProcessError(1, ["ssh"], stderr="mv: cannot stat\n")

    with pytest.raises(subprocess.CalledProcessError):
        ssh.with_connection_retry(attempt)

    assert attempts == 1


def test_retries_give_up_rather_than_looping_forever(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ssh.time, "sleep", lambda _: None)
    attempts = 0

    def attempt() -> None:
        nonlocal attempts
        attempts += 1
        raise subprocess.CalledProcessError(
            255, ["ssh"], stderr=RESET_DURING_HANDSHAKE
        )

    with pytest.raises(subprocess.CalledProcessError):
        ssh.with_connection_retry(attempt)

    assert attempts == ssh.MAX_ATTEMPTS


def test_captured_stderr_is_replayed_rather_than_swallowed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    ssh.echo_stderr(b"Connection to 203.0.113.10 closed.\n")

    assert "Connection to 203.0.113.10 closed." in capsys.readouterr().err
