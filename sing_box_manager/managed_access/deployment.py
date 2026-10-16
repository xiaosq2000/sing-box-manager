"""Explicit managed deployment. No copy, preparation, or activation is implicit."""

import shlex
from pathlib import Path
from urllib.parse import urlsplit

from sing_box_manager.control_plane.deployment import service_unit as private_unit
from sing_box_manager.deploy import REMOTE_PIXI_PYTHON_RELATIVE_PATH, DeploymentError
from sing_box_manager.provision import NGINX_SITE_PATH, _install_unit, _ssh
from sing_box_manager.settings import Settings

SERVICE = "sing-box-manager-portal.service"
FENCE = "/var/lib/sing-box-manager-control-plane/owner.json"
RUNS = "/var/lib/sing-box-manager-control-plane/runs"


def command(
    root: str, action: str, *, privileged: bool = False, extra: tuple[str, ...] = ()
) -> str:
    argv = ["sudo"] if privileged else ["sudo", "-u", "sbm"]
    argv += [
        "env",
        "-i",
        "PATH=/usr/sbin:/usr/bin:/sbin:/bin",
        "PYTHONNOUSERSITE=1",
        "PYTHONDONTWRITEBYTECODE=1",
        str(Path(root) / REMOTE_PIXI_PYTHON_RELATIVE_PATH),
        "-m",
        "sing_box_manager",
        "--config",
        str(Path(root) / "config/inventory/runtime.yaml"),
        "managed-access",
        "--action",
        action,
        *extra,
    ]
    return f"cd {shlex.quote(root)} && {shlex.join(argv)}"


def guard_command(*, managed: bool) -> str:
    # This probe must work before the application or Pixi environment is installed.
    # System Python is required; never fall back to ambiguous shell existence tests.
    check = """import stat,sys
from pathlib import Path
def marker(path):
    if not path.is_absolute() or '..' in path.parts: raise OSError()
    for parent in reversed(path.parents):
        try: info=parent.lstat()
        except FileNotFoundError: return None
        if not stat.S_ISDIR(info.st_mode): raise OSError()
    try: return path.lstat()
    except FileNotFoundError: return None
try:
    info=marker(Path(sys.argv[1]))
    allowed=(info is not None and stat.S_ISREG(info.st_mode)) if sys.argv[2]=='managed' else info is None
except OSError:
    allowed=False
raise SystemExit(0 if allowed else 1)
"""
    return shlex.join(
        [
            "sudo",
            "python3",
            "-I",
            "-B",
            "-c",
            check,
            FENCE,
            "managed" if managed else "legacy",
        ]
    )


def preflight(hostname: str, root: str, *, managed: bool) -> None:
    # Read-only even for dry runs, and before provisioning, rsync, or any publication.
    _ssh(
        hostname,
        guard_command(managed=managed),
        "Host ownership does not match the selected deployment mode.",
    )
    if managed:
        _ssh(
            hostname,
            command(root, "status", extra=("--require-active",)),
            "Managed deployment requires active database ownership.",
        )


def validate(settings: Settings) -> None:
    origin = settings.control_plane.portal_origin
    if (
        not settings.control_plane.enabled
        or not settings.control_plane.portal_enabled
        or origin is None
    ):
        raise DeploymentError(
            "Managed deployment requires the private control plane and public Django portal."
        )
    if urlsplit(origin).hostname != settings.deployment.host.lower() or urlsplit(
        origin
    ).port not in {None, 443}:
        raise DeploymentError(
            "The public portal origin must use the deployment host on HTTPS port 443."
        )


def service_unit(root: str) -> str:
    return (
        private_unit(root)
        .replace("Private service administration", "Public subscription portal")
        .replace("control-plane --action serve", "managed-access --action serve")
        + "\n[Service]\nNice=10\nCPUWeight=10\nCPUQuota=50%\nMemoryMax=384M\n"
    )


def apply_node(hostname: str, root: str, server_dir: str, *, dry_run: bool) -> None:
    _ssh(
        hostname,
        command(
            root,
            "node-apply-current",
            privileged=True,
            extra=(
                "--binary",
                str(Path(server_dir) / "sing-box"),
                "--acknowledge-interrupt",
            ),
        ),
        "Managed node application failed or is uncertain. Retained plans require inspection.",
        dry_run=dry_run,
    )


def nginx_switch_script(old_port: int, new_port: int) -> str:
    """Keep certbot TLS directives; replace only the selected upstream and logging."""
    # File paths and ports are fixed or validated integers. No account data enters it.
    if any(
        type(port) is not int or not 1 <= port <= 65535 for port in (old_port, new_port)
    ):
        raise ValueError("Invalid nginx upstream port")
    return f"""import os,re,stat,subprocess,tempfile
from pathlib import Path
site=Path({NGINX_SITE_PATH!r})
if site.is_symlink() or not site.is_file(): raise SystemExit('Unexpected nginx site')
info=site.stat()
if info.st_uid!=0 or info.st_mode & 0o022 or info.st_nlink!=1 or info.st_size>1048576: raise SystemExit('Unsafe nginx site')
original=site.read_text()
headers=(('Host','$host'),('X-Real-IP','$remote_addr'),('X-Forwarded-For','$proxy_add_x_forwarded_for'),('X-Forwarded-Proto','$scheme'))
header_names='|'.join(name for name,value in headers)
cleaned=re.sub(r'(?mi)^[ \\t]*proxy_set_header[ \\t]+(?:'+header_names+r')[ \\t]+[^;\\n]+;[ \\t]*(?:#[^\\n]*)?$','',original)
if any(re.search(r'proxy_set_header\\s+(?:'+header_names+r')\\s',line.split('#',1)[0],re.I) for line in cleaned.splitlines()): raise SystemExit('Unsupported proxy headers')
if len(re.findall(r'(?m)^[ \\t]*proxy_pass\\s',cleaned))!=1: raise SystemExit('Unexpected nginx upstream')
pattern=r'(?m)^([ \\t]*)proxy_pass[ \\t]+http://127\\.0\\.0\\.1:(?:{old_port}|{new_port});[ \\t]*(?:#[^\\n]*)?$'
def upstream(match):
    indent=match[1]
    return indent+'proxy_pass http://127.0.0.1:{new_port};'+''.join('\\n'+indent+'proxy_set_header '+name+' '+value+';' for name,value in headers)
updated,count=re.subn(pattern,upstream,cleaned)
if count!=1: raise SystemExit('Unexpected nginx upstream')
# Request URIs and error messages can contain bearer credentials.
updated=re.sub(r'(?m)^[ \\t]*(?:access_log|error_log)\\s+[^;\\n]+;[ \\t]*(?:#[^\\n]*)?$','',updated)
if any(re.search(r'\\b(?:access_log|error_log)\\s',line.split('#',1)[0]) for line in updated.splitlines()): raise SystemExit('Unsupported nginx logging directive')
updated,servers=re.subn(r'(?m)^([ \\t]*server[ \\t]*\\{{)[ \\t]*$',r'\\1\\n    access_log off;\\n    error_log /dev/null;',updated)
if not servers: raise SystemExit('Unexpected nginx server block')
runs=Path({RUNS!r})
if runs.is_symlink() or not runs.is_dir() or runs.stat().st_uid!=0 or stat.S_IMODE(runs.stat().st_mode)!=0o700: raise SystemExit('Unsafe nginx recovery directory')
backup=Path(tempfile.mkdtemp(prefix='managed-nginx-',dir=runs))
(backup/'site.before.conf').write_text(original)
os.chmod(backup/'site.before.conf',0o600)
staged=backup/'site.conf'
staged.write_text(updated)
os.chmod(staged,0o600)
def sync_dir(path):
    fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try: os.fsync(fd)
    finally: os.close(fd)
for path in (backup/'site.before.conf',staged):
    with path.open('rb') as handle: os.fsync(handle.fileno())
sync_dir(backup)
sync_dir(backup.parent)
def publish(content):
    fd,name=tempfile.mkstemp(prefix='.managed-',dir=site.parent)
    with os.fdopen(fd,'w') as output:
        output.write(content)
        output.flush()
        os.fchmod(output.fileno(),stat.S_IMODE(info.st_mode))
        os.fchown(output.fileno(),info.st_uid,info.st_gid)
        os.fsync(output.fileno())
    os.replace(name,site)
    sync_dir(site.parent)
# Retain the before/after files. A failed check restores only nginx's old file.
publish(updated)
result=subprocess.run(['nginx','-t'],capture_output=True,timeout=30)
if result.returncode:
    publish(original)
    raise SystemExit('Nginx validation failed; prior site retained')
if subprocess.run(['systemctl','reload','nginx'],capture_output=True,timeout=30).returncode:
    raise SystemExit('Nginx reload is uncertain; inspect retained files')
"""


def finish(hostname: str, root: str, settings: Settings, *, dry_run: bool) -> None:
    validate(settings)
    preflight(hostname, root, managed=True)
    _ssh(
        hostname,
        "sudo systemctl disable --now sing-box-manager-web.service",
        "Failed to keep the legacy portal disabled.",
        dry_run=dry_run,
    )
    _install_unit(hostname, service_unit(root), SERVICE, dry_run=dry_run)
    _ssh(
        hostname,
        f"sudo systemctl daemon-reload && sudo systemctl enable {SERVICE} && sudo systemctl restart {SERVICE}",
        "Public Django portal startup failed.",
        dry_run=dry_run,
    )
    _ssh(
        hostname,
        command(root, "health"),
        "Public Django portal readiness failed.",
        dry_run=dry_run,
    )
    script = (
        "from pathlib import Path\n"
        "from sing_box_manager.account_copy.export import load_source_settings\n"
        "from sing_box_manager.managed_access.deployment import validate, nginx_switch_script\n"
        f"runtime=load_source_settings(Path({str(Path(root) / 'config/inventory/runtime.yaml')!r}))\n"
        "validate(runtime)\n"
        "exec(nginx_switch_script(runtime.web.port,runtime.control_plane.portal_port))\n"
    )
    _ssh(
        hostname,
        f"cd {shlex.quote(root)} && sudo env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 {shlex.quote(str(Path(root) / REMOTE_PIXI_PYTHON_RELATIVE_PATH))} - <<'SBM_NGINX'\n"
        + script
        + "SBM_NGINX",
        "Public routing update failed or is uncertain.",
        dry_run=dry_run,
    )
