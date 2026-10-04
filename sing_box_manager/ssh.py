"""One SSH connection per deploy, and retries for the handshakes that still fail.

Every remote step used to open its own TCP connection and SSH handshake -- two
dozen of them in an ordinary deploy, plus one per rsync. That is free on a
clean path and expensive on a hostile one. When a middlebox forges RSTs during
the SSH banner exchange, *both* endpoints log "connection reset by peer" for
the same flow, which is only possible when neither of them sent the RST; at a
5% reset rate, twenty-five independent handshakes lose roughly two deploys in
three, always partway through and always leaving the remote half-updated.

`multiplexed_session` opens a control socket for the duration of a deploy, so
every later `ssh` and `rsync` rides one connection as a channel and the whole
run is exposed to a single handshake. Nothing here dials the master
explicitly: `ControlMaster=auto` means the first command through the socket
creates it, and that command is already covered by the retry below.

Retries are deliberately narrow. A transport that dies *after* `systemctl
restart` ran looks exactly like one that died before it, and not every remote
command is safe to run twice -- publishing metadata is a `mv` that fails the
second time. So a failure is only retried when ssh's own stderr proves it
happened during connection setup, before a remote shell could exist.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from pathlib import Path

# Bound how long a dead peer can stall a step, and notice a silently dropped
# connection instead of hanging on it for the kernel's full retransmit budget.
BASE_OPTIONS: tuple[str, ...] = (
    "-o",
    "ConnectTimeout=15",
    "-o",
    "ServerAliveInterval=15",
    "-o",
    "ServerAliveCountMax=4",
)

# The persist window only has to outlast the longest idle gap between deploy
# steps, not the deploy itself: an active transfer keeps the master busy, and
# `ssh -O exit` tears it down when the session ends either way.
CONTROL_PERSIST_SECONDS = 300

MAX_ATTEMPTS = 5
RETRY_BACKOFF_SECONDS: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0)

# Every one of these is emitted before ssh has a remote shell to talk to, so
# the command they accompany provably never ran. Anything else -- a mid-session
# "Broken pipe", a bare "Connection closed by <host>" -- is ambiguous about
# whether the remote already did the work, and is left to fail.
CONNECTION_FAILURE_MARKERS: tuple[str, ...] = (
    # Reset or timed out between TCP connect and the version banner.
    "kex_exchange_identification",
    "ssh_exchange_identification",
    "banner exchange",
    # TCP never came up at all: refused, unroutable, unresolvable, timed out.
    "ssh: connect to host",
    "Could not resolve hostname",
    # A multiplexed channel was refused, so the session request reached no shell.
    "mux_client_",
    "Control socket connect",
)

# Sockets under this path are shared by every ssh and rsync in one deploy.
# Keyed by hostname so a nested session for a different host cannot collide.
_CONTROL_PATHS: dict[str, str] = {}


def options_for(hostname: str) -> tuple[str, ...]:
    """Common ssh options for `hostname`, multiplexed if a session is open."""
    control_path = _CONTROL_PATHS.get(hostname)
    if control_path is None:
        return BASE_OPTIONS
    # `auto` rather than `yes`: if the master dies mid-deploy, later commands
    # quietly open their own connection instead of failing outright.
    return (
        *BASE_OPTIONS,
        "-o",
        "ControlMaster=auto",
        "-o",
        f"ControlPath={control_path}",
        "-o",
        f"ControlPersist={CONTROL_PERSIST_SECONDS}",
    )


def command(
    hostname: str, remote_command: str | None = None, *, tty: bool = False
) -> list[str]:
    """Build an ssh argv for `hostname` carrying this deploy's options."""
    argv = ["ssh", *options_for(hostname)]
    if tty:
        argv.append("-t")
    argv.append(hostname)
    if remote_command is not None:
        argv.append(remote_command)
    return argv


def rsync_transport(hostname: str) -> str:
    """The `rsync -e` transport string, sharing this deploy's connection.

    Forces no-PTY: user SSH config entries like `RequestTTY force` would
    otherwise allocate a pseudo-terminal for the data channel, and PTY
    artifacts (CR/LF translation, X11 warnings, MOTD bytes) corrupt rsync's
    binary wire protocol on the first handshake.
    """
    # rsync splits `-e` on whitespace without honouring quotes, so the options
    # have to survive naive splitting. `multiplexed_session` guarantees the
    # control path contains none; nothing else here can.
    return " ".join(["ssh", "-T", "-o", "RequestTTY=no", *options_for(hostname)])


def is_connection_failure(text: str) -> bool:
    """Whether ssh's stderr says the failure predates the remote command."""
    return any(marker in text for marker in CONNECTION_FAILURE_MARKERS)


def decode_stderr(stderr: str | bytes | None) -> str:
    """Normalise whatever `subprocess` handed back into text."""
    if stderr is None:
        return ""
    if isinstance(stderr, bytes):
        return stderr.decode("utf-8", errors="replace")
    return stderr


def echo_stderr(stderr: str | bytes | None) -> None:
    """Replay captured stderr, which is captured only so it can be classified.

    Callers pipe stderr to classify a failure, not to hide it: an ssh warning
    the user would have seen live must still reach them.
    """
    text = decode_stderr(stderr)
    if text:
        sys.stderr.write(text if text.endswith("\n") else f"{text}\n")
        sys.stderr.flush()


def with_connection_retry[T](attempt: Callable[[], T]) -> T:
    """Run `attempt`, retrying only what ssh proves never reached the remote.

    `attempt` must raise `subprocess.CalledProcessError` with stderr captured;
    an attempt whose stderr is unavailable is treated as unclassifiable and
    therefore not retried.
    """
    for index in range(MAX_ATTEMPTS):
        try:
            return attempt()
        except subprocess.CalledProcessError as exc:
            stderr = decode_stderr(exc.stderr)
            if index == MAX_ATTEMPTS - 1 or not is_connection_failure(stderr):
                raise
            delay = RETRY_BACKOFF_SECONDS[min(index, len(RETRY_BACKOFF_SECONDS) - 1)]
            echo_stderr(stderr)
            print(
                f"  SSH connection failed before the remote command started; "
                f"retrying in {delay:.0f}s "
                f"(attempt {index + 2} of {MAX_ATTEMPTS}).",
                file=sys.stderr,
            )
            time.sleep(delay)
    raise AssertionError("retry loop exited without a result")


@contextlib.contextmanager
def multiplexed_session(hostname: str) -> Iterator[None]:
    """Route every ssh and rsync for `hostname` over one shared connection."""
    if hostname in _CONTROL_PATHS:
        yield
        return

    directory = tempfile.mkdtemp(prefix="sbm-ssh-")
    control_path = os.fspath(Path(directory) / "s")
    if not _usable_control_path(control_path):
        shutil.rmtree(directory, ignore_errors=True)
        yield
        return

    _CONTROL_PATHS[hostname] = control_path
    try:
        yield
    finally:
        _CONTROL_PATHS.pop(hostname, None)
        _close_master(hostname, control_path)
        shutil.rmtree(directory, ignore_errors=True)


def _usable_control_path(control_path: str) -> bool:
    """Whether a socket path is short enough and safe to splice into `-e`.

    `sockaddr_un.sun_path` holds 108 bytes on Linux and 104 on BSD, and ssh
    appends nothing but still needs room; whitespace would break rsync's `-e`
    splitting. An exotic TMPDIR should cost multiplexing, not the deploy.
    """
    return len(control_path) <= 90 and not any(ch.isspace() for ch in control_path)


def _close_master(hostname: str, control_path: str) -> None:
    subprocess.run(
        ["ssh", "-o", f"ControlPath={control_path}", "-O", "exit", hostname],
        check=False,
        capture_output=True,
    )
