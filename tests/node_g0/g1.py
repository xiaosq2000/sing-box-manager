"""Single-node fault checks using only the existing private controller and guest."""

import contextlib
import hashlib
import json

from tests.node_g0.reliability import (
    ONCE,
    STATE,
    observe,
    python,
    reject_once,
    wait_proxy,
)


def local_state(guest):
    return json.loads(
        python(
            guest,
            f"""
import hashlib,json,sqlite3,pathlib
state=pathlib.Path({STATE!r})
source=json.loads(pathlib.Path('/etc/sbm-node-g0/identity.json').read_bytes())['source_id']
db=sqlite3.connect('file:'+str(state/'traffic.sqlite3')+'?mode=ro',uri=True)
db.execute('BEGIN')
rows=[]
for credential,day,version,upload,download in db.execute('SELECT credential,day,version,upload,download FROM usage ORDER BY day,credential'):
    value=dict(source_id=source,credential_id=credential,usage_date=day,version=version,upload=upload,download=download)
    value['digest']=hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    rows.append(value)
print(json.dumps(dict(source_id=source,usage=rows,
    checkpoints=db.execute('SELECT invocation,credential,upload,download FROM checkpoints ORDER BY invocation,credential').fetchall(),
    finished=db.execute('SELECT invocation FROM finished ORDER BY invocation').fetchall(),
    gaps=db.execute('SELECT invocation,reason FROM gaps ORDER BY invocation').fetchall())))
""",
        )
    )


def downloaded(state):
    return sum(row["download"] for row in state["usage"])


def reliability_checks(
    *, guest, root, wait, write, control, report, fetch, bundle, controller, database
):
    checks = []
    added = 0

    def step(name):
        print(f"G1 check: {name}", flush=True)

    def retain(name, value):
        write(root / f"g1-{name}.json", json.dumps(value, sort_keys=True))

    def settle(name):
        guest("sudo systemctl stop sbm-g0-agent")
        guest(ONCE, log=root / f"g1-{name}-cycle.log")
        local, central = local_state(guest), report()
        expected = sorted(
            central["usage"], key=lambda row: (row["usage_date"], row["credential_id"])
        )
        if (
            local["usage"] != expected
            or central["nodes"][0]["source_id"] != local["source_id"]
        ):
            raise RuntimeError("keyed accounting evidence differs")
        if central["nodes"][0]["coverage_gaps"] != len(local["gaps"]):
            raise RuntimeError("accounting coverage warning was not reported")
        retain(name, {"local": local, "central": central, "running": observe(guest)})
        return local

    def publish(change=None):
        generation = report()["nodes"][0]["generation"] + 1
        candidate = bundle(generation)
        if change:
            change(candidate["config"])
        candidate["config_sha256"] = hashlib.sha256(
            json.dumps(
                candidate["config"], sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        write(
            root / "bundle-next.json",
            json.dumps(candidate, sort_keys=True, separators=(",", ":")),
        )
        control("publish")
        return generation

    def wait_engine():
        probe = """
import os,socket,subprocess
pid=subprocess.check_output(['systemctl','show','sbm-g0-proxy','--property=MainPID','--value'],text=True).strip()
ready=False
try:
    if os.path.samefile('/proc/'+pid+'/exe','/opt/sbm-node-g0/sing-box'):
        with socket.create_connection(('127.0.0.1',18080),timeout=0.25):
            ready=True
except OSError:
    pass
print(ready)
"""
        wait(lambda: python(guest, probe).strip() == b"True")
        wait_proxy(guest, wait)

    def same_running(before):
        if observe(guest) != before:
            raise RuntimeError("fault changed the selected running engine")

    def traffic():
        nonlocal added
        count = fetch()
        added += count
        return count

    # This rule affects only the private guest's loopback counter listener.
    rule = "-d 127.0.0.1 -p tcp --dport 19080 -j REJECT"

    @contextlib.contextmanager
    def counter_blocked():
        guest("sudo iptables -w -I OUTPUT 1 " + rule)
        try:
            yield
        finally:
            guest("sudo iptables -w -D OUTPUT " + rule)

    step("counter readiness and final sampling")
    before = settle("initial")
    running = observe(guest)
    guest("sudo iptables -w -I OUTPUT 1 " + rule)
    # A real guest timer makes the API reachable during the bounded RPC call.
    guest(
        "sudo systemd-run --quiet --unit=sbm-g0-counter-release --on-active=1s --timer-property=AccuracySec=1ms /usr/sbin/iptables -w -D OUTPUT "
        + rule
    )
    try:
        guest(ONCE, log=root / "g1-counter-delayed.log")
    finally:
        write(
            root / "g1-counter-release.log",
            guest("sudo journalctl -u sbm-g0-counter-release --no-pager"),
        )
    same_running(running)
    checks.append("native delayed counter readiness without engine restart")
    before = local_state(guest)
    payload = (
        traffic()
    )  # No agent is running. Only final collection can observe these bytes.
    with counter_blocked():
        reject_once(
            guest,
            root / "g1-counter-unavailable.log",
            "counter sample failed: counter API deadline exceeded",
        )
        if local_state(guest) != before:
            raise RuntimeError("failed counter sample changed durable accounting")
        same_running(running)
        generation = publish()
        reject_once(
            guest,
            root / "g1-final-sample-failed.log",
            "final sample failed: counter sample failed: counter API deadline exceeded",
        )
        same_running(running)
        if report()["nodes"][0]["applied_generation"] == generation:
            raise RuntimeError("failed final sample acknowledged new policy")
    guest(ONCE, log=root / "g1-final-sample-retry.log")
    after = settle("final-sample")
    old = running["receipt"]["invocation"]
    old_before = next(row[3] for row in before["checkpoints"] if row[0] == old)
    old_after = next(row[3] for row in after["checkpoints"] if row[0] == old)
    if (
        old_after < old_before + payload
        or [old] not in after["finished"]
        or after["gaps"] != before["gaps"]
    ):
        raise RuntimeError("replacement did not commit the final old counters")
    checks.extend(
        [
            "unavailable counter rejects sample and ordinary replacement",
            "final counters commit before confirmed engine stop",
        ]
    )

    step("invalid config and failed stop")
    running = observe(guest)
    publish(lambda config: config["inbounds"][0].update(type="g1-invalid-protocol"))
    reject_once(guest, root / "g1-invalid-config.log", "config check failed")
    same_running(running)
    publish()
    # MainPID=0 is insufficient when a real child survives in the service cgroup.
    dropin = "/etc/systemd/system/sbm-g0-proxy.service.d"
    python(
        guest,
        f"import pathlib; p=pathlib.Path('{dropin}'); p.mkdir(exist_ok=True); (p/'g1-stop.conf').write_text('[Service]\\nKillMode=process\\n')",
    )
    guest("sudo systemctl daemon-reload")
    python(
        guest,
        f"""
import pathlib,subprocess
unit=subprocess.check_output(['systemctl','show','sbm-g0-proxy','--property=ControlGroup','--value'],text=True).strip()
assert unit.startswith('/system.slice/')
child=subprocess.Popen(['/usr/bin/sleep','600'],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
pathlib.Path('/sys/fs/cgroup'+unit+'/cgroup.procs').write_text(str(child.pid))
pathlib.Path('{STATE}/stop-child-pid').write_text(str(child.pid))
""",
    )
    try:
        reject_once(
            guest, root / "g1-stop-failed.log", "engine cgroup termination unconfirmed"
        )
        selected = python(
            guest,
            f"import pathlib; print((pathlib.Path('{STATE}')/'current').resolve().name)",
        ).strip()
        if selected != str(running["receipt"]["generation"]).encode():
            raise RuntimeError("failed stop published a different generation")
        retain(
            "stop-blocked",
            json.loads(
                python(
                    guest,
                    """
import json,pathlib,subprocess
unit=subprocess.check_output(['systemctl','show','sbm-g0-proxy','--property=ControlGroup','--value'],text=True).strip()
print(json.dumps(dict(group=unit,remaining_pids=pathlib.Path('/sys/fs/cgroup'+unit+'/cgroup.procs').read_text().split(),main_pid=subprocess.check_output(['systemctl','show','sbm-g0-proxy','--property=MainPID','--value'],text=True).strip())))
""",
                )
            ),
        )
    finally:
        guest("sudo systemctl kill --kill-whom=all --signal=KILL sbm-g0-proxy")
        python(
            guest,
            f"import pathlib; p=pathlib.Path('{dropin}/g1-stop.conf'); p.rename(p.with_suffix('.aside'))",
        )
        guest("sudo systemctl daemon-reload")
    settle("stop-recovery")
    checks.extend(
        [
            "invalid config leaves the working process and pointer unchanged",
            "failed systemd stop prevents publication and permits local recovery",
        ]
    )

    step("runtime startup failure and forward repair")
    old_generation = observe(guest)["receipt"]["generation"]
    failed_generation = publish(
        lambda config: config["inbounds"][0].update(listen_port=18080)
    )
    # The guest HTTP target already owns 18080. sing-box check succeeds, run fails.
    reject_once(
        guest,
        root / "g1-runtime-start-failed.log",
        (
            "published startup identity unconfirmed",
            "engine startup identity unconfirmed",
            "counter sample failed: counter API deadline exceeded",
        ),
    )
    wait(
        lambda: (
            guest("systemctl show sbm-g0-proxy --property=Result --value").strip()
            in (b"exit-code", b"start-limit-hit")
        )
    )
    write(
        root / "g1-runtime-start-journal.log",
        guest("sudo journalctl -u sbm-g0-proxy --no-pager -n 80"),
    )
    selected = python(
        guest,
        f"import pathlib; print((pathlib.Path('{STATE}')/'current').resolve().name)",
    ).strip()
    if (
        selected != str(failed_generation).encode()
        or report()["nodes"][0]["applied_generation"] != old_generation
    ):
        raise RuntimeError("failed startup rolled back policy or fabricated success")
    repaired_generation = publish()
    guest("sudo systemctl start sbm-g0-agent")
    wait(lambda: report()["nodes"][0]["applied_generation"] == repaired_generation)
    settle("forward-repair")
    if observe(guest)["receipt"]["generation"] != repaired_generation:
        raise RuntimeError("newer policy did not repair startup")
    traffic()
    checks.append(
        "runtime-invalid published policy repairs only through a newer generation"
    )

    step("HTTPS, PostgreSQL and authentication outage autonomy")
    for fault in ("https", "postgres", "authentication"):
        before = settle(f"before-{fault}")
        running = observe(guest)
        guest("sudo systemctl start sbm-g0-agent")
        if fault == "https":
            controller(False)
        elif fault == "postgres":
            database(False)
        else:
            write(root / "deny-auth", b"fixture authorization failure\n")
        try:
            count = traffic()
            wait(lambda: downloaded(local_state(guest)) >= downloaded(before) + count)
            same_running(running)
        finally:
            if fault == "https":
                controller(True)
            elif fault == "postgres":
                database(True)
            else:
                (root / "deny-auth").rename(root / "deny-auth.aside")
        after = settle(f"after-{fault}")
        if after["source_id"] != before["source_id"] or after["gaps"] != before["gaps"]:
            raise RuntimeError(
                "control-plane outage changed source or invented engine loss"
            )
    checks.append(
        "proxy and local accounting survive HTTPS, database and authorization outages"
    )

    step("agent crash, engine crash and replay")
    before = settle("before-agent-crash")
    running = observe(guest)
    guest("sudo systemctl start sbm-g0-agent")
    agent_pid = guest("systemctl show sbm-g0-agent --property=MainPID --value").strip()
    guest("sudo systemctl kill --kill-whom=main --signal=KILL sbm-g0-agent")
    wait(
        lambda: (
            guest("systemctl show sbm-g0-agent --property=MainPID --value").strip()
            not in (b"0", agent_pid)
        )
    )
    same_running(running)
    after = settle("after-agent-crash")
    if after["usage"] != before["usage"] or after["source_id"] != before["source_id"]:
        raise RuntimeError("agent crash replayed committed usage")
    guest("sudo systemctl start sbm-g0-agent")
    guest("sudo systemctl kill --kill-whom=main --signal=KILL sbm-g0-proxy")
    wait(
        lambda: (
            guest("systemctl show sbm-g0-proxy --property=InvocationID --value").strip()
            not in (b"", running["receipt"]["invocation"].encode())
        )
    )
    wait_proxy(guest, wait)
    traffic()
    after = settle("after-engine-crash")
    if not any(row[0] == running["receipt"]["invocation"] for row in after["gaps"]):
        raise RuntimeError("engine crash did not report an unobserved tail")
    checks.append(
        "agent crash preserves committed usage; engine crash records uncertainty"
    )

    step("crash before pointer publication")
    before = settle("before-publication-crash")
    running = observe(guest)
    # Delay only the fixture restart to retain and inspect the crash boundary.
    python(
        guest,
        "import pathlib; p=pathlib.Path('/etc/systemd/system/sbm-g0-agent.service.d'); p.mkdir(exist_ok=True); (p/'g1-delay.conf').write_text('[Service]\\nRestartSec=5\\n')",
    )
    hook = f"""#!/bin/sh
if [ -f {STATE}/crash-before-armed ]; then
    mv {STATE}/crash-before-armed {STATE}/crash-before-consumed
    readlink {STATE}/current > {STATE}/crash-before-pointer
    systemctl show sbm-g0-agent --property=InvocationID --value > {STATE}/crash-before-invocation
    systemctl kill --kill-whom=main --signal=KILL sbm-g0-agent
fi
"""
    python(
        guest,
        f"import pathlib; p=pathlib.Path('/opt/sbm-node-g0/g1-before'); p.write_text({hook!r}); p.chmod(0o700); pathlib.Path('{STATE}/crash-before-armed').touch(); pathlib.Path('{dropin}/g1-before.conf').write_text('[Service]\\nExecStopPost=/opt/sbm-node-g0/g1-before\\n')",
    )
    guest("sudo systemctl daemon-reload")
    desired = publish()
    guest("sudo systemctl start sbm-g0-agent")
    wait(
        lambda: (
            python(
                guest,
                f"import pathlib; print(pathlib.Path('{STATE}/crash-before-pointer').exists())",
            ).strip()
            == b"True"
        )
    )
    controller(False)
    try:
        old_agent = guest(f"sudo cat {STATE}/crash-before-invocation").strip()
        if not old_agent:
            raise RuntimeError("crash hook lacks agent identity")
        wait(
            lambda: (
                guest(
                    "systemctl show sbm-g0-agent --property=InvocationID --value"
                ).strip()
                not in (b"", old_agent)
            )
        )
        retained = guest(f"sudo readlink {STATE}/current").strip()
        if not retained.endswith(b"/" + str(running["receipt"]["generation"]).encode()):
            raise RuntimeError("pre-publication crash selected pending policy")
        wait_engine()
        count = traffic()
        wait(lambda: downloaded(local_state(guest)) >= downloaded(before) + count)
        if observe(guest)["receipt"]["generation"] != running["receipt"]["generation"]:
            raise RuntimeError("offline recovery changed selected generation")
    finally:
        controller(True)
        python(
            guest,
            f"import pathlib; p=pathlib.Path('{dropin}/g1-before.conf'); p.rename(p.with_suffix('.aside'))",
        )
        guest("sudo systemctl daemon-reload")
    wait(lambda: report()["nodes"][0]["applied_generation"] == desired)
    settle("after-publication-crash")
    checks.append(
        "pre-publication agent crash recovers the old durable pointer while offline"
    )

    step("crash after durable pointer publication")
    hook = f"""#!/bin/sh
if [ -f {STATE}/crash-after-armed ]; then
    mv {STATE}/crash-after-armed {STATE}/crash-after-consumed
    readlink {STATE}/current > {STATE}/crash-after-pointer
    systemctl show sbm-g0-agent --property=InvocationID --value > {STATE}/crash-after-invocation
    systemctl kill --kill-whom=main --signal=KILL sbm-g0-agent
fi
"""
    python(
        guest,
        f"import pathlib; p=pathlib.Path('/opt/sbm-node-g0/g1-after'); p.write_text({hook!r}); p.chmod(0o700); pathlib.Path('{STATE}/crash-after-armed').touch(); pathlib.Path('{dropin}/g1-after.conf').write_text('[Service]\\nExecStartPre=/opt/sbm-node-g0/g1-after\\n')",
    )
    guest("sudo systemctl daemon-reload")
    desired = publish()
    guest("sudo systemctl start sbm-g0-agent")
    wait(
        lambda: (
            python(
                guest,
                f"import pathlib; print(pathlib.Path('{STATE}/crash-after-pointer').exists())",
            ).strip()
            == b"True"
        )
    )
    retained = guest(f"sudo cat {STATE}/crash-after-pointer").strip()
    if not retained.endswith(b"/" + str(desired).encode()):
        raise RuntimeError(
            "post-publication hook did not observe the durable new pointer"
        )
    old_agent = guest(f"sudo cat {STATE}/crash-after-invocation").strip()
    if not old_agent:
        raise RuntimeError("crash hook lacks agent identity")
    wait(
        lambda: (
            guest("systemctl show sbm-g0-agent --property=InvocationID --value").strip()
            not in (b"", old_agent)
        )
    )
    wait(lambda: report()["nodes"][0]["applied_generation"] == desired)
    after = settle("after-durable-publication-crash")
    if observe(guest)["receipt"]["generation"] != desired:
        raise RuntimeError("post-publication crash rolled policy backward")
    python(
        guest,
        f"import pathlib; p=pathlib.Path('{dropin}/g1-after.conf'); p.rename(p.with_suffix('.aside')); p=pathlib.Path('/etc/systemd/system/sbm-g0-agent.service.d/g1-delay.conf'); p.rename(p.with_suffix('.aside'))",
    )
    guest("sudo systemctl daemon-reload")
    checks.append("post-publication agent crash recovers only the new durable pointer")

    step("guest OS reboot with controller offline")
    before = settle("before-os-reboot")
    running = observe(guest)
    boot = guest("cat /proc/sys/kernel/random/boot_id").strip()
    guest("sudo systemctl start sbm-g0-agent")
    controller(False)
    try:
        # A successful submission is not reboot evidence. A changed boot ID is required.
        guest("sudo systemctl reboot --no-block", log=root / "g1-reboot-request.log")

        def rebooted():
            try:
                return guest("cat /proc/sys/kernel/random/boot_id").strip() not in (
                    b"",
                    boot,
                )
            except RuntimeError:
                return False

        wait(rebooted, seconds=180)
        wait_engine()
        after_boot = observe(guest)
        if (
            after_boot["receipt"]["generation"] != running["receipt"]["generation"]
            or after_boot["receipt"]["invocation"] == running["receipt"]["invocation"]
        ):
            raise RuntimeError("reboot did not recover the selected local policy")
        count = traffic()
        wait(lambda: downloaded(local_state(guest)) >= downloaded(before) + count)
        retain(
            "offline-reboot",
            {
                "before_boot_id": boot.decode(),
                "after_boot_id": guest("cat /proc/sys/kernel/random/boot_id")
                .decode()
                .strip(),
                "running": after_boot,
                "local": local_state(guest),
            },
        )
    finally:
        controller(True)
    after = settle("after-os-reboot")
    if after["source_id"] != before["source_id"]:
        raise RuntimeError("reboot changed the registered accounting source")
    # A second identical sample must not increment a bucket version or add usage.
    repeated = settle("replay-after-reboot")
    if repeated["usage"] != after["usage"]:
        raise RuntimeError("reboot recovery duplicated accounting")
    checks.append(
        "real guest reboot recovers local policy offline and preserves source/checkpoints"
    )
    guest("sudo systemctl start sbm-g0-agent")
    retain("checks", checks)
    return added, checks
