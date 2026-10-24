"""Run the startup subset of G1 inside the existing disposable G0 guest."""

import json
import shlex

STATE = "/var/lib/sbm-node-g0"
ONCE = (
    "sudo /opt/sbm-node-g0/sbm-node-g0 run --once "
    "--identity /etc/sbm-node-g0/identity.json "
    f"--state {STATE} --engine /opt/sbm-node-g0/sing-box"
)


def python(guest, source):
    return guest("sudo /usr/bin/python3 -c " + shlex.quote(source))


def reject_once(guest, log, message):
    """Require the agent's exact failure, not an SSH or harness failure."""
    try:
        guest(ONCE, log=log)
    except RuntimeError as error:
        if (
            getattr(error, "returncode", None) == 1
            and log.read_bytes() == f"G0 failed: {message}\n".encode()
        ):
            return
        raise RuntimeError("startup negative control failed unexpectedly") from None
    raise RuntimeError("startup negative control accepted invalid state")


def observe(guest):
    return json.loads(
        python(
            guest,
            f"""
import hashlib,json,os,pathlib,subprocess
state=pathlib.Path({STATE!r})
current=(state/'current').resolve(strict=True)
bundle=json.loads((current/'bundle.json').read_bytes())
receipt=json.loads((state/'running.json').read_bytes())
properties=dict(line.split('=',1) for line in subprocess.check_output([
    'systemctl','show','sbm-g0-proxy','--property=ActiveState',
    '--property=MainPID','--property=InvocationID'],text=True).splitlines())
pid=int(properties['MainPID'])
digest=hashlib.sha256((current/'config.json').read_bytes()).hexdigest()
assert properties['ActiveState']=='active' and pid>0
assert receipt==dict(generation=bundle['generation'],config_sha256=digest,
    invocation=properties['InvocationID'],pid=pid)
assert digest==bundle['config_sha256']
assert current==state/'generations'/str(bundle['generation'])
actual=os.stat('/proc/%d/exe'%pid)
installed=os.stat('/opt/sbm-node-g0/sing-box')
assert (actual.st_dev,actual.st_ino)==(installed.st_dev,installed.st_ino)
argv=pathlib.Path('/proc/%d/cmdline'%pid).read_bytes().split(b'\\0')
assert argv[1:-1]==[b'run',b'-D',bytes(state),b'-c',bytes(current/'config.json')]
print(json.dumps(dict(receipt=receipt,current=str(current),config_sha256=digest,
    systemd=properties,executable=os.readlink('/proc/%d/exe'%pid),
    process_file=[actual.st_dev,actual.st_ino],installed_file=[installed.st_dev,installed.st_ino],
    argv=[x.decode() for x in argv[:-1]])))
""",
        )
    )


def startup_checks(*, guest, root, wait, write, control, report, fetch, next_bundle):
    """Return added payload bytes and passed checks. Keep all identities private."""
    guest("sudo systemctl stop sbm-g0-agent")
    original = observe(guest)
    write(root / "startup-before.json", json.dumps(original))
    receipt = original["receipt"]

    def restore():
        python(
            guest,
            f"import pathlib; pathlib.Path('{STATE}/running.json').write_text({json.dumps(receipt)!r})",
        )

    for field, value in (
        ("config_sha256", "0" * 64),
        ("invocation", "0" * 32),
    ):
        corrupted = {**receipt, field: value}
        try:
            python(
                guest,
                f"import pathlib; pathlib.Path('{STATE}/running.json').write_text({json.dumps(corrupted)!r})",
            )
            reject_once(
                guest,
                root / f"startup-reject-{field}.log",
                "engine startup identity unconfirmed",
            )
        finally:
            restore()
    if observe(guest) != original:
        raise RuntimeError("identity rejection changed the running engine")

    guest("sudo systemctl restart sbm-g0-proxy")
    # Type=simple can report active before the wrapper finishes exec.
    wait(
        lambda: (
            guest(
                "sudo readlink /proc/$(systemctl show sbm-g0-proxy --property=MainPID --value)/exe"
            ).strip()
            == b"/opt/sbm-node-g0/sing-box"
        )
    )
    restarted = observe(guest)
    write(root / "startup-restarted.json", json.dumps(restarted))
    if (
        restarted["receipt"]["invocation"] == receipt["invocation"]
        or restarted["receipt"]["generation"] != receipt["generation"]
        or restarted["receipt"]["config_sha256"] != receipt["config_sha256"]
    ):
        raise RuntimeError("service restart did not preserve the selected config")
    extra_bytes = fetch()
    guest(ONCE, log=root / "startup-restart-cycle.log")
    if report()["nodes"][0]["invocation"] != restarted["receipt"]["invocation"]:
        raise RuntimeError("restarted invocation was not reported")

    holder = f"""
import fcntl,pathlib,time
lock=open('{STATE}/lifecycle.lock','a')
fcntl.flock(lock,fcntl.LOCK_EX)
pathlib.Path('{STATE}/lock-holder-ready').touch()
time.sleep(60)
"""
    guest(
        "sudo systemd-run --quiet --unit=sbm-g0-lock-holder /usr/bin/python3 -c "
        + shlex.quote(holder)
    )
    try:
        wait(
            lambda: (
                python(
                    guest,
                    f"import pathlib; print(pathlib.Path('{STATE}/lock-holder-ready').exists())",
                ).strip()
                == b"True"
            )
        )
        write(
            root / "bundle-next.json",
            json.dumps(next_bundle, sort_keys=True, separators=(",", ":")),
        )
        control("publish")
        reject_once(
            guest, root / "startup-lock-busy.log", "lifecycle lock wait expired"
        )
        if (
            observe(guest) != restarted
            or report()["nodes"][0]["applied_generation"] != 2
        ):
            raise RuntimeError("lock contention changed the running policy")
    finally:
        guest("sudo systemctl stop sbm-g0-lock-holder")
    # This starts the real wrapper. Holding the lifecycle lock across start deadlocks.
    guest(ONCE, log=root / "startup-next-cycle.log")
    applied = observe(guest)
    write(root / "startup-generation-3.json", json.dumps(applied))
    if (
        applied["receipt"]["generation"] != 3
        or applied["receipt"]["config_sha256"] == restarted["receipt"]["config_sha256"]
        or applied["receipt"]["invocation"] == restarted["receipt"]["invocation"]
        or report()["nodes"][0]["applied_generation"] != 3
    ):
        raise RuntimeError("generation did not recover after lock release")

    guest("sudo systemctl start sbm-g0-agent")
    probe = f"""
import fcntl
lock=open('{STATE}/agent.lock','a')
try:
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    print('free')
except BlockingIOError:
    print('held')
"""
    wait(lambda: python(guest, probe).strip() == b"held")
    reject_once(
        guest, root / "startup-duplicate-writer.log", "agent writer already active"
    )
    if observe(guest) != applied:
        raise RuntimeError("duplicate writer changed the running engine")
    extra_bytes += fetch()
    return extra_bytes, [
        "native receipt digest and invocation rejection",
        "service restart with loaded-config and process identity",
        "lifecycle contention without stop or pointer publication",
        "generation 3 after lock release without startup deadlock",
        "duplicate agent writer rejection",
    ]
