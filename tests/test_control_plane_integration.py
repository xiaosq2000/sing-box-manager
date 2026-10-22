"""Real PostgreSQL and HTTP checks, isolated from machine services and inventory."""

import http.cookiejar
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import psycopg
import pytest
import yaml

from sing_box_manager.control_plane.database import provision_sql
from sing_box_manager.control_plane.deployment import wrapper_script

ROOT = Path(__file__).parents[1]


@pytest.fixture(scope="module")
def postgres(tmp_path_factory):
    tools = {
        name: shutil.which(name)
        for name in ("initdb", "pg_ctl", "psql", "pg_dump", "pg_restore")
    }
    if not all(tools.values()) or os.geteuid() == 0:
        reason = "PostgreSQL test tools and a non-root account are required"
        if os.environ.get("SBM_REQUIRE_POSTGRES") == "1":
            pytest.fail(reason)
        pytest.skip(reason)
    root = tmp_path_factory.mktemp("postgres")
    data = root / "data"
    sockets = root / "sockets"
    sockets.mkdir(mode=0o700)
    # Keep subprocess environments free of operator credentials and DB targets.
    env = {
        key: os.environ[key]
        for key in ("PATH", "HOME", "LANG", "LC_ALL", "LD_LIBRARY_PATH", "PYTHONPATH")
        if key in os.environ
    }
    subprocess.run(
        [
            tools["initdb"],
            "-D",
            str(data),
            "--username=postgres",
            "--auth-local=trust",
            "--auth-host=reject",
            "--no-locale",
            "--encoding=UTF8",
        ],
        check=True,
        capture_output=True,
        env=env,
    )
    subprocess.run(
        [
            tools["pg_ctl"],
            "-D",
            str(data),
            "-l",
            str(root / "postgres.log"),
            "-w",
            "-o",
            f"-k {sockets} -h '' -p 5432",
            "start",
        ],
        check=True,
        capture_output=True,
        env=env,
    )
    try:
        yield {"root": root, "sockets": sockets, "tools": tools, "env": env}
    finally:
        subprocess.run(
            [tools["pg_ctl"], "-D", str(data), "-m", "immediate", "-w", "stop"],
            check=True,
            capture_output=True,
            env=env,
        )


def psql(pg, database, content):
    return subprocess.run(
        [
            pg["tools"]["psql"],
            "-X",
            "-v",
            "ON_ERROR_STOP=1",
            "-h",
            str(pg["sockets"]),
            "-U",
            "postgres",
            "-d",
            database,
        ],
        input=content,
        text=True,
        capture_output=True,
        env=pg["env"],
    )


def config(pg, tmp_path, database):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    path = tmp_path / "runtime.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "sing_box_version": "1.14.2",
                "config_root": str(tmp_path / "config"),
                "control_plane": {
                    "enabled": True,
                    "port": port,
                    "secret_key": "synthetic-fixture-key-not-for-production-"
                    + "0" * 32,
                    "database_socket_dir": str(pg["sockets"]),
                    "database_name": database,
                },
            }
        )
    )
    path.chmod(0o600)
    env = {
        **pg["env"],
        "SBM_CONFIG": str(path),
        "DJANGO_SETTINGS_MODULE": "sing_box_manager.control_plane.django_settings",
    }
    return path, port, env


def cli(path, env, action, *, legacy=False):
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "sing_box_manager",
            "--config",
            str(path),
            "control-plane",
            *(["--action"] if legacy else []),
            action,
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def python(env, content):
    result = subprocess.run(
        [sys.executable, "-"],
        input=content,
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_schema_repeat_migration_and_backup_restore(postgres, tmp_path):
    pg = postgres
    result = psql(pg, "postgres", provision_sql("sbm_fixture"))
    assert result.returncode == 0, result.stderr
    path, _, env = config(pg, tmp_path, "sbm_fixture")
    before = python(
        env,
        "import django; django.setup(); from django.test import Client; print(Client(HTTP_HOST='127.0.0.1').get('/health/').status_code)",
    )
    assert before.strip() == "503"
    for action in ("check", "migrate", "collect-static"):
        result = cli(path, env, action)
        assert result.returncode == 0, result.stderr
    output = python(
        env,
        """
import django
django.setup()
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import Client
User = get_user_model()
operator = User.objects.create_superuser('fixture_operator', 'operator@example.com', 'synthetic-fixture-pass-123')
assert operator.password.startswith('argon2$')
try:
    with transaction.atomic():
        User.objects.create_user('fixture_operator')
except IntegrityError:
    pass
else:
    raise AssertionError('database did not enforce uniqueness')
client = Client(HTTP_HOST='127.0.0.1', enforce_csrf_checks=True)
assert client.get('/admin/').status_code == 302
assert client.post('/admin/login/', {'username': 'fixture_operator', 'password': 'synthetic-fixture-pass-123'}).status_code == 403
assert client.login(username='fixture_operator', password='synthetic-fixture-pass-123')
assert client.get('/admin/').status_code == 200
assert client.get('/health/').json() == {'status': 'ok'}
assert client.get('/health/', HTTP_HOST='attacker.example.com').status_code == 400
ordinary = User.objects.create_user('fixture_nonstaff')
client.force_login(ordinary)
assert client.get('/admin/').status_code == 302
from sing_box_manager.control_plane.models import Account, Credential
proxy = Account.objects.create(username='fixture_proxy', enabled=False, portal_enabled=True, subscription_counter=4)
Credential.objects.create(account=proxy, protocol='trojan', secret='fixture-backup-secret')
print('ok')
""",
    )
    assert output.strip() == "ok"
    assert psql(pg, "postgres", provision_sql("sbm_fixture")).returncode == 0
    assert cli(path, env, "migrate", legacy=True).returncode == 0
    migration_check = subprocess.run(
        [sys.executable, "-m", "django", "makemigrations", "--check", "--dry-run"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert migration_check.returncode == 0, migration_check.stderr
    with psycopg.connect(
        host=str(pg["sockets"]), user="sbm", dbname="sbm_fixture"
    ) as connection:
        assert (
            connection.execute("SELECT count(*) FROM control_plane_user").fetchone()[0]
            == 2
        )
        privileges = connection.execute(
            "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls FROM pg_roles WHERE rolname = current_user"
        ).fetchone()
        assert privileges == (False,) * 5
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute("CREATE ROLE forbidden_role")
    dump = tmp_path / "backup.dump"
    subprocess.run(
        [
            pg["tools"]["pg_dump"],
            "-h",
            str(pg["sockets"]),
            "-U",
            "sbm",
            "-d",
            "sbm_fixture",
            "-Fc",
            "-f",
            str(dump),
        ],
        check=True,
        capture_output=True,
        env=pg["env"],
    )
    dump.chmod(0o600)
    assert (
        psql(
            pg, "postgres", "CREATE DATABASE sbm_fixture_restore OWNER sbm;"
        ).returncode
        == 0
    )
    subprocess.run(
        [
            pg["tools"]["pg_restore"],
            "-h",
            str(pg["sockets"]),
            "-U",
            "postgres",
            "-d",
            "sbm_fixture_restore",
            "--role=sbm",
            "--no-owner",
            str(dump),
        ],
        check=True,
        capture_output=True,
        env=pg["env"],
    )
    with psycopg.connect(
        host=str(pg["sockets"]), user="sbm", dbname="sbm_fixture_restore"
    ) as connection:
        assert (
            connection.execute("SELECT count(*) FROM control_plane_user").fetchone()[0]
            == 2
        )
        assert connection.execute(
            "SELECT enabled, portal_enabled, subscription_counter FROM control_plane_account"
        ).fetchone() == (False, True, 4)
        assert connection.execute(
            "SELECT protocol, secret, enabled FROM control_plane_credential"
        ).fetchone() == ("trojan", "fixture-backup-secret", True)
    disabled = yaml.safe_load(path.read_text())
    disabled["control_plane"]["enabled"] = False
    path.write_text(yaml.safe_dump(disabled))
    assert cli(path, env, "migrate").returncode == 1
    with psycopg.connect(
        host=str(pg["sockets"]), user="sbm", dbname="sbm_fixture"
    ) as connection:
        assert (
            connection.execute("SELECT count(*) FROM control_plane_user").fetchone()[0]
            == 2
        )


def test_real_http_service_and_collected_admin_assets(postgres, tmp_path):
    pg = postgres
    result = psql(pg, "postgres", provision_sql("sbm_http_fixture"))
    assert result.returncode == 0, result.stderr
    path, port, env = config(pg, tmp_path, "sbm_http_fixture")
    for action in ("migrate", "collect-static"):
        result = cli(path, env, action)
        assert result.returncode == 0, result.stderr
    python(
        env,
        "import django; django.setup(); from django.contrib.auth import get_user_model; get_user_model().objects.create_superuser('http_operator', 'operator@example.com', 'synthetic-fixture-pass-123')",
    )
    log = tmp_path / "service.log"
    with log.open("w") as output:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "sing_box_manager",
                "--config",
                str(path),
                "control-plane",
                "serve",
            ],
            cwd=ROOT,
            env=env,
            stdout=output,
            stderr=output,
            start_new_session=True,
        )
        try:
            result = cli(path, env, "health")
            assert result.returncode == 0, log.read_text()
            cookies = http.cookiejar.CookieJar()
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}),
                urllib.request.HTTPCookieProcessor(cookies),
            )
            with opener.open(f"http://127.0.0.1:{port}/admin/") as response:
                assert response.status == 200
                page = response.read().decode()
                assert "Service administration" in page
                # Browser form POSTs need a same-origin Origin header. With
                # no-referrer, browsers can send Origin: null, which must fail.
                assert response.headers["Referrer-Policy"] == "same-origin"
            asset = re.search(
                r'href="(/static/admin/css/base[^\"]+\.css)"', page
            ).group(1)
            with opener.open(f"http://127.0.0.1:{port}{asset}") as response:
                assert response.status == 200
                assert "text/css" in response.headers["Content-Type"]
                assert response.read()
            csrf = next(
                cookie.value for cookie in cookies if cookie.name == "sbm_control_csrf"
            )
            form_fields = {
                "username": "http_operator",
                "password": "synthetic-fixture-pass-123",
                "csrfmiddlewaretoken": csrf,
                "next": "/admin/",
            }
            form = urllib.parse.urlencode(form_fields).encode()
            origin = f"http://127.0.0.1:{port}"
            login_url = origin + "/admin/login/"
            for rejected_origin in (
                "null",
                "http://attacker.example.com",
                f"http://127.0.0.1:{port + 1}",
                f"http://localhost:{port}",
            ):
                request = urllib.request.Request(
                    login_url, data=form, headers={"Origin": rejected_origin}
                )
                with pytest.raises(urllib.error.HTTPError) as error:
                    opener.open(request)
                assert error.value.code == 403
            invalid_token = csrf[:-1] + ("a" if csrf[-1] != "a" else "b")
            for invalid_fields in (
                {
                    key: value
                    for key, value in form_fields.items()
                    if key != "csrfmiddlewaretoken"
                },
                {**form_fields, "csrfmiddlewaretoken": invalid_token},
            ):
                request = urllib.request.Request(
                    login_url,
                    data=urllib.parse.urlencode(invalid_fields).encode(),
                    headers={"Origin": origin},
                )
                with pytest.raises(urllib.error.HTTPError) as error:
                    opener.open(request)
                assert error.value.code == 403
            request = urllib.request.Request(
                login_url, data=form, headers={"Origin": origin}
            )
            with opener.open(request) as response:
                assert response.geturl().endswith("/admin/")
                assert "Site administration" in response.read().decode()
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/health/",
                headers={"Host": "attacker.example.com"},
            )
            with pytest.raises(urllib.error.HTTPError) as error:
                opener.open(request)
            assert error.value.code == 400
            assert cli(path, env, "create-admin", legacy=True).returncode == 1
            assert cli(path, env, "createsuperuser").returncode == 1
            with psycopg.connect(
                host=str(pg["sockets"]), user="sbm", dbname="sbm_http_fixture"
            ) as connection:
                connection.execute(
                    "DELETE FROM django_migrations WHERE app = 'control_plane'"
                )
            with pytest.raises(urllib.error.HTTPError) as error:
                opener.open(f"http://127.0.0.1:{port}/health/")
            assert error.value.code == 503
            assert "secret_key" not in error.value.read().decode()
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=10)


def test_deployed_wrapper_runs_real_cli_and_migrations(postgres, tmp_path):
    pg = postgres
    assert psql(pg, "postgres", provision_sql("sbm_wrapper_fixture")).returncode == 0
    path, _, _ = config(pg, tmp_path, "sbm_wrapper_fixture")
    root = tmp_path / "deployed manager"
    interpreter = root / ".pixi/envs/default/bin/python"
    interpreter.parent.mkdir(parents=True)
    interpreter.symlink_to(sys.executable)
    (root / "sing_box_manager").symlink_to(
        ROOT / "sing_box_manager", target_is_directory=True
    )
    runtime = root / "config/inventory/runtime.yaml"
    runtime.parent.mkdir(parents=True)
    shutil.copyfile(path, runtime)
    wrapper = tmp_path / "sbm"
    wrapper.write_text(wrapper_script(str(root)))
    wrapper.chmod(0o755)

    def invoke(args, env=pg["env"]):
        return subprocess.run(
            [str(wrapper), *args],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    for args in (
        ["control-plane", "migrate"],
        ["control-plane", "--action", "migrate"],
    ):
        result = invoke(args)
        assert result.returncode == 0, result.stderr
    disabled = yaml.safe_load(path.read_text())
    disabled["control_plane"]["enabled"] = False
    path.write_text(yaml.safe_dump(disabled))
    override_env = {**pg["env"], "SBM_CONFIG": str(path)}
    assert invoke(["control-plane", "migrate"], override_env).returncode == 1
    result = invoke(["--config", str(runtime), "control-plane", "check"], override_env)
    assert result.returncode == 0, result.stderr
    with psycopg.connect(
        host=str(pg["sockets"]), user="sbm", dbname="sbm_wrapper_fixture"
    ) as connection:
        assert (
            connection.execute("SELECT count(*) FROM django_migrations").fetchone()[0]
            > 0
        )


def test_provisioning_refuses_other_owners_and_privileged_roles(postgres):
    pg = postgres
    assert (
        psql(
            pg,
            "postgres",
            "CREATE DATABASE sbm_other_owner;\n\\connect sbm_other_owner\nCREATE TABLE sentinel (id integer); INSERT INTO sentinel VALUES (7);",
        ).returncode
        == 0
    )
    result = psql(pg, "postgres", provision_sql("sbm_other_owner"))
    assert result.returncode != 0
    assert "owned by another role" in result.stderr
    with psycopg.connect(
        host=str(pg["sockets"]), user="postgres", dbname="sbm_other_owner"
    ) as connection:
        assert connection.execute("SELECT id FROM sentinel").fetchone() == (7,)
    with psycopg.connect(
        host=str(pg["sockets"]), user="postgres", dbname="postgres", autocommit=True
    ) as connection:
        connection.execute("ALTER ROLE sbm CREATEDB")
        result = psql(pg, "postgres", provision_sql("sbm_fixture"))
        assert result.returncode != 0
        assert "incompatible privileges" in result.stderr
        connection.execute("ALTER ROLE sbm NOCREATEDB")


def test_inventory_import_subscriptions_admin_and_node_sync(postgres, tmp_path):
    pg = postgres
    assert psql(pg, "postgres", provision_sql("sbm_accounts_fixture")).returncode == 0
    path, _, env = config(pg, tmp_path, "sbm_accounts_fixture")
    example = yaml.safe_load((ROOT / "config/inventory/example.yaml").read_text())
    # Keep this import fixture on its original three-protocol infrastructure.
    example["deployment"].pop("snell_port", None)
    pw_hash = example["web_portal"]["users"][0]["password_hash"]
    sub_db = tmp_path / "subscriptions.sqlite3"
    from sing_box_manager.release.client_downloads import client_manifest
    from sing_box_manager.release.manifest import MANIFEST_FILENAME, Manifest, Member

    releases_dir = tmp_path / "releases"
    release_dir = releases_dir / "sing-box-v1.14.2"
    client_dir = release_dir / "client"
    (client_dir / "sbc" / "linux-amd64").mkdir(parents=True)
    (client_dir / "sbc" / "linux-amd64" / "sbc").write_bytes(b"test")
    cm_bytes = client_manifest(
        [("sbc/linux-amd64/sbc", 4, "a" * 64)],
        sbc="0.2.0",
        sing_box="1.14.2",
    )
    (client_dir / "manifest.json").write_bytes(cm_bytes)
    (client_dir / "manifest.json.sig").write_text("fixture-sig\n")
    manifest = Manifest(
        upstream_version="1.14.2",
        prefixes=(),
        archives=(),
        client_members=(
            Member(
                arcname="manifest.json",
                mode=0o644,
                size=len(cm_bytes),
                sha256="b" * 64,
                src="client/manifest.json",
            ),
            Member(
                arcname="manifest.json.sig",
                mode=0o644,
                size=12,
                sha256="c" * 64,
                src="client/manifest.json.sig",
            ),
            Member(
                arcname="sbc/linux-amd64/sbc",
                mode=0o755,
                size=4,
                sha256="a" * 64,
                src="client/sbc/linux-amd64/sbc",
            ),
        ),
    )
    (release_dir / MANIFEST_FILENAME).write_text(manifest.to_json())

    full_cfg = yaml.safe_load(path.read_text())
    full_cfg.update(
        {
            "releases_root": str(releases_dir),
            "web": {
                "session_secret": "synthetic-session-secret-" + "1" * 24,
                "subscription_secret": "synthetic-subscription-secret-" + "2" * 24,
                "subscription_database_path": str(sub_db),
            },
            "deployment": example["deployment"],
            "web_portal": {
                "users": [
                    {
                        "username": "alice",
                        "password_hash": pw_hash,
                        "enabled": True,
                        "admin": True,
                    },
                    {
                        "username": "bob",
                        "password_hash": pw_hash,
                        "enabled": False,
                        "admin": False,
                    },
                    {
                        "username": "carol",
                        "password_hash": pw_hash,
                        "enabled": True,
                        "admin": False,
                    },
                ]
            },
            "trojan": {
                "users": [
                    {
                        "username": "alice",
                        "password": "alice-trojan-secret",
                        "enabled": True,
                    },
                    {
                        "username": "bob",
                        "password": "bob-trojan-secret",
                        "enabled": True,
                    },
                    {
                        "username": "dave",
                        "password": "dave-trojan-secret",
                        "enabled": True,
                    },
                ]
            },
            "hysteria2": {
                "obfs_password": "fixture-obfs-password",
                "users": [
                    {
                        "username": "alice",
                        "password": "alice-hy2-secret",
                        "enabled": True,
                    },
                ],
            },
            "naive": {
                "users": [
                    {
                        "username": "alice",
                        "password": "alice-naive-secret",
                        "enabled": True,
                    },
                ]
            },
        }
    )
    path.write_text(yaml.safe_dump(full_cfg))

    import sqlite3

    with sqlite3.connect(sub_db) as counters:
        counters.execute("CREATE TABLE reset_counters (username TEXT PRIMARY KEY, counter INTEGER)")
        counters.execute("INSERT INTO reset_counters VALUES ('alice', 2)")

    assert cli(path, env, "migrate").returncode == 0
    assert cli(path, env, "collect-static").returncode == 0
    imported = cli(path, env, "import-inventory")
    assert imported.returncode == 0, imported.stderr
    assert '"accounts": 4' in imported.stdout
    assert '"credentials": 5' in imported.stdout

    # Repeat import is idempotent and never lowers subscription_counter.
    sub_db.unlink()
    imported_again = cli(path, env, "import")
    assert imported_again.returncode == 0, imported_again.stderr

    output = python(
        env,
        """
import json
import os
import django
django.setup()
from django.test import Client
from sing_box_manager.control_plane.admin import AccountAdmin, AccountAdminForm
from sing_box_manager.control_plane.models import Account, Credential
from sing_box_manager.control_plane.services import (
    build_inventory_from_db,
    sync_node_config,
    verify_inventory_parity,
)
from sing_box_manager.settings import load_settings
from sing_box_manager.subscription import subscription_token

runtime = load_settings(os.environ["SBM_CONFIG"])
alice = Account.objects.get(username="alice")
assert alice.subscription_counter == 2
assert alice.is_portal_admin is True

secret = runtime.web.subscription_secret
alice_token = subscription_token(secret, "alice", 2)
old_alice_token = subscription_token(secret, "alice", 1)
bob_token = subscription_token(secret, "bob", 0)
carol_token = subscription_token(secret, "carol", 0)
dave_token = subscription_token(secret, "dave", 0)

client = Client(HTTP_HOST="127.0.0.1")
assert client.get(f"/sub/{old_alice_token}?format=sbc&os=linux&sing-box=1.14.2").status_code == 404
assert client.get(f"/sub/{bob_token}?format=sbc&os=linux&sing-box=1.14.2").status_code == 404
assert client.get(f"/sub/{carol_token}?format=sbc&os=linux&sing-box=1.14.2").status_code == 404
assert client.get(f"/sub/{dave_token}?format=sbc&os=linux&sing-box=1.14.2").status_code == 404

resp = client.get(f"/sub/{alice_token}?format=sbc&os=linux&sing-box=1.14.2")
assert resp.status_code == 200
assert resp.headers["Referrer-Policy"] == "no-referrer"
etag = resp.headers["ETag"]
payload = resp.json()
assert payload["latest"] == {"sing_box": "1.14.2", "sbc": "0.2.0"}
assert payload["config"]["inbounds"][0]["type"] == "mixed"
tun = payload["config_tun"]
inbound = tun["inbounds"][0]
assert inbound["type"] == "tun"
assert inbound["dns_mode"] == "hijack"
assert inbound["auto_route"] and inbound["strict_route"] and inbound["auto_redirect"]
assert any(":" in address for address in inbound["address"])
assert tun["dns"]["strategy"] == "ipv4_only"
assert all(server["tag"] != "system" for server in tun["dns"]["servers"])
rules = tun["route"]["rules"]
private = next(i for i, rule in enumerate(rules) if rule.get("ip_is_private") is True)
assert rules[private + 1] == {"ip_version": 6, "action": "reject"}
assert payload["config"]["dns"].get("strategy") != "ipv4_only"
assert not any(rule.get("ip_version") == 6 for rule in payload["config"]["route"]["rules"])
assert client.get(
    f"/sub/{alice_token}?format=sbc&os=linux&sing-box=1.14.2",
    HTTP_IF_NONE_MATCH=etag,
).status_code == 304
assert client.get(f"/sub/{alice_token}?format=sbc&os=linux&sing-box=1.13.0").status_code == 409
assert client.get(f"/sub/{alice_token}?format=raw&os=linux&sing-box=1.14.2").status_code == 400

# Mobile remote profile and browser subscription page work on public Host, while /admin/ is 404 off loopback.
public_client = Client(HTTP_HOST=runtime.deployment.host)
assert public_client.get("/admin/").status_code == 404
assert client.get("/admin/", HTTP_X_FORWARDED_FOR="203.0.113.5").status_code == 404
mobile_resp = public_client.get(f"/sub/{alice_token}")
assert mobile_resp.status_code == 200
assert mobile_resp.json()["inbounds"][0]["type"] == "tun"
assert mobile_resp.json()["outbounds"][0]["type"] == "selector"
assert mobile_resp.json()["dns"].get("strategy") != "ipv4_only"
assert not any(rule.get("ip_version") == 6 for rule in mobile_resp.json()["route"]["rules"])
assert public_client.get(f"/sub/{alice_token}?protocol=hysteria2").status_code == 400
assert public_client.get(f"/sub/{alice_token}?protocol=unknown").status_code == 400
page_resp = public_client.get(f"/sub/{alice_token}/", HTTP_X_FORWARDED_PROTO="https")
assert page_resp.status_code == 200
assert f"https://{runtime.deployment.host}/sub/{alice_token}" in page_resp.content.decode()
assert f"https://{runtime.deployment.host}/install.sh" in page_resp.content.decode()
assert public_client.get(f"/sub/{carol_token}/").status_code == 404

# Portal-only user carol can still fetch signed manifest files, while bob/dave cannot.
assert client.get(f"/sub/{carol_token}/files/manifest.json").status_code == 200
sbc_resp = client.get(f"/sub/{alice_token}/files/sbc/linux-amd64/sbc")
assert b"".join(sbc_resp.streaming_content) == b"test"
assert client.get(f"/sub/{alice_token}/files/../manifest.json").status_code == 404
assert client.get(f"/sub/{bob_token}/files/manifest.json").status_code == 404
assert client.get("/install.sh").status_code == 200
assert client.get("/install.ps1").status_code == 200

# Admin hashes optional legacy passwords, generates protocol credentials, and resets links.
old_hash = alice.portal_password_hash
form = AccountAdminForm(
    data={
        "username": "alice",
        "enabled": True,
        "portal_enabled": True,
        "is_portal_admin": True,
        "new_portal_password": "new-operator-set-password",
        "portal_password_hash": old_hash,
        "subscription_counter": alice.subscription_counter,
    },
    instance=alice,
)
assert form.is_valid(), form.errors
saved = form.save()
assert saved.portal_password_hash != old_hash

from sing_box_manager.control_plane.services import (
    ensure_default_credentials,
    request_node_sync,
)

new_form = AccountAdminForm(
    data={
        "username": "frank",
        "enabled": True,
        "portal_enabled": True,
        "is_portal_admin": False,
        "new_portal_password": "",
        "portal_password_hash": "",
        "subscription_counter": 0,
    },
)
assert new_form.is_valid(), new_form.errors
frank = new_form.save()
assert frank.portal_password_hash == ""
created_protos = ensure_default_credentials(frank)
assert set(created_protos) == {"trojan", "hysteria2", "naive", "snell"}
frank_token = subscription_token(secret, "frank", 0)
assert client.get(f"/sub/{frank_token}?format=sbc&os=linux&sing-box=1.14.2").status_code == 200
trigger = request_node_sync(runtime)
assert trigger.is_file()
frank.delete()

Account.objects.filter(pk=alice.pk).update(subscription_counter=3)
assert client.get(f"/sub/{alice_token}?format=sbc&os=linux&sing-box=1.14.2").status_code == 404
new_token = subscription_token(secret, "alice", 3)
assert client.get(f"/sub/{new_token}?format=sbc&os=linux&sing-box=1.14.2").status_code == 200

# Corrupting a credential in DB causes parity check to fail.
Credential.objects.filter(account=alice, protocol="trojan").update(secret="tampered")
try:
    verify_inventory_parity(runtime, build_inventory_from_db(runtime), runtime.traffic_stats)
except ValueError:
    pass
else:
    raise AssertionError("parity check did not detect tampered credential")
print("ok")
""",
    )
    assert output.strip() == "ok"

    node_cfg = tmp_path / "sing-box-server.json"
    sync_env = {
        **env,
        "SBM_SING_BOX_CONFIG": str(node_cfg),
        "SBM_SING_BOX_BINARY": str(tmp_path / "nonexistent-sing-box"),
    }
    sync_out = python(
        sync_env,
        """
import json
import os
from pathlib import Path
import django
django.setup()
from sing_box_manager.control_plane.services import sync_node_config
from sing_box_manager.settings import load_settings
runtime = load_settings(os.environ["SBM_CONFIG"])
result = sync_node_config(runtime, restart_service=False)
print(json.dumps(result))
""",
    )
    assert node_cfg.is_file()
    rendered_server = yaml.safe_load(node_cfg.read_text())
    assert [i["tag"] for i in rendered_server["inbounds"]] == [
        "trojan",
        "hysteria2",
        "naive",
    ]
    assert "trojan" in sync_out
