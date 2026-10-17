"""Synthetic native rehearsal. Execute only on an opted-in GitHub-hosted runner.

No production inputs, SSH, global credentials, or uploaded diagnostic artifacts.
Only the SSH transport is replaced with local execution. Services and PostgreSQL
are real. Local tests must mock every process call and must never invoke the run.
"""

import base64
import hashlib
import http.client
import json
import os
from pathlib import Path
import platform
import pwd
import secrets
import re
import shlex
import shutil
import ssl
import subprocess
import socket
import stat
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import time
from unittest.mock import patch
from uuid import UUID

ROOT = Path("/srv/sbm-managed-native")
SOURCE = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config/inventory/runtime.yaml"
PYTHON = ROOT / ".pixi/envs/default/bin/python"
WORK = Path("/var/lib/sbm-managed-native-fixture")
DOMAIN = "vpn.example.test"
DATABASE = "sbm_managed_native"
RESTORED = "sbm_managed_restored"
NS, SOURCE_ID = UUID(int=101), UUID(int=102)
PASSWORD = "synthetic-native-login"
PROXY_PASSWORD = "synthetic-native-protocol"
PROXY_NAMES = ("http_proxy", "https_proxy", "ftp_proxy", "socks_proxy", "all_proxy")
COMMAND_NUMBER = 0
UNITS = (
    "sing-box-manager-portal.service",
    "sing-box-manager-web.service",
    "sing-box-manager-control-plane.service",
    "sing-box.service",
)


class NativeFailure(RuntimeError):
    """A fixed public-safe assertion, not captured process output."""


def require(value, message):
    if not value:
        raise NativeFailure(message)


def guard():
    require(
        os.environ.get("GITHUB_ACTIONS") == "true"
        and os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted"
        and os.environ.get("SBM_MANAGED_NATIVE_E2E") == "1",
        "An opted-in disposable GitHub-hosted runner is required",
    )
    release = platform.freedesktop_os_release()
    require(
        release.get("ID") == "ubuntu" and release.get("VERSION_ID") == "24.04",
        "Ubuntu 24.04 is required",
    )
    for path in (
        ROOT,
        WORK,
        Path("/opt/sing-box-manager"),
        Path("/var/lib/sing-box-manager-control-plane"),
        Path("/var/lib/sing-box-manager"),
        Path("/var/lib/sing-box"),
        Path("/usr/local/etc/sing-box"),
        Path("/etc/nginx/sites-available/sing-box-manager.conf"),
        Path("/etc/nginx/sites-enabled/sing-box-manager.conf"),
        Path("/usr/local/bin/sing-box"),
        *[Path("/etc/systemd/system") / unit for unit in UNITS],
    ):
        require(not os.path.lexists(path), "Refusing an existing native target")
    for parent in ROOT.parents:
        info = parent.lstat()
        require(
            stat.S_ISDIR(info.st_mode)
            and info.st_uid == 0
            and not info.st_mode & 0o022,
            "Native application ancestors must be root-owned and non-writable",
        )
    try:
        pwd.getpwnam("sbm")
    except KeyError:
        pass
    else:
        raise NativeFailure("Refusing an existing sbm account")


def run(args, *, data=None, cwd=None, timeout=120, success=True):
    global COMMAND_NUMBER
    COMMAND_NUMBER += 1
    env = {
        key: value
        for key, value in os.environ.items()
        if key.lower() not in PROXY_NAMES
    }
    result = subprocess.run(
        args, input=data, capture_output=True, cwd=cwd, env=env, timeout=timeout
    )
    if success:
        require(
            result.returncode == 0,
            f"Native command {COMMAND_NUMBER} ({Path(args[0]).name}) failed with exit {result.returncode}",
        )
    return result


def shell(value):
    return run(["sudo", "bash", "-eu", "-c", value], timeout=600)


def local_ssh(_host, value, _error, *, dry_run=False):
    require(not dry_run, "Native rehearsal cannot use dry-run service stand-ins")
    shell(value)


def local_stdout(_host, value, _error):
    return shell(value).stdout.decode().strip()


def local_write(_host, path, content, *, mode=None, dry_run=False):
    require(not dry_run, "Native rehearsal cannot use dry-run file stand-ins")
    write(Path(path), content.encode(), int(mode or "0644", 8))


def write(path, content, mode=0o640):
    # Values travel on stdin, not in shell arguments or environment variables.
    run(["sudo", "tee", str(path)], data=content)
    shell(
        f"chown root:sbm {shlex.quote(str(path))}; chmod {mode:o} {shlex.quote(str(path))}"
    )


def python(code, *, user="root", config=CONFIG, success=True):
    environment = [
        "PATH=/usr/sbin:/usr/bin:/sbin:/bin",
        "LANG=C.UTF-8",
        "PYTHONDONTWRITEBYTECODE=1",
        "PYTHONNOUSERSITE=1",
        f"SBM_CONFIG={config}",
        "DJANGO_SETTINGS_MODULE=sing_box_manager.control_plane.django_settings",
    ]
    wrapped = f"""import sys,traceback
try:
 exec(compile({code!r}, '<native-fixture>', 'exec'))
except Exception as error:
 kind=type(error).__name__
 codes=('unsafe_directory','unsafe_source_file','source_changed','source_rejected','source_auth_mismatch','source_proxy_mismatch','account_copy_context_rejected','production_target_required','field_parity_failed','render_parity_failed','portal_verifier_cost_unsupported')
 if kind=='ImportRejectedError' and str(error) in codes:
  kind=str(error)
 elif kind not in ('AssertionError','ImportError','ValueError','KeyError','TypeError','PermissionError','ProgrammingError','OperationalError','IntegrityError'):
  kind='OtherError'
 lines=[frame.lineno for frame in traceback.extract_tb(error.__traceback__) if frame.filename=='<native-fixture>']
 print('NATIVE_ERROR:'+kind+':'+str(lines[-1] if lines else 0),file=sys.stderr)
 sys.exit(1)
"""
    result = run(
        [
            "sudo",
            "-u",
            user,
            "sh",
            "-c",
            f"cd {shlex.quote(str(ROOT))} && exec "
            + shlex.join(["env", "-i", *environment, str(PYTHON), "-B", "-"]),
        ],
        data=wrapped.encode(),
        success=False,
    )
    if success and result.returncode:
        diagnostic = re.search(
            rb"^NATIVE_ERROR:([A-Za-z_]+):([0-9]+)$", result.stderr, re.MULTILINE
        )
        require(
            False,
            "Native Python failed"
            + (
                ": "
                + diagnostic[1].decode()
                + " at fixture line "
                + diagnostic[2].decode()
                if diagnostic
                else " without a safe diagnostic"
            ),
        )
    return result


def database(code, *, config=CONFIG):
    preamble = """import django,json; django.setup()
from pathlib import Path
from uuid import UUID
from django.db import connection
from sing_box_manager.control_plane.django_settings import runtime
from sing_box_manager.control_plane.models import ImportTarget,ImportSource,ImportRun,ProxySubject,ImportedAccess,LegacyAccess
from sing_box_manager.account_copy import service as copying
from sing_box_manager.managed_access import service
from sing_box_manager.managed_access.models import Ownership,LinkGeneration
"""
    result = python(preamble + code, user="sbm", config=config)
    return json.loads(result.stdout) if result.stdout.strip() else None


def node(action, run_dir=None):
    # Exercise actual supervisor functions and IPC, with a private operator umask.
    code = f"""import os,json
os.umask(0o077)
from pathlib import Path
from sing_box_manager.account_copy.export import load_source_settings
from sing_box_manager.managed_access import node
runtime=load_source_settings(Path({str(CONFIG)!r}))
try:
 result=node.{action}(runtime{", Path(" + repr(str(run_dir)) + ")" if run_dir else ""}{", acknowledge_interrupt=True" if action in ("fence", "apply") else ""})
 print(json.dumps(result))
except node.NodeRejected as error:
 # This exception class contains fixed product error codes, never process output.
 print(json.dumps({{"rejected":str(error)}}))
"""
    value = json.loads(python(code).stdout)
    require(
        "rejected" not in value,
        "Native supervisor rejected: " + value.get("rejected", ""),
    )
    return value


def request(path, *, method="GET", headers=None, body=None):
    context = ssl.create_default_context(
        cafile=str(SOURCE / ".cache/native-fixture-cert.pem")
    )
    connection = http.client.HTTPSConnection(
        "127.0.0.1", 443, context=context, timeout=10
    )
    try:
        connection.request(
            method, path, body=body, headers={"Host": DOMAIN, **(headers or {})}
        )
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read(2**20)
    finally:
        connection.close()


def wait_http(path, expected=200):
    for _ in range(40):
        try:
            if request(path)[0] == expected:
                return
        except (OSError, http.client.HTTPException):
            pass
        time.sleep(0.25)
    raise NativeFailure("Native HTTPS readiness failed")


def settings():
    from argon2 import PasswordHasher
    from sing_box_manager.settings import Settings

    return Settings.model_validate(
        {
            "config_path": str(CONFIG),
            "sing_box_version": "1.14.2",
            "deployment": {
                "host": DOMAIN,
                "ip": "127.0.0.1",
                "trojan_port": 18443,
                "hysteria2_port": 18444,
                "naive_port": 18445,
                "tls": {
                    "enabled": True,
                    "self_signed_cert": False,
                    "server_name": DOMAIN,
                    "certificate_path": str(ROOT / "fixture.crt"),
                    "key_path": str(ROOT / "fixture.key"),
                },
            },
            "web": {
                "allowed_hosts": [DOMAIN, "localhost", "127.0.0.1"],
                "session_secret": secrets.token_urlsafe(48),
                "subscription_secret": secrets.token_urlsafe(48),
                "subscription_database_path": "/var/lib/sing-box-manager/subscriptions.sqlite3",
            },
            "control_plane": {
                "enabled": True,
                "secret_key": secrets.token_urlsafe(48),
                "database_name": DATABASE,
                "portal_enabled": True,
                "portal_origin": "https://" + DOMAIN,
                "subscription_secret": secrets.token_urlsafe(48),
            },
            "web_portal": {
                "users": [
                    {
                        "username": "alice",
                        "password_hash": PasswordHasher(
                            time_cost=1, memory_cost=1024, parallelism=1
                        ).hash(PASSWORD),
                        "admin": True,
                    }
                ]
            },
            "trojan": {"users": [{"username": "alice", "password": PROXY_PASSWORD}]},
            "hysteria2": {
                "users": [{"username": "alice", "password": PROXY_PASSWORD}],
                "obfs_password": "synthetic-native-obfs",
            },
            "naive": {"users": [{"username": "alice", "password": PROXY_PASSWORD}]},
            "traffic_stats": {"enabled": True},
        }
    )


def seed_files(runtime):
    import yaml
    from sing_box_manager.release.profiles import build_server_config
    from sing_box_manager.release.client_downloads import (
        client_manifest,
        generate_key,
        public_key,
        sign,
    )
    from sing_box_manager.release.manifest import Manifest, Member
    from sing_box_manager.release.artifacts import ReleaseInfo

    shell(
        f"install -d -o root -g sbm -m 0750 {ROOT}/config/inventory {ROOT}/config/generated {ROOT}/releases/native/client /usr/local/etc/sing-box; install -d -o sbm -g sbm -m 0700 {WORK}"
    )
    write(
        CONFIG,
        yaml.safe_dump(
            runtime.model_dump(mode="json", exclude={"config_path"})
        ).encode(),
    )
    write(
        ROOT / runtime.auth_snapshot_path,
        json.dumps(
            {"users": [row.model_dump(mode="json") for row in runtime.web_portal.users]}
        ).encode(),
    )
    write(
        Path("/usr/local/etc/sing-box/config.json"),
        json.dumps(build_server_config(runtime, runtime.traffic_stats)).encode(),
        0o600,
    )
    payload = b"synthetic signed native download\n"
    manifest = client_manifest(
        [("fixture.txt", len(payload), hashlib.sha256(payload).hexdigest())],
        sbc="native-fixture",
        sing_box="1.14.2",
    )
    key = generate_key()
    members = []
    for name, data in [
        ("fixture.txt", payload),
        ("manifest.json", manifest),
        ("manifest.json.sig", sign(key, manifest).encode()),
    ]:
        write(ROOT / "releases/native/client" / name, data)
        members.append(
            Member(
                name,
                0o644,
                len(data),
                hashlib.sha256(data).hexdigest(),
                "client/" + name,
            )
        )
    release = Manifest(
        upstream_version="1.14.2",
        prefixes=(),
        archives=(),
        client_members=tuple(members),
    )
    write(ROOT / "releases/native/manifest.json", release.to_json().encode())
    info = ReleaseInfo(
        "native",
        "synthetic-native",
        False,
        "20261017",
        "1.14.2",
        deployment_host=DOMAIN,
    )
    write(ROOT / runtime.release_info_path, json.dumps(info.to_dict()).encode())
    return public_key(key)


def setup(runtime):
    from sing_box_manager.control_plane import deployment as private
    from sing_box_manager import provision

    pixi = shutil.which("pixi")
    require(pixi is not None, "Pixi is required")
    with patch.object(provision, "_ssh", local_ssh):
        provision._create_service_user_and_root(
            "fixture-local", str(ROOT), dry_run=False
        )
    for name in (
        "sing_box_manager",
        "scripts",
        "pixi.toml",
        "pixi.lock",
        "pyproject.toml",
        "config/inventory/example.yaml",
    ):
        target = ROOT / name
        shell(
            f"mkdir -p {shlex.quote(str(target.parent))}; cp -a {shlex.quote(str(SOURCE / name))} {shlex.quote(str(target))}"
        )
    run(
        [
            "sudo",
            "env",
            *[
                item
                for key in PROXY_NAMES
                for variant in (key, key.upper())
                for item in ("-u", variant)
            ],
            "XDG_CACHE_HOME=/var/cache/sbm-managed-native",
            pixi,
            "install",
            "--locked",
            "--no-hard-links",
            "--manifest-path",
            str(ROOT / "pixi.toml"),
        ],
        timeout=900,
    )
    shell(
        "export DEBIAN_FRONTEND=noninteractive; apt-get update -qq; apt-get install -y -qq postgresql-16 postgresql-client-16 postgresql postgresql-client nginx"
    )
    shell(
        f'openssl req -x509 -newkey rsa:2048 -nodes -days 2 -subj /CN={DOMAIN} -addext "subjectAltName=DNS:{DOMAIN},IP:127.0.0.1" -keyout {ROOT}/fixture.key -out {ROOT}/fixture.crt'
    )
    cert = run(["sudo", "cat", str(ROOT / "fixture.crt")]).stdout
    (SOURCE / ".cache/native-fixture-cert.pem").write_bytes(cert)
    signing_public = seed_files(runtime)
    binary = SOURCE / ".cache/sing-box-manager/server/sing-box"
    require(binary.is_file(), "Pinned server binary is required")
    shell(
        f"cp -L {shlex.quote(str(binary))} /usr/local/bin/sing-box; chmod 0755 /usr/local/bin/sing-box; cp {ROOT}/scripts/sing-box.service /etc/systemd/system/sing-box.service"
    )
    shell(
        f"chown -R root:sbm {ROOT}; chmod -R u+rwX,g+rX,g-w,o-rwx {ROOT}; chmod 0640 {CONFIG}; chmod 0600 {ROOT}/fixture.key"
    )
    with (
        patch.multiple(private, _ssh=local_ssh, _ssh_write=local_write),
        patch.multiple(
            provision, _ssh=local_ssh, _ssh_stdout=local_stdout, _ssh_write=local_write
        ),
    ):
        private.deploy("fixture-local", str(ROOT), runtime)
    database(
        "with connection.cursor() as c:\n c.execute('SHOW server_version_num'); assert int(c.fetchone()[0])//10000==16\nassert not Ownership.objects.exists()\nassert not LinkGeneration.objects.exists()\n"
        "from sing_box_manager.control_plane.models import User\n"
        "from django.contrib.sessions.backends.db import SessionStore\n"
        "operator=User.objects.create_user(username='native-operator',password='synthetic-native-operator',is_staff=True,is_superuser=True)\n"
        "session=SessionStore(); session['fixture']='synthetic-native-session'; session.save()\n"
    )
    local_write(
        None,
        "/etc/systemd/system/sing-box-manager-web.service",
        provision._web_service_unit(str(ROOT)),
    )
    site = f"""server {{
    listen 127.0.0.1:443 ssl;
    server_name {DOMAIN};
    ssl_certificate {ROOT}/fixture.crt;
    ssl_certificate_key {ROOT}/fixture.key;
    access_log off;
    error_log /dev/null;
    location / {{
        proxy_pass http://127.0.0.1:47070;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }}
}}
"""
    local_write(None, provision.NGINX_SITE_PATH, site)
    shell(
        f"ln -s {provision.NGINX_SITE_PATH} /etc/nginx/sites-enabled/sing-box-manager.conf"
    )
    shell("systemctl daemon-reload")
    shell("systemctl start sing-box.service")
    shell("systemctl start sing-box-manager-web.service")
    shell("nginx -t")
    # Package installation does not guarantee that the runner starts nginx.
    shell("systemctl restart nginx")
    # The legacy landing page redirects to login. Test a usable old link instead.
    from sing_box_manager.subscription import subscription_token

    old = "/sub/" + subscription_token(runtime.web.subscription_secret, "alice")
    wait_http(old + "?format=sbc&os=linux&sing-box=1.14.2")
    return signing_public


def copy_prepare():
    shell(f"install -d -o root -g root -m 0700 {ROOT}/private-inputs")
    python(f"""from pathlib import Path
from uuid import UUID
from sing_box_manager.account_copy.export import load_source_settings,export_snapshot
runtime=load_source_settings(Path({str(CONFIG)!r}))
export_snapshot(runtime,Path({str(ROOT / "private-inputs/snapshot.json")!r}),Path({str(ROOT / "config/generated/auth-users.json")!r}),Path('/usr/local/etc/sing-box/config.json'),namespace=UUID(int=101),source_identity=UUID(int=102),sequence=1)
""")
    shell(
        f"install -o sbm -g sbm -m 0600 {ROOT}/private-inputs/snapshot.json {WORK}/snapshot.json"
    )
    database(f"""namespace=UUID(int=101); source=UUID(int=102)
copying.authorize(runtime,namespace,source)
copying.preview(Path({str(WORK / "snapshot.json")!r}),Path({str(WORK / "report.json")!r}),runtime)
copying.apply(Path({str(WORK / "snapshot.json")!r}),Path({str(WORK / "report.json")!r}),runtime)
assert not ImportTarget.objects.get().fixture_enabled
assert not Ownership.objects.exists() and not LegacyAccess.objects.exists()
copying.close(runtime,namespace,source)
service.prepare(runtime,namespace)
assert not Ownership.objects.get().active
""")


def link():
    value = database(
        "value=service.owner(runtime,active=True)\nprint(json.dumps({'url':service.link_for(runtime,value,ProxySubject.objects.get(username='alice').pk)}))"
    )
    return value["url"].removeprefix("https://" + DOMAIN)


def check_download(token_path, public_key):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    status, _, manifest = request(token_path + "/files/manifest.json")
    require(status == 200, "Signed manifest unavailable")
    status, _, signature = request(token_path + "/files/manifest.json.sig")
    require(status == 200, "Manifest signature unavailable")
    Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key)).verify(
        base64.b64decode(signature), manifest
    )
    status, _, body = request(token_path + "/files/fixture.txt")
    entry = json.loads(manifest)["files"][0]
    require(
        status == 200 and hashlib.sha256(body).hexdigest() == entry["sha256"],
        "Signed download mismatch",
    )
    require(
        request(token_path + "/files/unknown")[0] == 404,
        "Download path boundary failed",
    )


def portal_checks(runtime, public_key):
    from sing_box_manager.subscription import subscription_token

    old = "/sub/" + subscription_token(runtime.web.subscription_secret, "alice")
    params = "?format=sbc&os=linux&sing-box=1.14.2"
    current = link()
    require(
        current != old and len(current.removeprefix("/sub/")) == 22,
        "Fresh token format failed",
    )
    status, headers, body = request(current + params)
    require(status == 200 and json.loads(body), "Subscription unavailable")
    require(
        request(current + params, headers={"If-None-Match": headers["ETag"]})[0] == 304,
        "ETag negotiation failed",
    )
    require(
        request(current + "?format=sbc&os=linux&sing-box=1.0.0")[0] == 409,
        "Version boundary failed",
    )
    for path in (
        old + params,
        old + "/files/manifest.json",
        "/admin/",
        "/api/token",
        "/files/archive.zip",
    ):
        require(request(path)[0] == 404, "Legacy or operator route exposed")
    check_download(current, public_key)
    basic = "Basic " + base64.b64encode(("alice:" + PASSWORD).encode()).decode()
    require(
        request("/api/sub", method="POST", headers={"Authorization": basic})[0] == 200,
        "Password exchange failed",
    )
    require(
        request(
            "/api/sub",
            method="POST",
            headers={"Authorization": "Bearer synthetic-token"},
        )[0]
        == 401,
        "Legacy machine token accepted",
    )
    database(
        "service.reset_link(runtime,ProxySubject.objects.get(username='alice').pk)"
    )
    require(
        request(current + params)[0] == 404
        and request(current + "/files/manifest.json")[0] == 404,
        "Revocation failed",
    )
    require(request(link() + params)[0] == 200, "Replacement link unavailable")


def proxy_check(password, *, accepted=True):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"synthetic-native-proxy-ok")

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    response = b""
    try:
        context = ssl.create_default_context(
            cafile=str(SOURCE / ".cache/native-fixture-cert.pem")
        )
        with (
            socket.create_connection(("127.0.0.1", 18443), timeout=3) as raw,
            context.wrap_socket(raw, server_hostname=DOMAIN) as client,
        ):
            # Trojan CONNECT: SHA224 password, IPv4 destination, then HTTP bytes.
            address = b"\x01\x01\x7f\x00\x00\x01" + server.server_port.to_bytes(
                2, "big"
            )
            client.sendall(
                hashlib.sha224(password.encode()).hexdigest().encode()
                + b"\r\n"
                + address
                + b"\r\nGET / HTTP/1.0\r\nHost: fixture.test\r\n\r\n"
            )
            while len(response) < 65536:
                chunk = client.recv(4096)
                if not chunk:
                    break
                response += chunk
    except (OSError, ssl.SSLError):
        require(not accepted, "Native Trojan request failed")
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
    require(
        (b"synthetic-native-proxy-ok" in response) == accepted,
        "Native Trojan credential boundary failed",
    )


def apply_edit():
    database("""from sing_box_manager.control_plane.import_schema import AccessRecords
value=service.owner(runtime,active=True)
records=service.accounts(value).model_dump(mode='json')
records['trojan'][0]['password']='synthetic-native-updated-protocol'
service.save_account(runtime,'alice',AccessRecords.model_validate(records),subject_id=ProxySubject.objects.get(username='alice').pk,expected_revision=value.revision)
assert not service.ready(service.owner(runtime),runtime)
assert runtime.trojan.users[0].password=='synthetic-native-protocol'
""")
    params = "?format=sbc&os=linux&sing-box=1.14.2"
    require(request(link() + params)[0] == 503, "Pending configuration was served")
    node("plan", Path("/var/lib/sing-box-manager-control-plane/runs/second"))
    node("apply", Path("/var/lib/sing-box-manager-control-plane/runs/second"))
    require(request(link() + params)[0] == 200, "Applied configuration unavailable")
    python(
        "import json\nfrom pathlib import Path\nconfig=json.loads(Path('/usr/local/etc/sing-box/config.json').read_text())\nassert next(row for row in config['inbounds'] if row['type']=='trojan')['users'][0]['password']=='synthetic-native-updated-protocol'\n"
    )


def snapshot(config=CONFIG):
    return database(
        """import hashlib
result={}
with connection.cursor() as c:
 c.execute('SET default_transaction_read_only=on')
 for name in connection.introspection.table_names():
  c.execute('SELECT to_jsonb(t)::text FROM '+connection.ops.quote_name(name)+' t ORDER BY to_jsonb(t)::text COLLATE "C"')
  result[name]=hashlib.sha256(json.dumps(c.fetchall()).encode()).hexdigest()
 c.execute("SELECT table_name,column_name,data_type,udt_name,is_nullable,column_default,is_identity,identity_generation FROM information_schema.columns WHERE table_schema='public' ORDER BY table_name,column_name")
 columns=c.fetchall()
 c.execute("SELECT conrelid::regclass::text,pg_get_constraintdef(oid) FROM pg_constraint WHERE connamespace='public'::regnamespace ORDER BY 1,2")
 constraints=c.fetchall()
 c.execute("SELECT sequencename FROM pg_sequences WHERE schemaname='public' ORDER BY sequencename")
 sequences={}
 for (name,) in c.fetchall():
  c.execute('SELECT last_value,is_called FROM '+connection.ops.quote_name(name))
  sequences[name]=c.fetchone()
print(json.dumps({'tables':result,'columns':columns,'constraints':constraints,'sequences':sequences}))
""",
        config=config,
    )


def recovery(runtime):
    import yaml
    from sing_box_manager.control_plane.database import provision_sql

    before = snapshot()
    require(len(before["tables"]) == 19, "Unexpected backup table count")
    dump = run(
        [
            "sudo",
            "-u",
            "sbm",
            "/usr/lib/postgresql/16/bin/pg_dump",
            "-Fc",
            "--no-password",
            "--host=/var/run/postgresql",
            "--dbname=" + DATABASE,
        ]
    ).stdout
    write(ROOT / "managed-fixture.dump", dump, 0o600)
    run(
        [
            "sudo",
            "-u",
            "postgres",
            "/usr/lib/postgresql/16/bin/psql",
            "-X",
            "-v",
            "ON_ERROR_STOP=1",
            "--dbname=postgres",
        ],
        data=provision_sql(RESTORED).encode(),
    )
    run(
        [
            "sudo",
            "-u",
            "sbm",
            "/usr/lib/postgresql/16/bin/pg_restore",
            "--single-transaction",
            "--exit-on-error",
            "--no-owner",
            "--role=sbm",
            "--dbname=" + RESTORED,
        ],
        data=dump,
    )
    data = runtime.model_dump(mode="json", exclude={"config_path"})
    data["control_plane"]["database_name"] = RESTORED
    restored_config = ROOT / "config/inventory/restored.yaml"
    write(restored_config, yaml.safe_dump(data).encode())
    require(snapshot(restored_config) == before, "Native restore changed retained rows")
    database(
        """from sing_box_manager.control_plane.import_service import database_identity
from sing_box_manager.managed_access.errors import AccessRejected
from sing_box_manager.account_copy.models import AccountCopyAdmission
value=Ownership.objects.get()
assert value.active and value.revision==value.applied_revision==2
assert LinkGeneration.objects.get().generation==2
assert AccountCopyAdmission.objects.get().closed
assert database_identity(runtime,ImportTarget.objects.get())!=value.target_identity
try: service.owner(runtime,active=True)
except AccessRejected as error: assert str(error)=='ownership_state_rejected'
else: raise AssertionError('Restored database served under a stale destination binding')
""",
        config=restored_config,
    )
    require(snapshot() == before, "Restore check changed the source database")
    require(request("/health/")[0] == 200, "Restore check disturbed the active fixture")


def main():
    guard()  # Must precede imports that can call host tools and every mutation.
    phase = "fixture-setup"
    try:
        runtime = settings()
        public_key = setup(runtime)
        phase = "account-copy-and-preparation"
        proxy_check(PROXY_PASSWORD)
        copy_prepare()
        phase = "host-fence"
        node("fence")
        phase = "first-node-application"
        directory = Path("/var/lib/sing-box-manager-control-plane/runs/first")
        node("plan", directory)
        node("apply", directory)
        database("assert not Ownership.objects.get().active")
        phase = "ownership-activation"
        database("service.activate(runtime)")
        phase = "native-public-service-and-nginx"
        from sing_box_manager.managed_access import deployment
        from sing_box_manager import provision

        with (
            patch.multiple(deployment, _ssh=local_ssh),
            patch.multiple(provision, _ssh=local_ssh, _ssh_write=local_write),
        ):
            deployment.finish("fixture-local", str(ROOT), runtime, dry_run=False)
        wait_http("/health/")
        phase = "portal-and-revocation"
        portal_checks(runtime, public_key)
        proxy_check(PROXY_PASSWORD)
        phase = "database-owned-edit-and-reapplication"
        apply_edit()
        proxy_check("synthetic-native-updated-protocol")
        proxy_check(PROXY_PASSWORD, accepted=False)
        phase = "native-postgresql-16-restore"
        recovery(runtime)
        print(
            "PASS: native copy/prepare/fence/apply/activate, hardened public service, HTTPS nginx routing, signed downloads, fresh links/revocation, DB-owned reapplication, and PG16 restore binding rejection."
        )
    except Exception as error:
        if isinstance(error, NativeFailure):
            print(str(error))  # Only fixed assertions and supervisor error codes.
        print(
            "FAIL: native managed rehearsal at "
            + phase
            + ". Private fixture diagnostics are not uploaded."
        )
        raise SystemExit(1) from None
    finally:
        # Only this disposable job's exact service names. Retain files until runner teardown.
        for unit in UNITS:
            run(["sudo", "systemctl", "stop", unit], success=False)


if __name__ == "__main__":
    main()
