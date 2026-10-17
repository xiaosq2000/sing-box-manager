"""No native commands run here. Privileged effects use temporary-file stand-ins."""

import os
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from sing_box_manager.managed_access import node, storage
from tests import native_managed as native


@pytest.mark.parametrize(
    "missing", ["GITHUB_ACTIONS", "RUNNER_ENVIRONMENT", "SBM_MANAGED_NATIVE_E2E"]
)
def test_native_refuses_before_any_command(monkeypatch, missing):
    for name, value in [
        ("GITHUB_ACTIONS", "true"),
        ("RUNNER_ENVIRONMENT", "github-hosted"),
        ("SBM_MANAGED_NATIVE_E2E", "1"),
    ]:
        monkeypatch.setenv(name, value)
    monkeypatch.delenv(missing)
    monkeypatch.setattr(
        native, "run", lambda *a, **kw: pytest.fail("Host command before guard")
    )
    with pytest.raises(RuntimeError, match="opted-in disposable"):
        native.main()


@pytest.mark.parametrize(
    "release,existing",
    [
        ({"ID": "ubuntu", "VERSION_ID": "26.04"}, False),
        ({"ID": "other", "VERSION_ID": "24.04"}, False),
        ({"ID": "ubuntu", "VERSION_ID": "24.04"}, True),
    ],
)
def test_native_refuses_wrong_host_or_existing_target(monkeypatch, release, existing):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("RUNNER_ENVIRONMENT", "github-hosted")
    monkeypatch.setenv("SBM_MANAGED_NATIVE_E2E", "1")
    monkeypatch.setattr(native.platform, "freedesktop_os_release", lambda: release)
    monkeypatch.setattr(native.os.path, "lexists", lambda path: existing)
    monkeypatch.setattr(
        native, "run", lambda *a, **kw: pytest.fail("Host command before guard")
    )
    with pytest.raises(RuntimeError):
        native.main()


def test_native_processes_clear_download_proxies_and_capture_output(monkeypatch):
    for key in native.PROXY_NAMES:
        monkeypatch.setenv(key, "synthetic proxy")
        monkeypatch.setenv(key.upper(), "synthetic proxy")
    calls = []

    def invoke(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout=b"synthetic-private-output")

    monkeypatch.setattr(native.subprocess, "run", invoke)
    native.local_ssh(
        "never-contact-this-host.test", "printf synthetic", "fixed failure"
    )
    native.python('print("synthetic-private-body")', user="sbm")
    assert calls[0][0] == ["sudo", "bash", "-eu", "-c", "printf synthetic"]
    assert calls[1][0][:5] == ["sudo", "-u", "sbm", "sh", "-c"]
    assert calls[1][0][5].startswith("cd /srv/sbm-managed-native && exec env -i ")
    assert calls[1][1]["cwd"] is None
    assert b"synthetic-private-body" in calls[1][1]["input"]
    assert b"<native-fixture>" in calls[1][1]["input"]
    for _, kwargs in calls:
        assert not any(key.lower() in native.PROXY_NAMES for key in kwargs["env"])
        assert kwargs["capture_output"] is True
        assert kwargs["timeout"] <= 600


def test_native_fixture_contains_only_loopback_and_example_inputs(monkeypatch):
    from sing_box_manager.account_copy.export import _parse_source

    runtime = native.settings()
    writes = {}
    monkeypatch.setattr(native, "shell", lambda *a: None)
    monkeypatch.setattr(
        native,
        "write",
        lambda path, data, mode=0o640: writes.update({path: (data, mode)}),
    )
    native.seed_files(runtime)
    parsed = _parse_source(writes[native.CONFIG][0], native.CONFIG)
    assert parsed.model_dump() == runtime.model_dump()
    assert parsed.deployment.ip == "127.0.0.1"
    assert parsed.deployment.host == "vpn.example.test"
    assert parsed.web.allowed_hosts == ["vpn.example.test", "localhost", "127.0.0.1"]
    assert (
        len(
            {
                parsed.control_plane.secret_key,
                parsed.control_plane.subscription_secret,
                parsed.web.subscription_secret,
                parsed.web.session_secret,
            }
        )
        == 4
    )
    assert writes[native.CONFIG][1] == 0o640
    assert writes[Path("/usr/local/etc/sing-box/config.json")][1] == 0o600


def test_native_setup_keeps_all_host_commands_local(tmp_path, monkeypatch):
    source = tmp_path / "source"
    binary = source / ".cache/sing-box-manager/server/sing-box"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"synthetic-binary")
    monkeypatch.setattr(native, "SOURCE", source)
    monkeypatch.setattr(native.shutil, "which", lambda name: "/fixture/bin/pixi")
    monkeypatch.setattr(
        native.subprocess, "run", lambda *a, **kw: pytest.fail("Unmocked host command")
    )
    monkeypatch.setattr(native, "python", lambda *a, **kw: SimpleNamespace(stdout=b""))
    monkeypatch.setattr(native, "wait_http", lambda *a, **kw: None)
    calls = []

    def invoke(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout=b"ubuntu\n")

    monkeypatch.setattr(native, "run", invoke)
    native.setup(native.settings())
    assert calls and all(argv[0] == "sudo" for argv in calls)
    assert not any("ssh" in argv or "rsync" in argv for argv in calls)
    assert any("nginx -t" in " ".join(argv) for argv in calls)
    assert any(argv[-1] == "systemctl restart nginx" for argv in calls)
    assert any(
        "systemctl restart sing-box-manager-control-plane.service" in " ".join(argv)
        for argv in calls
    )


def test_native_python_errors_do_not_print_process_output(monkeypatch):
    monkeypatch.setattr(
        native,
        "run",
        lambda *a, **kw: SimpleNamespace(
            returncode=1,
            stdout=b"synthetic-private-stdout",
            stderr=b"synthetic-private-stderr\nNATIVE_ERROR:AssertionError:7\n",
        ),
    )
    with pytest.raises(native.NativeFailure) as failure:
        native.python("assert False")
    assert (
        str(failure.value) == "Native Python failed: AssertionError at fixture line 7"
    )


def test_new_node_state_supports_private_umask(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setattr(storage, "STATE_ROOT", state)
    monkeypatch.setattr(node, "directory", lambda *args, **kwargs: nullcontext())
    monkeypatch.setattr(node.os, "geteuid", lambda: 0)
    monkeypatch.setattr(node.os, "chown", lambda *args: None)
    monkeypatch.setattr(
        node.pwd, "getpwnam", lambda name: SimpleNamespace(pw_gid=os.getegid())
    )
    original_stat, original_fstat = Path.stat, os.fstat

    def info(value):
        return SimpleNamespace(
            st_uid=0,
            st_gid=os.getegid(),
            st_mode=value.st_mode,
            st_nlink=value.st_nlink,
        )

    monkeypatch.setattr(
        Path,
        "stat",
        lambda path, **kwargs: (
            info(original_stat(path, **kwargs))
            if path == state
            else original_stat(path, **kwargs)
        ),
    )
    monkeypatch.setattr(node.os, "fstat", lambda fd: info(original_fstat(fd)))
    mask = os.umask(0o077)
    try:
        with node._locked():
            assert original_stat(state).st_mode & 0o777 == 0o750
        state.chmod(0o700)
        with (
            pytest.raises(node.NodeRejected, match="unsafe_state_directory"),
            node._locked(),
        ):
            pass
        assert original_stat(state).st_mode & 0o777 == 0o700
    finally:
        os.umask(mask)
