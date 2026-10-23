"""Fixture-only KVM diagnostic checks: no real device, sudo, monitor or VM.

Standard-library unittest also runs on a hosted runner without a Pixi install.
"""

import contextlib
import copy
import errno
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "kvm_access_probe", ROOT / "scripts/dev/kvm-access-probe.py"
)
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)
PRIVATE = "PRIVATE-fixture-credential-and-path"
ACL = "user::rw-\nuser:1001:rw-\ngroup::rw-\nmask::rw-\nother::---\n"
PROPERTIES = (
    "DEVNAME=/dev/kvm\nDEVPATH=/devices/virtual/misc/kvm\nSUBSYSTEM=misc\n"
    "TAGS=:seat:uaccess:\nCURRENT_TAGS=:uaccess:\n"
)


class KvmAccessProbeTests(unittest.TestCase):
    def environment(self):
        return {
            "SBM_KVM_ACCESS_PROBE": "1",
            "GITHUB_ACTIONS": "true",
            "RUNNER_ENVIRONMENT": "github-hosted",
            "RUNNER_OS": "Linux",
            "GITHUB_EVENT_NAME": "push",
            "GITHUB_REF": probe.REF,
            "GITHUB_REPOSITORY": "xiaosq2000/sing-box-manager",
            "GITHUB_WORKFLOW_REF": probe.WORKFLOW,
        }

    def test_guard_requires_every_hosted_runner_opt_in_field_and_nonroot(self):
        with (
            patch.object(probe.sys, "platform", "linux"),
            patch.object(probe.os, "geteuid", return_value=1001),
        ):
            with patch.dict(os.environ, self.environment(), clear=True):
                self.assertTrue(probe.guard())
            for key in self.environment():
                for value in (None, PRIVATE):
                    env = self.environment()
                    if value is None:
                        del env[key]
                    else:
                        env[key] = value
                    with (
                        self.subTest(key=key, value=value),
                        patch.dict(os.environ, env, clear=True),
                        patch.object(probe, "run", side_effect=AssertionError("ran")),
                    ):
                        output = io.StringIO()
                        with contextlib.redirect_stdout(output):
                            self.assertEqual(probe.main(), 2)
                        self.assertNotIn(PRIVATE, output.getvalue())
            with (
                patch.dict(os.environ, self.environment(), clear=True),
                patch.object(probe.os, "geteuid", return_value=0),
            ):
                self.assertFalse(probe.guard())
            with (
                patch.dict(os.environ, self.environment(), clear=True),
                patch.object(probe.sys, "platform", "darwin"),
            ):
                self.assertFalse(probe.guard())

    def test_acl_separates_entry_mask_and_effective_permissions(self):
        self.assertEqual(
            probe.acl_fields(ACL, 1001),
            {
                "status": "ok",
                "uid_entry": "rw-",
                "mask": "rw-",
                "effective_uid_entry": "rw-",
            },
        )
        reduced = ACL.replace("mask::rw-", "mask::r--")
        reduced = reduced.replace("user:1001:rw-", "user:1001:rw-\t#effective:r--")
        result = probe.acl_fields(reduced + "user:2000:rwx\n# " + PRIVATE, 1001)
        self.assertEqual(result["uid_entry"], "rw-")
        self.assertEqual(result["mask"], "r--")
        self.assertEqual(result["effective_uid_entry"], "r--")
        removed = probe.acl_fields(ACL.replace("user:1001:rw-\n", ""), 1001)
        self.assertIsNone(removed["uid_entry"])
        self.assertEqual(removed["mask"], "rw-")
        self.assertNotIn(PRIVATE, json.dumps(result))
        for invalid in (
            "",
            ACL + "mask::rwx\n",
            ACL.replace("mask::rw-\n", ""),
            ACL.replace("user:1001:rw-", "user:1001:" + PRIVATE),
        ):
            self.assertEqual(probe.acl_fields(invalid, 1001), {"status": "invalid-acl"})

    def test_snapshot_observes_exact_device_without_changing_permissions(self):
        info = SimpleNamespace(
            st_mode=0o20660,
            st_ino=44,
            st_dev=5,
            st_rdev=os.makedev(10, 232),
            st_uid=0,
            st_gid=108,
            st_ctime_ns=99,
        )
        with (
            patch.object(probe.Path, "stat", return_value=info) as stat_call,
            patch.object(probe.os, "geteuid", return_value=1001),
            patch.object(
                probe, "command", return_value=({"status": "ok"}, ACL)
            ) as command,
            patch.object(
                probe.os, "open", side_effect=PermissionError(errno.EACCES, PRIVATE)
            ) as open_call,
        ):
            result = probe.snapshot()
        stat_call.assert_called_once_with()
        open_call.assert_called_once_with("/dev/kvm", os.O_RDWR | os.O_CLOEXEC)
        command.assert_called_once_with(
            ["getfacl", "--omit-header", "--numeric", "--absolute-names", "/dev/kvm"]
        )
        self.assertEqual(result["open_rdwr"], {"status": "os-error", "errno": 13})
        self.assertEqual(result["device"]["mode"], 0o660)
        self.assertNotIn(PRIVATE, json.dumps(result))

    def test_command_errors_never_publish_raw_stderr_or_exception(self):
        failures = (
            (
                subprocess.TimeoutExpired(
                    [PRIVATE], 10, output=PRIVATE, stderr=PRIVATE
                ),
                "timeout",
            ),
            (FileNotFoundError(errno.ENOENT, PRIVATE), "os-error"),
        )
        for error, expected in failures:
            with (
                self.subTest(expected=expected),
                patch.object(probe.subprocess, "run", side_effect=error),
            ):
                status, output = probe.command(["fixture"])
                self.assertEqual(status["status"], expected)
                self.assertEqual(output, "")
                self.assertNotIn(PRIVATE, json.dumps(status))
        result = subprocess.CompletedProcess([], 1, PRIVATE, PRIVATE)
        with patch.object(probe.subprocess, "run", return_value=result):
            status, _ = probe.command(["fixture"])
        self.assertEqual(
            status,
            {"status": "command-failed", "returncode": 1, "stderr_present": True},
        )

    def test_properties_and_rule_evidence_have_a_small_public_projection(self):
        fields = probe.property_fields(
            PROPERTIES + f"SECRET={PRIVATE}\nID_SEAT={PRIVATE}\n"
        )
        self.assertTrue(fields["uaccess_tag"])
        self.assertTrue(fields["current_uaccess_tag"])
        self.assertNotIn(PRIVATE, json.dumps(fields))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / (PRIVATE + ".rules")
            path.write_text(
                '# KERNEL=="kvm", SECRET="' + PRIVATE + '"\n'
                'KERNEL=="kvm", GROUP="kvm", MODE="0660", TAG+="uaccess", SECRET="'
                + PRIVATE
                + '"\n'
                'TAG=="uaccess", RUN{builtin}+="uaccess"\n'
                'KERNEL=="other", SECRET="' + PRIVATE + '"\n'
            )
            with patch.object(probe, "RULE_DIRS", (directory,)):
                result = probe.rules()
            self.assertEqual(result["status"], "ok")
            self.assertEqual(len(result["candidates"]), 2)
            first, second = result["candidates"]
            self.assertEqual(first["line"], 2)
            self.assertEqual(first["assignments"], ["GROUP", "MODE", "TAG"])
            self.assertTrue(second["uaccess_builtin"])
            self.assertEqual(first["known_name"], "other")
            self.assertNotIn(PRIVATE, json.dumps(result))
            (Path(directory) / "70-uaccess.rules").write_text(
                'KERNEL=="kvm", TAG+="uaccess"\n'
            )
            with patch.object(probe, "RULE_DIRS", (directory,)):
                self.assertIn(
                    "70-uaccess.rules",
                    {item["known_name"] for item in probe.rules()["candidates"]},
                )

    def event(self, source="UDEV", path=probe.DEVPATH):
        padding = "" if source == "KERNEL" else "  "
        return [
            f"{source}{padding}[ 123.456] change {path} (misc)",
            f"DEVPATH={path}",
            "DEVNAME=/dev/kvm",
            "SUBSYSTEM=misc",
            "SEQNUM=17",
            "TAGS=:uaccess:",
            "EVENT=create",
            "PID=1234",
            "CREATED=2",
            "COUNT=1",
            "STATS_PATH=" + PRIVATE,
            "SECRET=" + PRIVATE,
        ]

    def test_monitor_filters_unrelated_devices_and_all_nonwhitelisted_properties(self):
        for source in ("KERNEL", "UDEV"):
            fields = probe.monitor_event(self.event(source))
            self.assertEqual(fields["source"], source.lower())
            self.assertEqual(fields["sequence"], 17)
            self.assertTrue(fields["uaccess_tag"])
            self.assertEqual(fields["vm_event"], "create")
            self.assertEqual(
                (fields["pid"], fields["created"], fields["count"]), (1234, 2, 1)
            )
            self.assertNotIn(PRIVATE, json.dumps(fields))
        invalid_values = [
            line.replace("EVENT=create", "EVENT=" + PRIVATE).replace(
                "PID=1234", "PID=" + PRIVATE
            )
            for line in self.event()
        ]
        fields = probe.monitor_event(invalid_values)
        self.assertIsNone(fields["vm_event"])
        self.assertNotIn("pid", fields)
        self.assertNotIn(PRIVATE, json.dumps(fields))
        self.assertIsNone(
            probe.monitor_event(self.event(path=probe.DEVPATH + "-other"))
        )
        wrong_name = self.event()
        wrong_name[2] += "-other"
        self.assertEqual(
            probe.monitor_event(wrong_name), {"status": "invalid-kvm-event"}
        )
        malformed = self.event()
        malformed[0] = PRIVATE
        self.assertEqual(
            probe.monitor_event(malformed), {"status": "invalid-kvm-event"}
        )
        monitor = probe.Monitor()
        monitor.stopping = True
        stream = "monitor will print the received events for:\nUDEV - udev\nKERNEL - kernel\n\n"
        stream += "\n".join(self.event("KERNEL")) + "\n\n"
        stream += "\n".join(self.event(path=probe.DEVPATH + "-other")) + "\n\n"
        stream += "\n".join(self.event()) + "\n\n"
        monitor.read(io.StringIO(stream))
        self.assertTrue(monitor.ready.is_set())
        self.assertEqual(len(monitor.result["events"]), 2)
        self.assertEqual(monitor.result["status"], "ok")
        self.assertNotIn(PRIVATE, json.dumps(monitor.result))
        monitor.stopping = False
        monitor.read(io.StringIO(""))
        self.assertEqual(monitor.result["status"], "monitor-exited-early")

    def test_monitor_start_and_stop_failures_are_coverage_failures(self):
        monitor = probe.Monitor()
        with patch.object(
            probe.subprocess,
            "Popen",
            side_effect=PermissionError(errno.EACCES, PRIVATE),
        ):
            monitor.start()
        self.assertEqual(monitor.stop()["status"], "os-error")
        self.assertNotIn(PRIVATE, json.dumps(monitor.result))
        monitor = probe.Monitor()
        monitor.process = Mock()
        monitor.process.poll.return_value = None
        monitor.process.returncode = -9
        monitor.process.wait.side_effect = [subprocess.TimeoutExpired([], 5), -9]
        monitor.thread = Mock()
        monitor.thread.is_alive.return_value = False
        self.assertEqual(monitor.stop()["status"], "monitor-stop-timeout")
        monitor.process.kill.assert_called_once()

    def test_monitor_cleanup_handles_a_privileged_launcher(self):
        for timed_out in (False, True):
            with self.subTest(timed_out=timed_out):
                monitor = probe.Monitor()
                monitor.process = Mock(pid=1234)
                monitor.process.poll.return_value = None
                monitor.process.returncode = -9 if timed_out else -15
                monitor.process.terminate.side_effect = PermissionError(
                    errno.EPERM, PRIVATE
                )
                monitor.process.kill.side_effect = PermissionError(errno.EPERM, PRIVATE)
                monitor.process.wait.side_effect = (
                    [subprocess.TimeoutExpired([], 5), -9] if timed_out else [-15]
                )
                with patch.object(
                    probe, "command", return_value=({"status": "ok"}, PRIVATE)
                ) as command:
                    result = monitor.stop()
                expected = [["sudo", "-n", "kill", "-TERM", "1234"]]
                if timed_out:
                    expected.append(["sudo", "-n", "kill", "-KILL", "1234"])
                self.assertEqual(
                    [call.args[0] for call in command.call_args_list], expected
                )
                self.assertEqual(
                    result["status"], "monitor-stop-timeout" if timed_out else "ok"
                )
                self.assertNotIn(PRIVATE, json.dumps(result))

    def test_qmp_uses_one_paused_networkless_machine_and_requires_enabled_reply(self):
        outputs = (
            (
                0,
                {"return": {"enabled": True, "present": True}, "id": "kvm-probe"},
                True,
            ),
            (
                1,
                {"return": {"enabled": True, "present": True}, "id": "kvm-probe"},
                False,
            ),
            (
                0,
                {"return": {"enabled": False, "present": True}, "id": "kvm-probe"},
                False,
            ),
            (0, {"return": {"enabled": True, "present": True}, "id": PRIVATE}, False),
            (0, {"error": PRIVATE}, False),
        )
        for code, message, success in outputs:
            output = PRIVATE + "\n" + json.dumps(message) + "\n[]\n"
            with (
                self.subTest(code=code, message=message),
                patch.object(
                    probe.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], code, output, PRIVATE),
                ) as run,
            ):
                result = probe.qemu_probe()
            run.assert_called_once()
            argv = run.call_args.args[0]
            self.assertEqual(
                argv,
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
            )
            self.assertEqual(run.call_args.kwargs["timeout"], 15)
            self.assertEqual(
                [
                    json.loads(line)
                    for line in run.call_args.kwargs["input"].splitlines()
                ],
                [
                    {"execute": "qmp_capabilities"},
                    {"execute": "query-kvm", "id": "kvm-probe"},
                    {"execute": "quit"},
                ],
            )
            self.assertEqual(
                result["status"] == "ok" and result["query_kvm_enabled"], success
            )
            self.assertNotIn(PRIVATE, json.dumps(result))

    def test_journal_summaries_filter_messages_and_keep_unavailability_visible(self):
        records = [
            {
                "MESSAGE": "/dev/kvm: ACL uaccess permission denied " + PRIVATE,
                "OTHER": PRIVATE,
            },
            {"MESSAGE": "/dev/kvm-other: ACL " + PRIVATE},
            {"MESSAGE": "/private/dev/kvm: ACL " + PRIVATE},
            {"MESSAGE": "unrelated " + PRIVATE},
        ]
        output = "\n".join(json.dumps(record) for record in records)
        result = probe.journal_summary(output)
        self.assertEqual(
            result,
            {
                "status": "ok",
                "matched_records": 1,
                "uaccess_mentions": 1,
                "acl_mentions": 1,
                "permission_mentions": 1,
            },
        )
        self.assertNotIn(PRIVATE, json.dumps(result))
        self.assertEqual(probe.journal_summary("[]")["status"], "invalid-journal")
        self.assertEqual(
            probe.journal_summary(output + "\nnot-json")["status"], "invalid-journal"
        )
        self.assertEqual(
            probe.journal_summary("\n".join([json.dumps(records[0])] * 201))["status"],
            "truncated",
        )
        with patch.object(
            probe,
            "command",
            return_value=({"status": "command-failed", "returncode": 1}, PRIVATE),
        ) as command:
            summaries = probe.journals(100.0, 101.0)
        self.assertEqual(command.call_count, 4)
        self.assertIn("--output-fields=__CURSOR", command.call_args_list[0].args[0])
        for call in command.call_args_list[1:]:
            argv = call.args[0]
            self.assertIn("--since=@100.000000", argv)
            self.assertIn("--until=@101.000000", argv)
            self.assertTrue(any(argument.startswith("--grep=") for argument in argv))
            self.assertTrue(
                "--dmesg" in argv
                or any(argument.startswith("--unit=") for argument in argv)
            )
        self.assertEqual(summaries["availability"]["status"], "journal-unavailable")
        self.assertTrue(
            all(
                summaries[source]["status"] == "journal-query-failed"
                for source in ("kernel", "udev", "logind")
            )
        )
        self.assertNotIn(PRIVATE, json.dumps(summaries))

    def test_journal_no_matches_is_distinct_from_missing_journal_or_denied_query(self):
        available = (
            {"status": "ok", "returncode": 0, "stderr_present": False},
            json.dumps({"__CURSOR": PRIVATE}),
        )
        no_match = (
            {"status": "command-failed", "returncode": 1, "stderr_present": False},
            "",
        )
        denied = (
            {"status": "command-failed", "returncode": 1, "stderr_present": True},
            "",
        )
        for first, query, complete in (
            (available, no_match, True),
            (({"status": "ok", "returncode": 0}, ""), no_match, False),
            (available, denied, False),
        ):
            with (
                self.subTest(complete=complete, query=query),
                patch.object(
                    probe, "command", side_effect=[first, query, query, query]
                ),
            ):
                result = probe.journals(100, 101)
            self.assertEqual(
                all(item["status"] == "ok" for item in result.values()), complete
            )
            if complete:
                self.assertTrue(result["kernel"]["no_matches"])
                self.assertEqual(result["kernel"]["query_returncode"], 1)
                self.assertEqual(result["kernel"]["matched_records"], 0)
            self.assertNotIn(PRIVATE, json.dumps(result))

    def test_collection_limits_read_errors_and_missing_properties_remain_visible(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "70-fixture.rules"
            path.write_text('KERNEL=="kvm", TAG+="uaccess"\n' * (probe.LIMIT + 1))
            with patch.object(probe, "RULE_DIRS", (directory,)):
                self.assertEqual(probe.rules()["status"], "truncated")
            with (
                patch.object(probe, "RULE_DIRS", (directory,)),
                patch.object(
                    probe,
                    "read_rule",
                    side_effect=PermissionError(errno.EACCES, PRIVATE),
                ),
            ):
                self.assertEqual(probe.rules()["status"], "incomplete-rules")
            path.write_bytes(b"x" * (1024 * 1024 + 1))
            with self.assertRaises(ValueError):
                probe.read_rule(path)
        with patch.object(probe, "command", return_value=({"status": "ok"}, "")):
            self.assertEqual(probe.properties()["status"], "incomplete-properties")
        self.assertIsNone(probe.property_fields("")["uaccess_tag"])
        monitor = probe.Monitor()
        for _ in range(probe.LIMIT + 1):
            monitor.append(self.event())
        self.assertEqual(monitor.result["status"], "truncated")
        self.assertEqual(len(monitor.events), probe.LIMIT)
        with patch.object(
            probe.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(
                [PRIVATE], 15, output=PRIVATE, stderr=PRIVATE
            ),
        ):
            result = probe.qemu_probe()
        self.assertEqual(result["status"], "timeout")
        self.assertFalse(result["query_kvm_enabled"])
        self.assertNotIn(PRIVATE, json.dumps(result))

    def test_unavailable_device_never_grants_or_starts_qemu(self):
        monitor = Mock()
        monitor.stop.return_value = {"status": "ok"}
        with (
            patch.object(probe, "Monitor", return_value=monitor),
            patch.object(probe, "rules", return_value={"status": "ok"}),
            patch.object(probe, "properties", return_value={"status": "ok"}),
            patch.object(
                probe,
                "snapshot",
                return_value={"device": {"status": "os-error", "errno": 2}},
            ),
            patch.object(probe, "command") as command,
            patch.object(probe, "qemu_probe") as qemu,
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(probe.run(), 1)
        command.assert_not_called()
        qemu.assert_not_called()
        monitor.stop.assert_called_once()
        self.assertIn('"coverage_complete": false', output.getvalue())

    def test_monitor_not_ready_is_reported_without_launching_any_real_process(self):
        monitor = probe.Monitor()
        process = Mock(stdout=io.StringIO(""))
        process.poll.return_value = None
        process.returncode = -15
        with (
            patch.object(probe.subprocess, "Popen", return_value=process) as popen,
            patch.object(probe.threading, "Thread") as thread,
            patch.object(monitor.ready, "wait", return_value=False),
        ):
            thread.return_value.is_alive.return_value = False
            monitor.start()
            self.assertEqual(monitor.stop()["status"], "monitor-not-ready")
        self.assertEqual(
            popen.call_args.args[0],
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
        )

    def snapshot_fixture(self):
        return {
            "device": {"status": "ok", "character_device": True, "inode": 44},
            "identity": {"uid": 1001},
            "acl": probe.acl_fields(ACL, 1001),
            "open_rdwr": {"status": "ok"},
        }

    def test_sequence_never_regrants_retries_or_hides_access_qmp_or_coverage_failure(
        self,
    ):
        for failure in (
            None,
            "entry",
            "mask",
            "open",
            "qmp",
            "grant",
            "monitor",
            "rules",
            "properties",
            "journal",
            "acl",
        ):
            with self.subTest(failure=failure):
                before = self.snapshot_fixture()
                after = copy.deepcopy(before)
                if failure == "entry":
                    after["acl"].update(uid_entry=None, effective_uid_entry=None)
                if failure == "mask":
                    after["acl"].update(mask="r--", effective_uid_entry="r--")
                if failure == "open":
                    after["open_rdwr"] = {"status": "os-error", "errno": 13}
                if failure == "acl":
                    after["acl"] = {"status": "invalid-acl"}
                monitor = Mock()
                monitor.stop.return_value = {
                    "status": "monitor-exited-early" if failure == "monitor" else "ok"
                }
                status = lambda name: {"status": "timeout" if failure == name else "ok"}
                calls = []

                def snapshot():
                    calls.append("snapshot")
                    return [before, before, after][calls.count("snapshot") - 1]

                def grant(argv):
                    calls.append("grant")
                    self.assertEqual(
                        argv, ["sudo", "-n", "setfacl", "-m", "u:1001:rw", "/dev/kvm"]
                    )
                    return status("grant"), PRIVATE

                def qmp():
                    calls.append("qmp")
                    return {"status": "ok", "query_kvm_enabled": failure != "qmp"}

                output = io.StringIO()
                with contextlib.ExitStack() as stack:
                    for name, value in (
                        ("Monitor", Mock(return_value=monitor)),
                        ("rules", lambda: status("rules")),
                        ("properties", lambda: status("properties")),
                        ("snapshot", snapshot),
                        ("command", grant),
                        ("qemu_probe", qmp),
                        ("journals", lambda *args: {"kernel": status("journal")}),
                    ):
                        stack.enter_context(patch.object(probe, name, value))
                    stack.enter_context(
                        patch.object(probe.os, "geteuid", return_value=1001)
                    )
                    stack.enter_context(patch.object(probe.time, "sleep"))
                    stack.enter_context(contextlib.redirect_stdout(output))
                    self.assertEqual(probe.run(), 0 if failure is None else 1)
                self.assertEqual(
                    calls, ["snapshot", "grant", "snapshot", "qmp", "snapshot"]
                )
                monitor.start.assert_called_once()
                monitor.stop.assert_called_once()
                self.assertNotIn(PRIVATE, output.getvalue())
                records = [json.loads(line) for line in output.getvalue().splitlines()]
                self.assertEqual(records[-1]["success"], failure is None)
                delta = next(
                    record for record in records if record["record"] == "comparison"
                )
                if failure == "entry":
                    self.assertTrue(delta["uid_entry_changed"])
                    self.assertFalse(delta["mask_changed"])
                if failure == "mask":
                    self.assertFalse(delta["uid_entry_changed"])
                    self.assertTrue(delta["mask_changed"])

    def test_unexpected_failures_remain_red_and_do_not_print_exception_text(self):
        with (
            patch.object(probe, "guard", return_value=True),
            patch.object(probe, "run", side_effect=RuntimeError(PRIVATE)),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(probe.main(), 1)
        self.assertNotIn(PRIVATE, output.getvalue())
        self.assertFalse(json.loads(output.getvalue())["coverage_complete"])

    def test_workflow_is_push_only_and_does_not_change_existing_acceptance(self):
        text = (ROOT / ".github/workflows/kvm-access-probe.yml").read_text()
        trigger = text.split("\non:\n")[1].split("\npermissions:")[0]
        self.assertEqual(
            trigger,
            """  push:
    branches: [ci/kvm-acl-diagnostic]
    paths:
      - "scripts/dev/kvm-access-probe.py"
      - "tests/test_kvm_access_probe.py"
      - ".github/workflows/kvm-access-probe.yml"
      - "docs/DEVELOPMENT.md"
""",
        )
        self.assertIn("permissions:\n  contents: read\n", text)
        self.assertIn("runs-on: ubuntu-latest", text)
        self.assertIn("persist-credentials: false", text)
        self.assertIn("actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803", text)
        self.assertIn(
            "python3 -m unittest discover -s tests -p test_kvm_access_probe.py -v", text
        )
        self.assertIn('SBM_KVM_ACCESS_PROBE: "1"', text)
        self.assertEqual(text.count("run: python3 scripts/dev/kvm-access-probe.py"), 1)
        for forbidden in (
            "pull_request",
            "workflow_dispatch",
            "continue-on-error",
            "upload-artifact",
            "cache@",
            "pixi",
            "node-g0",
            "chown",
            "setfacl",
            "if:",
            "secrets.",
        ):
            self.assertNotIn(forbidden, text)
        for name in ("ci.yml", "sbc.yml"):
            workflow = (ROOT / ".github/workflows" / name).read_text()
            push = workflow.split("\n  push:\n")[1].split("\n  pull_request:")[0]
            self.assertIn("branches: [main]", push)


if __name__ == "__main__":
    unittest.main()
