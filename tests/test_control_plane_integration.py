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

import pytest
import psycopg
import yaml

from sing_box_manager.control_plane.database import provision_sql

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


def cli(path, env, action):
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "sing_box_manager",
            "--config",
            str(path),
            "control-plane",
            "--action",
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
print('ok')
""",
    )
    assert output.strip() == "ok"
    assert psql(pg, "postgres", provision_sql("sbm_fixture")).returncode == 0
    assert cli(path, env, "migrate").returncode == 0
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
                "--action",
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
            form = urllib.parse.urlencode(
                {
                    "username": "http_operator",
                    "password": "synthetic-fixture-pass-123",
                    "csrfmiddlewaretoken": csrf,
                    "next": "/admin/",
                }
            ).encode()
            with opener.open(
                f"http://127.0.0.1:{port}/admin/login/", data=form
            ) as response:
                assert response.geturl().endswith("/admin/")
                assert "Site administration" in response.read().decode()
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/health/",
                headers={"Host": "attacker.example.com"},
            )
            with pytest.raises(urllib.error.HTTPError) as error:
                opener.open(request)
            assert error.value.code == 400
            assert cli(path, env, "create-admin").returncode == 1
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
