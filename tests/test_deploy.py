"""Tests for remote deployment support.

The highest-severity thing in this file is the code sync's `--delete` scope. The
remote holds state nothing else can recreate -- the runtime config/inventory, the
traffic database, the pixi environment -- and a delete rule that reached any of
it would be an incident, not a bug. So the allowlist gets a test that inspects
every rsync command the deploy issues, rather than only checking that the happy
path happens to look right today.
"""

import shlex
import subprocess
from io import StringIO
from pathlib import Path

import pytest

from sing_box_manager.cli import main
from sing_box_manager.deploy import (
    CODE_FILE_PATHS,
    CODE_TREE_PATHS,
    DeploymentError,
    DeployOptions,
    _parse_rsync_progress_percent,
    _Remote,
    _rsync_directory,
    _Transfer,
    deploy_release,
)
from sing_box_manager.release import artifacts
from sing_box_manager.release.artifacts import ReleaseInfo, write_release_info
from sing_box_manager.release.manifest import MANIFEST_VERSION
from sing_box_manager.settings import Settings, TrafficStatsSettings

REMOTE_ROOT = "/opt/sing-box-manager"
HOST = "vpn-host"
RELEASE_DIR_NAME = "rel-cafebabe1234"
RSYNC_VERSION_LINE = "rsync  version 3.4.3  protocol version 32\n"
REMOTE_RSYNC_PATH = f"{REMOTE_ROOT}/.pixi/envs/default/bin/rsync"
REMOTE_PYTHON_PATH = f"{REMOTE_ROOT}/.pixi/envs/default/bin/python"
LOCAL_LOCK_DIGEST = "a" * 64
# Server-local paths are never direct rsync destinations. The runtime config is
# staged elsewhere and atomically published; the other paths remain untouched.
# `config/generated/` is deliberately absent -- deploy writes the auth snapshot
# and release metadata there, one file at a time and never with `--delete`.
SERVER_LOCAL_PATHS = (
    f"{REMOTE_ROOT}/config/inventory/runtime.yaml",
    f"{REMOTE_ROOT}/data",
    f"{REMOTE_ROOT}/.pixi",
)


def _settings(
    config_path: Path | None = None, *, traffic_stats_enabled: bool = False
) -> Settings:
    return Settings(
        sing_box_version="1.12.24",
        config_path=config_path,
        traffic_stats=TrafficStatsSettings(enabled=traffic_stats_enabled),
    )


def _is_control_socket_teardown(command: list[str]) -> bool:
    """Whether this is `ssh -O exit`, the best-effort multiplex teardown.

    It runs with `check=False` after the deploy has already finished, so it is
    neither a deploy step nor remote contact and does not belong in a recording.
    """
    return command[:1] == ["ssh"] and "-O" in command


class _Recorder:
    """Stands in for subprocess.run, recording commands and faking stdout."""

    def __init__(
        self,
        *,
        remote_lock_digest: str = LOCAL_LOCK_DIGEST,
        remote_manifest_version: str | None = None,
        web_unit_exists: bool = True,
    ) -> None:
        self.commands: list[list[str]] = []
        # Unit files travel as stdin to `sudo tee`, so the command alone says
        # which unit was written but not what it now contains.
        self.inputs: list[bytes] = []
        self._remote_lock_digest = remote_lock_digest
        self._remote_manifest_version = remote_manifest_version or str(
            MANIFEST_VERSION
        )
        self._web_unit_exists = web_unit_exists

    def __call__(
        self,
        command: list[str],
        *,
        check: bool,
        capture_output: bool = False,
        text: bool = False,
        stderr: int | None = None,
        input: bytes | None = None,  # noqa: A002
    ) -> subprocess.CompletedProcess[str]:
        if _is_control_socket_teardown(command):
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        assert check is True
        self.commands.append(command)
        if input is not None:
            self.inputs.append(input)
        return subprocess.CompletedProcess(
            command, 0, stdout=self._stdout_for(command), stderr=""
        )

    def _stdout_for(self, command: list[str]) -> str:
        if command == ["rsync", "--version"]:
            return RSYNC_VERSION_LINE
        if len(command) < 3 or command[0] != "ssh":
            return ""
        remote_command = command[-1]
        if remote_command.endswith("--version"):
            return RSYNC_VERSION_LINE
        if remote_command.startswith("sha256sum"):
            return f"{self._remote_lock_digest}\n"
        if "MANIFEST_VERSION" in remote_command:
            return f"{self._remote_manifest_version}\n"
        if "systemctl cat" in remote_command:
            return "1" if self._web_unit_exists else "0"
        return ""

    @property
    def rsync_commands(self) -> list[list[str]]:
        return [
            command
            for command in self.commands
            if command[0] == "rsync" and command != ["rsync", "--version"]
        ]

    @property
    def remote_commands(self) -> list[str]:
        return [command[-1] for command in self.commands if command[0] == "ssh"]

    def index_of(self, needle: str) -> int:
        """Position of the first recorded command containing `needle`."""
        for index, command in enumerate(self.commands):
            if any(needle in part for part in command):
                return index
        raise AssertionError(f"no recorded command contains {needle!r}")


def _write_lfs_fonts(project_root: Path) -> None:
    fonts_dir = project_root / "sing_box_manager" / "web" / "static" / "fonts"
    fonts_dir.mkdir(parents=True, exist_ok=True)
    for name in ("body.ttf", "mono.ttf"):
        (fonts_dir / name).write_bytes(b"\x00\x01\x00\x00" + b"glyph" * 220_000)


def _prepare_code_tree(project_root: Path) -> None:
    for tree in CODE_TREE_PATHS:
        (project_root / tree).mkdir(parents=True, exist_ok=True)
        (project_root / tree / "placeholder").write_text("x", encoding="utf-8")
    for name in CODE_FILE_PATHS:
        path = project_root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")
    _write_lfs_fonts(project_root)


def _prepare_release_artifacts(
    tmp_path: Path,
    settings: Settings,
    *,
    with_auth_snapshot: bool = True,
    with_release_dir: bool = True,
    with_manifest: bool = True,
    with_server_dir: bool = True,
    with_store: bool = True,
    dirty: bool = False,
) -> tuple[ReleaseInfo, Path]:
    release_info = ReleaseInfo(
        release_dir_name=RELEASE_DIR_NAME,
        commit_sha="9237d372",
        dirty=dirty,
        date="20260401",
        upstream_version=settings.sing_box_version,
        release_id="cafebabe1234" + "0" * 52,
        client_build_id="b" * 64,
    )
    write_release_info(tmp_path / settings.release_info_path, release_info)

    releases_root = tmp_path / settings.releases_root
    release_dir = releases_root / RELEASE_DIR_NAME
    if with_release_dir:
        release_dir.mkdir(parents=True)
        if with_manifest:
            (release_dir / "manifest.json").write_text("{}\n", encoding="utf-8")
        if with_server_dir:
            (release_dir / "server").mkdir()
    if with_store:
        (releases_root / "store" / "prefix").mkdir(parents=True)

    if with_auth_snapshot:
        auth_snapshot_path = tmp_path / settings.auth_snapshot_path
        auth_snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        auth_snapshot_path.write_text('{"users": []}\n', encoding="utf-8")

    return release_info, release_dir


def _runtime_inventory():
    from sing_box_manager.config_loader import RuntimeInventory

    return RuntimeInventory.model_validate(
        {
            "deployment": {
                "host": "vpn.example.com",
                "ip": "1.2.3.4",
                "trojan_port": 8443,
                "hysteria2_port": 4443,
                "naive_port": 9443,
                "tls": {
                    "enabled": True,
                    "self_signed_cert": False,
                    "key_path": (
                        "/etc/letsencrypt/live/vpn.example.com/privkey.pem"
                    ),
                    "certificate_path": (
                        "/etc/letsencrypt/live/vpn.example.com/fullchain.pem"
                    ),
                },
            },
            "web_portal": {
                "users": [
                    {
                        "username": "alice",
                        "password_hash": (
                            "$argon2id$v=19$m=65536,t=3,p=4$"
                            "c29tZXNhbHQxMjM0NTY$"
                            "c29tZWhhc2h2YWx1ZXNvbWVoaXN0b3J5"
                        ),
                    }
                ]
            },
            "trojan": {
                "users": [
                    {"username": "alice", "password": "x", "enabled": True}
                ]
            },
            "hysteria2": {
                "obfs_password": "x",
                "users": [
                    {"username": "alice", "password": "x", "enabled": True}
                ],
            },
            "naive": {
                "users": [
                    {"username": "alice", "password": "x", "enabled": True}
                ]
            },
            "vps_info": {
                "kiwi_veid": "test-veid",
                "kiwi_api_key": "test-api-key",
            },
        }
    )


def _deploy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    recorder: _Recorder | None = None,
    options: DeployOptions = DeployOptions(),
    protocols: list[str] | None = None,
    traffic_stats_enabled: bool = False,
    **artifact_kwargs: bool,
) -> _Recorder:
    config_path = tmp_path / "config" / "inventory" / "runtime.yaml"
    settings = _settings(
        config_path=config_path, traffic_stats_enabled=traffic_stats_enabled
    )
    _prepare_release_artifacts(tmp_path, settings, **artifact_kwargs)
    _prepare_code_tree(tmp_path)
    recorder = recorder or _Recorder()

    monkeypatch.setattr(artifacts, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr("sing_box_manager.deploy.subprocess.run", recorder)
    # Installing the managed units is part of every deploy now, and it runs
    # through the provisioning module's own subprocess handle.
    monkeypatch.setattr("sing_box_manager.provision.subprocess.run", recorder)
    monkeypatch.setattr(
        "sing_box_manager.deploy.sha256_for_path", lambda _path: LOCAL_LOCK_DIGEST
    )
    monkeypatch.setattr(
        "sing_box_manager.settings.load_runtime_inventory",
        lambda _path: _runtime_inventory(),
    )

    if options.bootstrap:
        monkeypatch.setattr(
            "sing_box_manager.provision.provision_remote",
            lambda **kwargs: None,
        )

    deploy_release(
        hostname=HOST,
        protocols=protocols,
        remote_root=REMOTE_ROOT,
        settings=settings,
        options=options,
    )
    return recorder


def test_delete_is_only_ever_scoped_inside_git_owned_trees(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The keystone safety property of the whole code-sync design."""
    recorder = _deploy(tmp_path, monkeypatch)

    allowed_delete_roots = {
        f"{HOST}:{REMOTE_ROOT}/{tree}/" for tree in CODE_TREE_PATHS
    } | {f"{HOST}:{REMOTE_ROOT}/releases/{RELEASE_DIR_NAME}/"}

    deleting = [
        command for command in recorder.rsync_commands if "--delete" in command
    ]
    assert deleting, "expected at least one --delete sync"
    for command in deleting:
        assert command[-1] in allowed_delete_roots, (
            f"--delete escaped the allowlist: {command[-1]}"
        )


def test_server_local_state_is_never_an_rsync_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _deploy(tmp_path, monkeypatch)

    for command in recorder.rsync_commands:
        destination = command[-1].split(":", 1)[1].rstrip("/")
        for protected in SERVER_LOCAL_PATHS:
            assert destination != protected, f"{protected} was a sync target"
            assert not destination.startswith(f"{protected}/"), (
                f"{destination} is inside {protected}"
            )
    # The repo root itself is never synced wholesale either.
    assert all(
        command[-1].rstrip("/") != f"{HOST}:{REMOTE_ROOT}"
        for command in recorder.rsync_commands
    )
    # config/generated/ is written, but only one named file at a time.
    generated_writes = [
        command
        for command in recorder.rsync_commands
        if f"{REMOTE_ROOT}/config/generated" in command[-1]
    ]
    assert generated_writes
    for command in generated_writes:
        assert "--delete" not in command
        assert command[-1].endswith(".json.tmp")


def test_deploy_stages_and_publishes_the_derived_runtime_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _deploy(tmp_path, monkeypatch)

    config_sync = next(
        command
        for command in recorder.rsync_commands
        if command[-1].endswith("/.sbm-runtime-config.deploy.tmp")
    )
    assert "--delete" not in config_sync

    publish = next(
        command
        for command in recorder.remote_commands
        if "sing_box_manager.remote_config" in command
    )
    assert f"{REMOTE_ROOT}/config/inventory/runtime.yaml" in publish


def test_the_shared_store_is_synced_without_delete_and_can_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """"Not in this release" and "unreferenced" are different questions."""
    recorder = _deploy(tmp_path, monkeypatch)

    store_syncs = [
        command
        for command in recorder.rsync_commands
        if command[-1] == f"{HOST}:{REMOTE_ROOT}/releases/store/"
    ]

    assert len(store_syncs) == 1
    assert "--delete" not in store_syncs[0]
    assert "--partial-dir=.rsync-partial" in store_syncs[0]
    # --ignore-existing would permanently skip a blob left partial by an
    # interrupted run, which is exactly the file that needs finishing.
    assert "--ignore-existing" not in store_syncs[0]
    # payload/ has to travel too: the server package's sing-box binary is a
    # relative symlink into it. upstream/ is a build input and stays home.
    assert "--exclude=upstream/" in store_syncs[0]


def test_the_release_directory_sync_preserves_the_permissions_it_was_built_with(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rendered configs are 0600 on disk; -a is what keeps them that way."""
    recorder = _deploy(tmp_path, monkeypatch)

    release_syncs = [
        command
        for command in recorder.rsync_commands
        if command[-1] == f"{HOST}:{REMOTE_ROOT}/releases/{RELEASE_DIR_NAME}/"
    ]

    assert len(release_syncs) == 1
    assert "-a" in release_syncs[0]
    assert "--delete" in release_syncs[0]
    # Already-compressed payload; -z would only burn CPU on both ends.
    assert "-z" not in release_syncs[0]


def test_code_ships_before_the_artifacts_that_depend_on_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _deploy(tmp_path, monkeypatch)

    last_code_sync = max(
        recorder.index_of(f"{HOST}:{REMOTE_ROOT}/{tree}/") for tree in CODE_TREE_PATHS
    )
    assert last_code_sync < recorder.index_of(
        f"{HOST}:{REMOTE_ROOT}/releases/store/"
    )
    assert recorder.index_of(
        f"{HOST}:{REMOTE_ROOT}/releases/store/"
    ) < recorder.index_of("server-install.sh")


def test_an_unchanged_lock_skips_the_remote_environment_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Solving an environment is minutes on a weak CPU; skipping it is the win."""
    recorder = _deploy(tmp_path, monkeypatch)

    assert not any(
        "pixi install" in command for command in recorder.remote_commands
    )


def test_a_changed_lock_installs_and_then_rechecks_the_rsync_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`pixi install` can replace the very binary the artifact sync uses."""
    recorder = _deploy(
        tmp_path,
        monkeypatch,
        recorder=_Recorder(remote_lock_digest="b" * 64),
    )

    install_index = recorder.index_of("pixi install --locked")
    version_checks = [
        index
        for index, command in enumerate(recorder.commands)
        if command == ["rsync", "--version"]
    ]

    assert any(index > install_index for index in version_checks), (
        "rsync versions were not re-checked after pixi install"
    )
    assert any(
        index > install_index
        and any(f"test -x {shlex.quote(REMOTE_RSYNC_PATH)}" in part for part in command)
        for index, command in enumerate(recorder.commands)
    ), "the managed rsync was not re-resolved after pixi install"
    # And the artifact transfers happen after all of that.
    assert install_index < recorder.index_of(
        f"{HOST}:{REMOTE_ROOT}/releases/store/"
    )


def test_bootstrap_pushes_code_with_the_remote_system_rsync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh host has no .pixi yet, so there is no managed rsync to point at."""
    recorder = _deploy(
        tmp_path, monkeypatch, options=DeployOptions(bootstrap=True)
    )

    code_syncs = [
        command
        for command in recorder.rsync_commands
        if command[-1].startswith(f"{HOST}:{REMOTE_ROOT}/sing_box_manager")
    ]
    assert code_syncs
    assert all("--rsync-path" not in command for command in code_syncs)

    # After `pixi install`, the artifact transfers do use the managed binary.
    store_sync = next(
        command
        for command in recorder.rsync_commands
        if command[-1] == f"{HOST}:{REMOTE_ROOT}/releases/store/"
    )
    assert store_sync[store_sync.index("--rsync-path") + 1] == REMOTE_RSYNC_PATH
    assert any("pixi install" in command for command in recorder.remote_commands)


def test_bootstrap_does_not_require_a_pixi_environment_that_cannot_exist_yet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _deploy(
        tmp_path, monkeypatch, options=DeployOptions(bootstrap=True)
    )

    preflight = recorder.remote_commands[0]
    assert "command -v rsync" in preflight
    assert "command -v pixi" in preflight
    assert REMOTE_RSYNC_PATH not in preflight
    assert "pixi.toml" not in preflight


def test_the_preflight_no_longer_demands_a_git_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The remote is a plain directory now; the deploy key can be revoked."""
    recorder = _deploy(tmp_path, monkeypatch)

    assert not any("git" in command for command in recorder.remote_commands)
    assert f"test -f {shlex.quote(f'{REMOTE_ROOT}/pixi.toml')}" in (
        recorder.remote_commands[0]
    )


def test_lfs_pointer_fonts_fail_before_any_remote_contact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    _prepare_release_artifacts(tmp_path, settings)
    _prepare_code_tree(tmp_path)
    fonts_dir = tmp_path / "sing_box_manager" / "web" / "static" / "fonts"
    (fonts_dir / "body.ttf").write_bytes(
        b"version https://git-lfs.github.com/spec/v1\noid sha256:abc\nsize 21000000\n"
    )
    recorder = _Recorder()
    monkeypatch.setattr(artifacts, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr("sing_box_manager.deploy.subprocess.run", recorder)

    with pytest.raises(DeploymentError, match="Git LFS pointer"):
        deploy_release(
            hostname=HOST,
            protocols=None,
            remote_root=REMOTE_ROOT,
            settings=settings,
        )

    assert recorder.commands == []


def test_a_remote_manifest_version_mismatch_stops_the_deploy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only reachable with --skip-code, which is exactly when it matters."""
    settings = _settings()
    _prepare_release_artifacts(tmp_path, settings)
    _prepare_code_tree(tmp_path)
    recorder = _Recorder(remote_manifest_version=str(MANIFEST_VERSION + 1))
    monkeypatch.setattr(artifacts, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr("sing_box_manager.deploy.subprocess.run", recorder)

    with pytest.raises(DeploymentError, match="manifest version"):
        deploy_release(
            hostname=HOST,
            protocols=None,
            remote_root=REMOTE_ROOT,
            settings=settings,
            options=DeployOptions(skip_code=True),
        )

    assert not recorder.rsync_commands


def test_skip_code_leaves_the_remote_code_tree_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _deploy(
        tmp_path, monkeypatch, options=DeployOptions(skip_code=True)
    )

    assert all(
        f"{REMOTE_ROOT}/sing_box_manager" not in command[-1]
        for command in recorder.rsync_commands
    )
    assert any(
        REMOTE_PYTHON_PATH in command for command in recorder.remote_commands
    ), "the manifest compatibility check is what makes --skip-code safe"


def test_a_dry_run_transfers_and_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _deploy(tmp_path, monkeypatch, options=DeployOptions(dry_run=True))

    assert recorder.rsync_commands
    for command in recorder.rsync_commands:
        assert "--dry-run" in command
        assert "--stats" in command

    mutating = ("server-install.sh", "mv -f", "chown", "systemctl restart")
    for remote_command in recorder.remote_commands:
        assert not any(fragment in remote_command for fragment in mutating), (
            f"dry run issued a mutating command: {remote_command}"
        )


def test_a_version_file_records_provenance_for_a_host_without_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    uploaded: dict[str, str] = {}
    original_run = subprocess.run

    recorder = _Recorder()

    def capture(command: list[str], **kwargs: object):
        if command[0] == "rsync" and command[-1].endswith("VERSION.tmp"):
            uploaded["contents"] = Path(command[-2]).read_text(encoding="utf-8")
        return recorder(command, **kwargs)  # type: ignore[arg-type]

    assert original_run is subprocess.run
    settings = _settings()
    _prepare_release_artifacts(tmp_path, settings)
    _prepare_code_tree(tmp_path)
    monkeypatch.setattr(artifacts, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr("sing_box_manager.deploy.subprocess.run", capture)
    monkeypatch.setattr(
        "sing_box_manager.deploy.sha256_for_path", lambda _path: LOCAL_LOCK_DIGEST
    )

    deploy_release(
        hostname=HOST,
        protocols=None,
        remote_root=REMOTE_ROOT,
        settings=settings,
    )

    assert "commit_sha=9237d372" in uploaded["contents"]
    assert "dirty=false" in uploaded["contents"]
    assert f"release_dir={RELEASE_DIR_NAME}" in uploaded["contents"]
    assert f"client_build_id={'b' * 64}" in uploaded["contents"]
    # Published atomically alongside the other metadata, not before it.
    publish = next(
        command for command in recorder.remote_commands if command.startswith("mv -f")
    )
    assert f"{REMOTE_ROOT}/VERSION" in publish


def test_successful_deploy_publishes_timestamp_after_web_health_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    uploaded: dict[str, artifacts.DeploymentInfo] = {}
    recorder = _Recorder()

    def capture(command: list[str], **kwargs: object):
        if command[0] == "rsync" and command[-1].endswith(
            "deployment-info.json.tmp"
        ):
            uploaded["info"] = artifacts.read_deployment_info(Path(command[-2]))
        return recorder(command, **kwargs)  # type: ignore[arg-type]

    settings = _settings()
    _prepare_release_artifacts(tmp_path, settings)
    _prepare_code_tree(tmp_path)
    monkeypatch.setattr(artifacts, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr("sing_box_manager.deploy.subprocess.run", capture)
    monkeypatch.setattr(
        "sing_box_manager.deploy.sha256_for_path", lambda _path: LOCAL_LOCK_DIGEST
    )

    deploy_release(
        hostname=HOST,
        protocols=None,
        remote_root=REMOTE_ROOT,
        settings=settings,
    )

    info = uploaded["info"]
    assert info.release_id == "cafebabe1234" + "0" * 52
    assert info.commit_sha == "9237d372"
    assert info.client_build_id == "b" * 64
    assert info.deployed_at.endswith("Z")
    assert recorder.index_of("systemctl is-active") < recorder.index_of(
        "deployment-info.json.tmp"
    )
    publish = next(
        command
        for command in recorder.remote_commands
        if "mv -f" in command and "deployment-info.json.tmp" in command
    )
    assert publish.index("chown root:sbm") < publish.index("mv -f")
    assert "chmod 0640" in publish


def test_the_tree_is_locked_down_before_root_runs_anything_from_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A compromised portal must not be able to plant code that root runs."""
    recorder = _deploy(tmp_path, monkeypatch)

    lockdown = recorder.index_of(f"chown -R root:sbm {shlex.quote(REMOTE_ROOT)}")
    command = recorder.commands[lockdown][-1]
    assert "chmod -R u+rwX,g+rX,g-w,o-rwx" in command
    assert "__pycache__" in command
    assert lockdown < recorder.index_of("server-install.sh")
    assert lockdown < recorder.index_of("sing_box_manager.remote_config")
    assert not any(
        "chown" in command and "sbm:" in command
        for command in recorder.remote_commands
    )


def test_every_deploy_redacts_tokens_from_the_nginx_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Subscription links carry their credential in the path nginx logs."""
    recorder = _deploy(tmp_path, monkeypatch)

    redaction = recorder.index_of("sbm_redacted")
    assert redaction > recorder.index_of("sing_box_manager.remote_config")
    assert redaction < recorder.index_of("systemctl restart sing-box-manager-web")


def test_the_server_package_is_installed_from_a_directory_not_a_tarball(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Extraction was real CPU on a weak VPS, and rsync can skip an unchanged tree."""
    recorder = _deploy(tmp_path, monkeypatch, protocols=["trojan"])

    assert not any("tar -xf" in command for command in recorder.remote_commands)
    install = next(
        command for command in recorder.remote_commands if "server-install.sh" in command
    )
    assert install == (
        f"cd {shlex.quote(f'{REMOTE_ROOT}/releases/{RELEASE_DIR_NAME}/server')} && "
        "./server-install.sh -p trojan"
    )


@pytest.mark.parametrize("protocols", [None, []])
def test_default_deploy_leaves_available_inbound_selection_to_the_installer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    protocols: list[str] | None,
) -> None:
    recorder = _deploy(tmp_path, monkeypatch, protocols=protocols)
    install = next(
        command
        for command in recorder.remote_commands
        if "server-install.sh" in command
    )
    assert install == (
        f"cd {shlex.quote(f'{REMOTE_ROOT}/releases/{RELEASE_DIR_NAME}/server')} && "
        "./server-install.sh"
    )


def test_managed_units_are_rewritten_on_an_ordinary_deploy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unit files are generated from the code, so a deploy has to ship them.

    They used to be written only under `--bootstrap`, which pinned every host
    to the units it was provisioned with: a release that added a directive --
    the traffic database's `StateDirectory`, say -- deployed clean and then
    failed at runtime against the unit from months earlier.
    """
    recorder = _deploy(tmp_path, monkeypatch, traffic_stats_enabled=True)

    written = [command for command in recorder.remote_commands if "tee" in command]
    for unit in (
        "sing-box-manager-web.service",
        "sing-box-manager-traffic-stats.service",
        "sing-box-manager-traffic-stats.timer",
    ):
        assert any(f"/etc/systemd/system/{unit}" in command for command in written)
    assert any(
        "systemctl daemon-reload" in command for command in recorder.remote_commands
    )
    contents = b"".join(recorder.inputs).decode("utf-8")
    assert "StateDirectory=sing-box-manager" in contents


def test_the_collector_runs_once_before_a_deploy_is_called_healthy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing else in a deploy exercises the timer-driven oneshot."""
    recorder = _deploy(tmp_path, monkeypatch, traffic_stats_enabled=True)

    start = recorder.index_of(
        "systemctl start sing-box-manager-traffic-stats.service"
    )
    assert start < recorder.index_of("deployment-info.json.tmp")


def test_the_collector_is_left_alone_when_traffic_stats_are_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _deploy(tmp_path, monkeypatch)

    assert not any(
        "traffic-stats" in command for command in recorder.remote_commands
    )


def test_a_missing_web_unit_is_not_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _deploy(tmp_path, monkeypatch, recorder=_Recorder(web_unit_exists=False))

    assert not any(
        "systemctl restart" in command for command in recorder.remote_commands
    )


@pytest.mark.parametrize(
    ("artifact_kwargs", "expected"),
    [
        ({"with_auth_snapshot": False}, "auth snapshot"),
        ({"with_release_dir": False}, "Release directory not found"),
        ({"with_manifest": False}, "Release manifest not found"),
        ({"with_server_dir": False}, "Server package not found"),
        ({"with_store": False}, "Payload store not found"),
    ],
)
def test_deploy_requires_a_complete_local_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    artifact_kwargs: dict[str, bool],
    expected: str,
) -> None:
    settings = _settings()
    _prepare_release_artifacts(tmp_path, settings, **artifact_kwargs)
    _prepare_code_tree(tmp_path)
    monkeypatch.setattr(artifacts, "PROJECT_ROOT", tmp_path)

    with pytest.raises(DeploymentError, match=expected):
        deploy_release(
            hostname=HOST,
            protocols=None,
            remote_root=REMOTE_ROOT,
            settings=settings,
        )


def test_deploy_requires_release_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(artifacts, "PROJECT_ROOT", tmp_path)

    with pytest.raises(DeploymentError, match="Release metadata not found"):
        deploy_release(
            hostname=HOST,
            protocols=None,
            remote_root=REMOTE_ROOT,
            settings=_settings(),
        )


def test_deploy_rejects_unknown_protocol(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(artifacts, "PROJECT_ROOT", tmp_path)

    with pytest.raises(DeploymentError, match="Unsupported protocol"):
        deploy_release(
            hostname=HOST,
            protocols=["wireguard"],
            remote_root=REMOTE_ROOT,
            settings=_settings(),
        )


def test_deploy_reports_an_rsync_version_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    _prepare_release_artifacts(tmp_path, settings)
    _prepare_code_tree(tmp_path)

    def fake_run(
        command: list[str],
        *,
        check: bool,
        capture_output: bool = False,
        text: bool = False,
        stderr: int | None = None,
    ) -> subprocess.CompletedProcess[str]:
        stdout = ""
        if command == ["rsync", "--version"]:
            stdout = RSYNC_VERSION_LINE
        elif command[0] == "ssh" and command[-1].endswith("--version"):
            stdout = "rsync  version 3.2.7  protocol version 31\n"
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(artifacts, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr("sing_box_manager.deploy.subprocess.run", fake_run)

    with pytest.raises(DeploymentError, match="does not match remote rsync"):
        deploy_release(
            hostname=HOST,
            protocols=None,
            remote_root=REMOTE_ROOT,
            settings=settings,
        )


def test_deploy_reports_a_missing_local_rsync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    _prepare_release_artifacts(tmp_path, settings)
    _prepare_code_tree(tmp_path)

    def fake_run(command: list[str], **kwargs: object):
        if command == ["rsync", "--version"]:
            raise FileNotFoundError(command[0])
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(artifacts, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr("sing_box_manager.deploy.subprocess.run", fake_run)

    with pytest.raises(DeploymentError, match="Required command not found: rsync"):
        deploy_release(
            hostname=HOST,
            protocols=None,
            remote_root=REMOTE_ROOT,
            settings=settings,
        )


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("  1,234  50%  1.00MB/s", 50),
        ("no percentage here", None),
        ("  9,999 120%", 100),
    ],
)
def test_parse_rsync_progress_percent(output: str, expected: int | None) -> None:
    assert _parse_rsync_progress_percent(output) == expected


def _remote() -> _Remote:
    return _Remote(
        hostname=HOST,
        root=REMOTE_ROOT,
        releases_root=f"{REMOTE_ROOT}/releases",
        release_dir=f"{REMOTE_ROOT}/releases/{RELEASE_DIR_NAME}",
        store_dir=f"{REMOTE_ROOT}/releases/store",
        server_dir=f"{REMOTE_ROOT}/releases/{RELEASE_DIR_NAME}/server",
        generated_dir=f"{REMOTE_ROOT}/config/generated",
        auth_snapshot=f"{REMOTE_ROOT}/config/generated/auth-users.json",
        release_info=f"{REMOTE_ROOT}/config/generated/release-info.json",
        deployment_info=f"{REMOTE_ROOT}/config/generated/deployment-info.json",
        version_file=f"{REMOTE_ROOT}/VERSION",
        rsync_path=REMOTE_RSYNC_PATH,
    )


def _transfer(source: Path) -> _Transfer:
    return _Transfer(
        source=source,
        destination=f"{HOST}:{REMOTE_ROOT}/releases/{RELEASE_DIR_NAME}",
        options=("-a", "-s", "--delete"),
        error_message="sync failed",
    )


def test_rsync_directory_keeps_a_plain_command_without_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands: list[list[str]] = []

    def fake_run(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        assert kwargs["check"] is True
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("sing_box_manager.deploy.subprocess.run", fake_run)

    _rsync_directory(_transfer(tmp_path), _remote(), DeployOptions())

    assert commands == [
        [
            "rsync",
            "-a",
            "-s",
            "--delete",
            "-e",
            (
                "ssh -T -o RequestTTY=no -o ConnectTimeout=15 "
                "-o ServerAliveInterval=15 -o ServerAliveCountMax=4"
            ),
            "--rsync-path",
            REMOTE_RSYNC_PATH,
            f"{tmp_path}/",
            f"{HOST}:{REMOTE_ROOT}/releases/{RELEASE_DIR_NAME}/",
        ]
    ]


def test_rsync_directory_uses_progress2_for_an_interactive_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands: list[list[str]] = []

    class _FakeProcess:
        def __init__(self, command: list[str]) -> None:
            commands.append(command)
            self.stdout = StringIO("  1,000  42%  1.00MB/s\r")

        def wait(self) -> int:
            return 0

    monkeypatch.setattr(
        "sing_box_manager.deploy.subprocess.Popen",
        lambda command, **kwargs: _FakeProcess(command),
    )

    _rsync_directory(
        _transfer(tmp_path), _remote(), DeployOptions(show_progress=True)
    )

    assert "--info=progress2" in commands[0]


def test_rsync_directory_progress_failure_raises_deployment_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _FailingProcess:
        def __init__(self, command: list[str]) -> None:
            self.stdout = StringIO("")

        def wait(self) -> int:
            return 23

    monkeypatch.setattr(
        "sing_box_manager.deploy.subprocess.Popen",
        lambda command, **kwargs: _FailingProcess(command),
    )

    with pytest.raises(DeploymentError, match="sync failed"):
        _rsync_directory(
            _transfer(tmp_path), _remote(), DeployOptions(show_progress=True)
        )


def _capture_deploy_call(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    captured: dict[str, object] = {}

    def fake_deploy_release(**kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(
        "sing_box_manager.deploy.deploy_release", fake_deploy_release
    )
    monkeypatch.setattr(
        "sing_box_manager.settings.load_settings", lambda _path: _settings()
    )
    monkeypatch.setenv("SBM_CONFIG", "config/inventory/runtime.yaml")
    return captured


def test_cli_deploy_uses_default_protocols_and_remote_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _capture_deploy_call(monkeypatch)

    main(["deploy", "--hostname", HOST])

    assert captured["hostname"] == HOST
    assert captured["protocols"] is None
    assert captured["remote_root"] == REMOTE_ROOT
    assert captured["options"] == DeployOptions(
        show_progress=captured["options"].show_progress  # type: ignore[union-attr]
    )


def test_cli_deploy_accepts_protocol_and_remote_root_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _capture_deploy_call(monkeypatch)

    main(
        [
            "deploy",
            "--hostname",
            HOST,
            "--remote-root",
            "/srv/sing-box-manager",
            "--protocol",
            "hysteria2",
            "naive",
        ]
    )

    assert captured["remote_root"] == "/srv/sing-box-manager"
    assert captured["protocols"] == ["hysteria2", "naive"]


def test_cli_deploy_forwards_the_deployment_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _capture_deploy_call(monkeypatch)

    main(["deploy", "--hostname", HOST, "--bootstrap", "--skip-code", "--dry-run"])

    options = captured["options"]
    assert isinstance(options, DeployOptions)
    assert options.bootstrap
    assert options.skip_code
    assert options.dry_run


def test_cli_deploy_forwards_certbot_email(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _capture_deploy_call(monkeypatch)

    main([
        "deploy", "--hostname", HOST,
        "--bootstrap", "--certbot-email", "admin@example.com",
    ])

    options = captured["options"]
    assert isinstance(options, DeployOptions)
    assert options.certbot_email == "admin@example.com"


def test_cli_deploy_certbot_email_defaults_to_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _capture_deploy_call(monkeypatch)

    main(["deploy", "--hostname", HOST])

    options = captured["options"]
    assert isinstance(options, DeployOptions)
    assert options.certbot_email is None


def test_cli_deploy_prints_deployment_errors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake_deploy_release(**kwargs: object) -> None:
        raise DeploymentError("boom")

    monkeypatch.setattr(
        "sing_box_manager.deploy.deploy_release", fake_deploy_release
    )
    monkeypatch.setattr(
        "sing_box_manager.settings.load_settings", lambda _path: _settings()
    )
    monkeypatch.setenv("SBM_CONFIG", "config/inventory/runtime.yaml")

    with pytest.raises(SystemExit) as exit_info:
        main(["deploy", "--hostname", HOST])

    assert exit_info.value.code == 1
    assert "boom" in capsys.readouterr().err


def test_deploy_ships_the_files_the_portal_reads_at_runtime() -> None:
    """The portal renders subscriptions on the server, which needs these."""
    from sing_box_manager.release.renderers import AI_SERVICE_RULES_PATH

    root = Path(__file__).resolve().parents[1]
    relative = AI_SERVICE_RULES_PATH.relative_to(root).as_posix()
    assert any(relative.startswith(f"{tree}/") for tree in CODE_TREE_PATHS)
