"""Ninja/Pydantic/PostgreSQL feasibility tests, with no operator inventory."""

import hashlib
import json
import os
import secrets
import subprocess
import sys

import pytest

from tests.test_control_plane_integration import postgres as postgres_fixture, psql

postgres = postgres_fixture
NODE = "11111111-1111-4111-8111-111111111111"
SOURCE = "22222222-2222-4222-8222-222222222222"
CREDENTIAL = "33333333-3333-4333-8333-333333333333"


@pytest.fixture(scope="module")
def g0_env(postgres, tmp_path_factory):
    if not __import__("importlib.util").util.find_spec("ninja"):
        if os.environ.get("SBM_REQUIRE_NODE_G0") == "1":
            pytest.fail("Install the locked node-g0 environment")
        pytest.skip("The opt-in node-g0 environment is required")
    root = tmp_path_factory.mktemp("node-g0")
    assert psql(postgres, "postgres", "CREATE DATABASE node_g0").returncode == 0
    secret = secrets.token_hex(32)
    (root / "control.json").write_text(
        json.dumps(
            {
                "django_secret": secrets.token_hex(32),
                "socket_dir": str(postgres["sockets"]),
            }
        )
    )
    config = {"log": {"level": "warn"}}
    encoded = json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
    (root / "bundle.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "node_id": "",
                "generation": 1,
                "config": config,
                "config_sha256": hashlib.sha256(encoded).hexdigest(),
                "stats_address": "127.0.0.1:19080",
                "roster": [
                    {"credential_id": CREDENTIAL, "wire_name": "trojan:fixture"}
                ],
            }
        )
    )
    env = {
        **postgres["env"],
        "SBM_NODE_G0_DIR": str(root),
        "DJANGO_SETTINGS_MODULE": "tests.node_g0.settings",
    }
    for action, data in (
        ("bootstrap", None),
        (
            "register",
            json.dumps(
                {
                    "node_id": NODE,
                    "source_id": SOURCE,
                    "token_digest": hashlib.sha256(secret.encode()).hexdigest(),
                }
            ),
        ),
    ):
        result = subprocess.run(
            [sys.executable, "-m", "tests.node_g0.control", action],
            env=env,
            input=data,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
    return env, secret


def execute(g0_env, script):
    env, secret = g0_env
    prefix = f"""
import django
django.setup()
import json, hashlib
from django.test import Client
from tests.node_g0.models import Node, Usage
from tests.node_g0.urls import canonical, digest
client = Client()
headers = {{"HTTP_AUTHORIZATION": "Bearer {NODE}.{secret}"}}
"""
    result = subprocess.run(
        [sys.executable, "-"],
        input=prefix + script,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr


def test_authentication_etag_and_private_docs(g0_env):
    execute(
        g0_env,
        """
assert client.get('/node-g0/v1/config').status_code == 401
response = client.get('/node-g0/v1/config', **headers)
assert response.status_code == 200
assert client.get('/node-g0/v1/config', HTTP_IF_NONE_MATCH=response['ETag'], **headers).status_code == 304
assert client.get('/node-g0/v1/docs').status_code == 404
assert client.get('/node-g0/v1/openapi.json').status_code == 404
bad = dict(headers)
bad['HTTP_AUTHORIZATION'] = bad['HTTP_AUTHORIZATION'][:-1] + ('0' if bad['HTTP_AUTHORIZATION'][-1] != '0' else '1')
assert client.get('/node-g0/v1/config', **bad).status_code == 401
body = json.loads(response.content)
assert hashlib.sha256(canonical(body['config'])).hexdigest() == body['config_sha256']
""",
    )


def test_absolute_snapshots_are_transactional_and_idempotent(g0_env):
    execute(
        g0_env,
        f"""
Usage.objects.all().delete()
row = {{'source_id': '{SOURCE}', 'credential_id': '{CREDENTIAL}', 'usage_date': '2026-10-23', 'version': 1, 'upload': 10, 'download': 20}}
def post(rows):
    return client.post('/node-g0/v1/traffic', data=canonical({{'snapshots': [dict(r, digest=digest(r)) for r in rows]}}), content_type='application/json', **headers)
assert post([row]).status_code == 200
assert post([row]).status_code == 200
assert Usage.objects.count() == 1 and Usage.objects.get().upload == 10
newer = dict(row, version=3, upload=30, download=60)
assert post([newer]).status_code == 200
assert post([row]).status_code == 200
assert Usage.objects.get().upload == 30
assert post([dict(newer, upload=31)]).status_code == 409
assert post([dict(newer, version=4, upload=29)]).status_code == 409
# Failure of a later row rolls back an earlier update in the same request.
assert post([dict(newer, version=4, upload=40), dict(row, credential_id='44444444-4444-4444-8444-444444444444')]).status_code == 403
assert Usage.objects.get().version == 3
assert post([dict(newer, source_id='55555555-5555-4555-8555-555555555555')]).status_code == 403
assert post([dict(newer, version=4, upload=-1)]).status_code == 422
assert post([dict(newer, version=4, upload=True)]).status_code == 422
assert post([dict(newer, version=4, upload=2**63)]).status_code == 422
""",
    )


def test_status_identity_and_strict_json(g0_env):
    execute(
        g0_env,
        """
node = Node.objects.get()
body = {'generation': node.generation, 'config_sha256': node.bundle['config_sha256'], 'invocation': 'a'*32}
def post(value):
    return client.post('/node-g0/v1/status', data=canonical(value), content_type='application/json', **headers)
assert post(body).status_code == 200
assert post(dict(body, generation=2)).status_code == 409
assert post(dict(body, extra='invalid')).status_code == 422
assert client.post('/node-g0/v1/status', data='{"generation":1,"generation":2}', content_type='application/json', **headers).status_code == 400
""",
    )


def test_registration_retry_does_not_change_policy(g0_env):
    env, secret = g0_env
    value = {
        "node_id": NODE,
        "source_id": SOURCE,
        "token_digest": hashlib.sha256(secret.encode()).hexdigest(),
    }
    for expected in (0, 0):
        result = subprocess.run(
            [sys.executable, "-m", "tests.node_g0.control", "register"],
            env=env,
            input=json.dumps(value),
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == expected, result.stderr
    value["token_digest"] = "0" * 64
    result = subprocess.run(
        [sys.executable, "-m", "tests.node_g0.control", "register"],
        env=env,
        input=json.dumps(value),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode != 0
    assert secret not in result.stdout + result.stderr
