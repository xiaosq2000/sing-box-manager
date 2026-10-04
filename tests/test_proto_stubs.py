"""Tests that the committed sing-box API stubs match their .proto source."""

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROTO_DIR = REPO_ROOT / "sing_box_manager" / "proto"
PROTO_RELATIVE = "sing_box_manager/proto/sing_box_api.proto"
GENERATED_NAMES = (
    "sing_box_api_pb2.py",
    "sing_box_api_pb2.pyi",
    "sing_box_api_pb2_grpc.py",
)


def test_wire_identity_matches_upstream():
    """The package and service names decide the gRPC method path.

    Renaming either silently points the client at a path sing-box does not
    serve, which surfaces as UNIMPLEMENTED at runtime rather than at import.
    """
    from sing_box_manager.proto import sing_box_api_pb2 as pb

    assert pb.DESCRIPTOR.package == "daemon"
    service = pb.DESCRIPTOR.services_by_name["StartedService"]
    assert service.full_name == "daemon.StartedService"
    assert service.methods_by_name["SubscribeConnections"].server_streaming


def test_connection_field_numbers_match_upstream():
    """Field numbers are the wire contract with sing-box 1.14.0.

    This proto is a trimmed copy, so nothing else in the build would notice a
    renumbering; the bytes would simply decode into the wrong attributes.
    """
    from sing_box_manager.proto import sing_box_api_pb2 as pb

    fields = {
        field.name: field.number
        for field in pb.Connection.DESCRIPTOR.fields  # ty: ignore[unresolved-attribute]
    }
    assert fields == {
        "id": 1,
        "inbound": 2,
        "inboundType": 3,
        "ipVersion": 4,
        "network": 5,
        "source": 6,
        "destination": 7,
        "domain": 8,
        "protocol": 9,
        "user": 10,
        "fromOutbound": 11,
        "createdAt": 12,
        "closedAt": 13,
        "uplink": 14,
        "downlink": 15,
        "uplinkTotal": 16,
        "downlinkTotal": 17,
        "rule": 18,
        "outbound": 19,
        "outboundType": 20,
        "chainList": 21,
        "processInfo": 22,
    }


def test_unknown_fields_are_tolerated():
    """A future sing-box may add fields to Connection.

    Proto3 drops field numbers it does not know rather than failing the parse,
    which is what lets this trimmed copy survive an upstream addition. Assert
    it rather than assume it, because the failure mode is a hard parse error on
    every event once sing-box is upgraded.
    """
    from sing_box_manager.proto import sing_box_api_pb2 as pb

    user = b"\x52\x05alice"  # field 10 (user), length-delimited, "alice"
    unknown = b"\x9a\x06\x03abc"  # field 99, length-delimited: not in our copy

    decoded = pb.Connection.FromString(user + unknown)

    assert decoded.user == "alice"
    # Round-tripping keeps the unknown bytes, so nothing is silently destroyed.
    assert pb.Connection.FromString(decoded.SerializeToString()).user == "alice"


def test_committed_stubs_are_current(tmp_path):
    """A stale committed stub is invisible until something breaks at runtime."""
    pytest.importorskip("grpc_tools", reason="grpcio-tools is a dev-only dependency")

    subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "grpc_tools.protoc",
            "--proto_path=.",
            f"--python_out={tmp_path}",
            f"--pyi_out={tmp_path}",
            f"--grpc_python_out={tmp_path}",
            PROTO_RELATIVE,
        ],
        check=True,
        cwd=REPO_ROOT,
    )

    regenerated_dir = tmp_path / "sing_box_manager" / "proto"
    for name in GENERATED_NAMES:
        assert (regenerated_dir / name).read_text() == (PROTO_DIR / name).read_text(), (
            f"{name} is out of date; run `pixi run gen-proto`"
        )
