"""Observe one KVM ACL grant and paused QEMU probe on a disposable hosted runner.

No guest image, network, fallback, second grant or second QEMU is used. Public
output is constructed from fixed labels, numeric metadata and permissions only.
Udev events and candidate rules do not identify an ACL writer.
"""
# ruff: noqa: INP001 -- standalone diagnostic, not a package

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

DEVICE = "/dev/kvm"
DEVPATH = "/devices/virtual/misc/kvm"
REF = "refs/heads/ci/kvm-acl-diagnostic"
WORKFLOW = "xiaosq2000/sing-box-manager/.github/workflows/kvm-access-probe.yml@" + REF
RULE_DIRS = (
    "/etc/udev/rules.d",
    "/run/udev/rules.d",
    "/usr/local/lib/udev/rules.d",
    "/usr/lib/udev/rules.d",
)
ACTIONS = {"add", "remove", "change", "move", "online", "offline", "bind", "unbind"}
PERMISSIONS = r"[r-][w-][x-]"
LIMIT = 200


def emit(record, **fields):
    print(json.dumps({"record": record, **fields}, sort_keys=True), flush=True)


def guard():
    """Prevent accidental execution outside the one explicitly authorized job."""
    expected = {
        "SBM_KVM_ACCESS_PROBE": "1",
        "GITHUB_ACTIONS": "true",
        "RUNNER_ENVIRONMENT": "github-hosted",
        "RUNNER_OS": "Linux",
        "GITHUB_EVENT_NAME": "push",
        "GITHUB_REF": REF,
        "GITHUB_REPOSITORY": "xiaosq2000/sing-box-manager",
        "GITHUB_WORKFLOW_REF": WORKFLOW,
    }
    return (
        sys.platform == "linux"
        and os.geteuid() != 0
        and all(os.environ.get(key) == value for key, value in expected.items())
    )


def command(argv, *, input_text=None, timeout=10):
    """Keep all command output private; callers project only approved fields."""
    try:
        result = subprocess.run(
            argv,
            input=input_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"status": "timeout"}, ""
    except OSError as error:
        return {"status": "os-error", "errno": error.errno}, ""
    return {
        "status": "ok" if result.returncode == 0 else "command-failed",
        "returncode": result.returncode,
        "stderr_present": bool(result.stderr),
    }, result.stdout


def permissions_intersection(entry, mask):
    return "".join(
        char if char == allowed else "-"
        for char, allowed in zip(entry, mask, strict=True)
    )


def acl_fields(text, uid):
    """Publish only this UID's entry and the mask, not other ACL identities."""
    entries = {}
    for line in text.splitlines():
        match = re.fullmatch(
            rf"(user:(?:{uid})?|group:|other:|mask:)"
            rf":({PERMISSIONS})(?:\s+#effective:{PERMISSIONS})?\s*",
            line,
        )
        if match:
            key, value = match.groups()
            if key in entries:
                return {"status": "invalid-acl"}
            entries[key] = value
        elif line.startswith((f"user:{uid}:", "mask::")):
            return {"status": "invalid-acl"}
    if not {"user:", "group:", "other:"} <= entries.keys():
        return {"status": "invalid-acl"}
    entry = entries.get(f"user:{uid}")
    mask = entries.get("mask:")
    if entry is not None and mask is None:
        return {"status": "invalid-acl"}
    return {
        "status": "ok",
        "uid_entry": entry,
        "mask": mask,
        "effective_uid_entry": permissions_intersection(entry, mask)
        if entry is not None
        else None,
    }


def snapshot():
    """These ordered stat/ACL/open observations are not an atomic snapshot."""
    result: dict[str, Any] = {
        "start_monotonic_ns": time.monotonic_ns(),
        "identity": {
            "uid": os.getuid(),
            "euid": os.geteuid(),
            "gid": os.getgid(),
            "egid": os.getegid(),
            "groups": sorted(os.getgroups()),
        },
    }
    try:
        info = Path(DEVICE).stat()
        result["device"] = {
            "status": "ok",
            "character_device": stat.S_ISCHR(info.st_mode),
            "inode": info.st_ino,
            "filesystem_device": info.st_dev,
            "major": os.major(info.st_rdev),
            "minor": os.minor(info.st_rdev),
            "mode": stat.S_IMODE(info.st_mode),
            "uid": info.st_uid,
            "gid": info.st_gid,
            "ctime_ns": info.st_ctime_ns,
        }
    except OSError as error:
        result["device"] = {"status": "os-error", "errno": error.errno}
    status, output = command(
        ["getfacl", "--omit-header", "--numeric", "--absolute-names", DEVICE]
    )
    result["acl"] = (
        acl_fields(output, os.geteuid()) if status["status"] == "ok" else status
    )
    try:
        descriptor = os.open(DEVICE, os.O_RDWR | os.O_CLOEXEC)
        os.close(descriptor)
        result["open_rdwr"] = {"status": "ok"}
    except OSError as error:
        result["open_rdwr"] = {"status": "os-error", "errno": error.errno}
    result["end_monotonic_ns"] = time.monotonic_ns()
    return result


def property_fields(text):
    values = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
    return {
        "device_matches": values.get("DEVNAME") == DEVICE,
        "devpath_matches": values.get("DEVPATH") == DEVPATH,
        "subsystem_matches": values.get("SUBSYSTEM") == "misc",
        "uaccess_tag": "uaccess" in values["TAGS"].split(":")
        if "TAGS" in values
        else None,
        "seat_tag": "seat" in values["TAGS"].split(":") if "TAGS" in values else None,
        "current_uaccess_tag": "uaccess" in values["CURRENT_TAGS"].split(":")
        if "CURRENT_TAGS" in values
        else None,
    }


def properties():
    status, output = command(
        ["udevadm", "info", "--query=property", "--name=" + DEVICE]
    )
    if status["status"] == "ok":
        status.update(property_fields(output))
        if not all(
            status[key]
            for key in ("device_matches", "devpath_matches", "subsystem_matches")
        ):
            status["status"] = "incomplete-properties"
    return status


def rule_lines(text):
    """Candidate statements, not proof of rule matching, order or execution."""
    result = []
    for number, statement in enumerate(text.splitlines(), 1):
        line = statement.split("#", 1)[0]
        if not re.search(r"\b(?:kvm|uaccess)\b", line):
            continue
        result.append(
            {
                "line": number,
                "mentions_kvm": bool(re.search(r"\bkvm\b", line)),
                "mentions_uaccess": bool(re.search(r"\buaccess\b", line)),
                "uaccess_builtin": bool(
                    re.search(r'RUN\{builtin\}\s*\+?=\s*"uaccess"', line)
                ),
                "assignments": [
                    key
                    for key in ("OWNER", "GROUP", "MODE", "TAG", "RUN", "SECLABEL")
                    if re.search(
                        rf"\b{key}(?:\{{[^}}]*\}})?\s*(?:\+=|-=|:=|=(?!=))", line
                    )
                ],
            }
        )
    return result


def read_rule(path):
    with path.open("rb") as stream:
        data = stream.read(1024 * 1024 + 1)
    if len(data) > 1024 * 1024:
        raise ValueError("oversized-rule-file")
    return data


def rules():
    result = {
        "status": "ok",
        "directories_present": 0,
        "read_errors": 0,
        "candidates": [],
    }
    for directory in RULE_DIRS:
        try:
            files = sorted(Path(directory).iterdir())
        except FileNotFoundError:
            continue
        except OSError:
            result["read_errors"] += 1
            continue
        result["directories_present"] += 1
        for path in files:
            if path.suffix != ".rules":
                continue
            try:
                data = read_rule(path)
            except (OSError, ValueError):
                result["read_errors"] += 1
                continue
            for fields in rule_lines(data.decode("utf-8", errors="replace")):
                if len(result["candidates"]) == LIMIT:
                    result["status"] = "truncated"
                    break
                result["candidates"].append(
                    {
                        "directory": directory,
                        "known_name": path.name
                        if path.name
                        in {
                            "50-udev-default.rules",
                            "70-uaccess.rules",
                            "73-seat-late.rules",
                        }
                        else "other",
                        "name_sha256": hashlib.sha256(path.name.encode()).hexdigest(),
                        "file_sha256": hashlib.sha256(data).hexdigest(),
                        **fields,
                    }
                )
    if result["status"] == "ok" and (
        result["read_errors"] or not result["directories_present"]
    ):
        result["status"] = "incomplete-rules"
    return result


def monitor_event(lines):
    if not lines:
        return None
    header = re.fullmatch(
        r"(KERNEL|UDEV)\s*\[\s*(\d+(?:\.\d+)?)\]\s+([a-z]+)\s+(\S+)\s+\(misc\)",
        lines[0],
    )
    values = dict(line.split("=", 1) for line in lines[1:] if "=" in line)
    if header is None:
        return (
            {"status": "invalid-kvm-event"}
            if values.get("DEVPATH") == DEVPATH
            else None
        )
    source, stamp, action, path = header.groups()
    if path != DEVPATH:
        return None
    sequence = values.get("SEQNUM", "")
    if (
        values.get("DEVPATH") != DEVPATH
        or values.get("DEVNAME", DEVICE) != DEVICE
        or action not in ACTIONS
        or not sequence.isascii()
        or not sequence.isdigit()
    ):
        return {"status": "invalid-kvm-event"}
    event = {
        "status": "ok",
        "source": "kernel" if source == "KERNEL" else "udev",
        "monitor_monotonic_seconds": float(stamp),
        "action": action,
        "sequence": int(sequence),
        "vm_event": values.get("EVENT")
        if values.get("EVENT") in {"create", "destroy"}
        else None,
        **property_fields("\n".join(lines[1:])),
    }
    for name in ("PID", "CREATED", "COUNT"):
        value = values.get(name, "")
        if value.isascii() and value.isdigit():
            event[name.lower()] = int(value)
    return event


class Monitor:
    def __init__(self):
        self.process = None
        self.thread = None
        self.ready = threading.Event()
        self.stopping = False
        self.events: list[dict[str, object]] = []
        self.result: dict[str, object] = {
            "status": "ok",
            "ready_banner": False,
            "events": self.events,
        }

    def start(self):
        try:
            self.process = subprocess.Popen(
                [
                    "sudo",
                    "-n",
                    "stdbuf",
                    "-oL",
                    "udevadm",
                    "monitor",
                    "--kernel",
                    "--udev",
                    "--property",
                    "--subsystem-match=misc",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError as error:
            self.result.update(status="os-error", errno=error.errno)
            return
        if self.process.stdout is None:
            self.result["status"] = "monitor-pipe-unavailable"
            return
        self.thread = threading.Thread(
            target=self.read, args=(self.process.stdout,), daemon=True
        )
        self.thread.start()
        if not self.ready.wait(5):
            self.result["status"] = "monitor-not-ready"

    def read(self, stream):
        lines = []
        banners = set()
        try:
            for raw_line in stream:
                line = raw_line.strip()
                if re.match(r"^(?:KERNEL|UDEV)\s+-", line):
                    banners.add(line.split()[0])
                    if banners == {"KERNEL", "UDEV"}:
                        self.result["ready_banner"] = True
                        self.ready.set()
                if re.match(r"^(?:KERNEL|UDEV)\s*\[", line):
                    self.append(lines)
                    lines = [line]
                elif not line:
                    self.append(lines)
                    lines = []
                elif lines and len(lines) < 128:
                    lines.append(line)
                elif lines:
                    self.result["status"] = "truncated"
            self.append(lines)
        except (OSError, ValueError):
            self.result["status"] = "monitor-read-error"
        if not self.stopping:
            self.result["status"] = "monitor-exited-early"

    def append(self, lines):
        event = monitor_event(lines)
        if event is not None:
            if event["status"] != "ok":
                self.result["status"] = "invalid-kvm-event"
            if len(self.events) == LIMIT:
                self.result["status"] = "truncated"
            else:
                self.events.append(
                    {"received_monotonic_ns": time.monotonic_ns(), **event}
                )

    def signal(self, name):
        process = self.process
        if process is None:
            return
        action = process.terminate if name == "TERM" else process.kill
        try:
            action()
        except ProcessLookupError:
            self.result["status"] = "monitor-exited-early"
        except PermissionError:
            status, _ = command(["sudo", "-n", "kill", "-" + name, str(process.pid)])
            if status["status"] != "ok":
                self.result["status"] = "monitor-signal-failed"

    def stop(self):
        if self.process is not None:
            if self.process.poll() is not None:
                self.result["status"] = "monitor-exited-early"
            self.stopping = True
            self.signal("TERM")
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.signal("KILL")
                self.process.wait(timeout=5)
                self.result["status"] = "monitor-stop-timeout"
            if self.thread is not None:
                self.thread.join(timeout=5)
                if self.thread.is_alive():
                    self.result["status"] = "monitor-reader-timeout"
                elif self.process.stdout is not None:
                    self.process.stdout.close()
            self.result["returncode"] = self.process.returncode
        return self.result


def qemu_probe():
    messages = [
        {"execute": "qmp_capabilities"},
        {"execute": "query-kvm", "id": "kvm-probe"},
        {"execute": "quit"},
    ]
    start = time.monotonic_ns()
    status, output = command(
        [
            "qemu-system-x86_64",
            "-no-user-config",
            "-machine",
            "accel=kvm",
            "-cpu",
            "max",
            "-m",
            "2048",
            "-smp",
            "2",
            "-nodefaults",
            "-nic",
            "none",
            "-display",
            "none",
            "-S",
            "-qmp",
            "stdio",
        ],
        input_text="".join(json.dumps(message) + "\n" for message in messages),
        timeout=15,
    )
    verified = False
    for line in output.splitlines():
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(message, dict) and message.get("id") == "kvm-probe":
            verified = message.get("return") == {"enabled": True, "present": True}
    return {
        **status,
        "query_kvm_enabled": verified,
        "start_monotonic_ns": start,
        "end_monotonic_ns": time.monotonic_ns(),
    }


def journal_summary(output):
    status = "ok"
    counts = {
        "matched_records": 0,
        "uaccess_mentions": 0,
        "acl_mentions": 0,
        "permission_mentions": 0,
    }
    for line in output.splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            status = "invalid-journal"
            continue
        message = entry.get("MESSAGE") if isinstance(entry, dict) else None
        if not isinstance(message, str):
            status = "invalid-journal"
            continue
        if not re.search(
            r"(?<![\w/-])(?:/dev/kvm|/devices/virtual/misc/kvm)(?:[^\w/-]|$)", message
        ):
            continue
        counts["matched_records"] += 1
        lowered = message.lower()
        for key, pattern in (
            ("uaccess_mentions", r"\buaccess\b"),
            ("acl_mentions", r"\bacl\b"),
            ("permission_mentions", r"\b(?:permission|denied)\b"),
        ):
            counts[key] += bool(re.search(pattern, lowered))
    if len(output.splitlines()) > LIMIT:
        status = "truncated"
    return {"status": status, **counts}


def journals(start, end):
    # A cursor-only read establishes journal access without reading an unrelated
    # message. --grep returns 1 for no matches, even with no journal files.
    availability, output = command(
        [
            "sudo",
            "-n",
            "journalctl",
            "--boot",
            "--no-pager",
            "--quiet",
            "--output=json",
            "--output-fields=__CURSOR",
            "--lines=1",
        ]
    )
    try:
        entry = json.loads(output)
        observed = isinstance(entry, dict) and isinstance(entry.get("__CURSOR"), str)
    except json.JSONDecodeError:
        observed = False
    accessible = (
        availability["status"] == "ok"
        and not availability.get("stderr_present")
        and observed
    )
    result: dict[str, Any] = {
        "availability": {"status": "ok" if accessible else "journal-unavailable"}
    }
    for source, selector in (
        ("kernel", ["--dmesg"]),
        ("udev", ["--unit=systemd-udevd.service"]),
        ("logind", ["--unit=systemd-logind.service"]),
    ):
        status, output = command(
            [
                "sudo",
                "-n",
                "journalctl",
                "--boot",
                "--no-pager",
                "--quiet",
                "--output=json",
                f"--lines={LIMIT + 1}",
                f"--since=@{start:.6f}",
                f"--until=@{end:.6f}",
                r"--grep=(^|[^[:alnum:]_/-])(/dev/kvm|/devices/virtual/misc/kvm)([^[:alnum:]_/-]|$)",
                *selector,
            ]
        )
        no_matches = (
            accessible
            and status.get("returncode") == 1
            and not output
            and not status.get("stderr_present")
        )
        if (
            status["status"] == "ok" and not status.get("stderr_present")
        ) or no_matches:
            result[source] = {
                **journal_summary(output),
                "query_returncode": status.get("returncode"),
                "no_matches": no_matches,
            }
        else:
            result[source] = {**status, "status": "journal-query-failed"}
    return result


def comparison(before, after):
    available = before["acl"]["status"] == after["acl"]["status"] == "ok"
    return {
        key + "_changed": before["acl"][key] != after["acl"][key] if available else None
        for key in ("uid_entry", "mask", "effective_uid_entry")
    } | {
        "rdwr_access_lost": before["open_rdwr"]["status"] == "ok"
        and after["open_rdwr"]["status"] != "ok",
        "device_metadata_changed": before["device"] != after["device"],
        "process_identity_changed": before["identity"] != after["identity"],
    }


def run():
    start = time.time()
    monitor = Monitor()
    try:
        monitor.start()
        rule_evidence = rules()
        emit("candidate-rules", **rule_evidence)
        initial_properties = properties()
        emit("properties", stage="before-grant", **initial_properties)
        baseline = snapshot()
        emit("snapshot", stage="before-grant", **baseline)
        if not baseline["device"].get("character_device", False):
            emit(
                "result",
                success=False,
                reason="kvm-character-device-unavailable",
                coverage_complete=False,
            )
            return 1
        grant, _ = command(
            ["sudo", "-n", "setfacl", "-m", f"u:{os.geteuid()}:rw", DEVICE]
        )
        emit("grant", **grant, end_monotonic_ns=time.monotonic_ns())
        before = snapshot()
        emit("snapshot", stage="before-probe", **before)
        probe = qemu_probe()
        emit("qmp-probe", **probe)
        after = snapshot()
        emit("snapshot", stage="after-probe", **after)
        # Bounded event drain only; never another grant, open, QEMU or retry.
        time.sleep(1)
        final_properties = properties()
        emit("properties", stage="after-probe", **final_properties)
    finally:
        event_evidence = monitor.stop()
        emit("events", **event_evidence)
    end = time.time()
    log_evidence = journals(start, end)
    emit(
        "journal-summary",
        window_start_realtime=start,
        window_end_realtime=end,
        **log_evidence,
    )
    delta = comparison(before, after)
    emit("comparison", **delta)
    coverage = all(
        item["status"] == "ok"
        for item in (
            rule_evidence,
            initial_properties,
            final_properties,
            event_evidence,
            *log_evidence.values(),
            *(
                item[key]
                for item in (baseline, before, after)
                for key in ("acl", "device")
            ),
        )
    )
    success = (
        coverage
        and grant["status"] == probe["status"] == "ok"
        and probe["query_kvm_enabled"]
        and all(
            item["open_rdwr"]["status"] == "ok"
            and item["acl"].get("effective_uid_entry") == "rw-"
            for item in (before, after)
        )
        and not any(delta.values())
    )
    emit(
        "result",
        success=success,
        coverage_complete=coverage,
        grant_succeeded=grant["status"] == "ok",
        qmp_succeeded=probe["status"] == "ok" and probe["query_kvm_enabled"],
        correlation_is_not_writer_attribution=True,
    )
    return 0 if success else 1


def main():
    if not guard():
        emit(
            "guard",
            allowed=False,
            reason="requires-opt-in-nonroot-hosted-diagnostic-push",
        )
        return 2
    try:
        return run()
    except (Exception, KeyboardInterrupt):  # Never publish raw errors or paths.
        emit(
            "result", success=False, reason="diagnostic-error", coverage_complete=False
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
