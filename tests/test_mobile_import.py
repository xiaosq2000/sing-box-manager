"""Offline import URI, QR decoding, and clipboard behavior checks."""

import base64
import io
import json
from pathlib import Path
import shutil
import subprocess
from unittest.mock import patch
from urllib.parse import parse_qs, unquote, urlsplit

from PIL import Image
import pytest
import zxingcpp

from sing_box_manager.control_plane import mobile
from sing_box_manager.control_plane.mobile import mobile_imports

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("username", ["fixture-user", '研发 & team/#?"'])
@pytest.mark.parametrize("origin", ["https://vpn.example.com", "https://vpn.example.com:8443"])
def test_qr_decodes_to_the_official_remote_profile_uri(username, origin):
    subscription = origin + "/sub/synthetic-subscription-token"
    mobile._qr_image.cache_clear()
    with patch("socket.socket", side_effect=AssertionError("QR generation must not use the network")):
        profiles = mobile_imports(subscription, username, ["trojan", "hysteria2", "naive"])
    for profile in profiles:
        uri = urlsplit(profile["import_uri"])
        assert (uri.scheme, uri.netloc, uri.path) == ("sing-box", "import-remote-profile", "")
        assert parse_qs(uri.query) == {"url": [profile["url"]]}
        assert unquote(uri.fragment) == username + " - " + profile["label"]
        remote = urlsplit(profile["url"])
        assert remote.path == "/sub/synthetic-subscription-token"
        assert parse_qs(remote.query) == {"format": ["mobile"], "protocol": [profile["protocol"]]}
        assert not remote.fragment
        prefix, encoded = profile["qr"].split(",", 1)
        assert prefix == "data:image/png;base64"
        image = Image.open(io.BytesIO(base64.b64decode(encoded))).convert("RGB")
        decoded = zxingcpp.read_barcode(image)
        assert decoded is not None
        assert decoded.text == profile["import_uri"]
        # No full configuration or protocol secret is encoded in the image.
        assert not decoded.text.startswith("{")


def test_repeated_pages_reuse_a_bounded_memory_cache():
    mobile._qr_image.cache_clear()
    with patch.object(mobile.segno, "make_qr", wraps=mobile.segno.make_qr) as encode:
        first = mobile_imports("https://vpn.example.com/sub/cache-fixture", "fixture", ["trojan"])
        assert mobile_imports("https://vpn.example.com/sub/cache-fixture", "fixture", ["trojan"]) == first
        assert encode.call_count == 1
    assert mobile._qr_image.cache_info().maxsize == 128


def test_import_choices_only_include_supplied_authorized_protocols():
    assert mobile_imports("https://vpn.example.com/sub/fixture", "fixture", []) == []
    profiles = mobile_imports("https://vpn.example.com/sub/fixture", "fixture", ["hysteria2"])
    assert [p["protocol"] for p in profiles] == ["hysteria2"]


@pytest.mark.parametrize("clipboard", ["working", "denied", "missing"])
def test_copy_button_and_manual_fallback(clipboard):
    node = shutil.which("node")
    assert node is not None, "The Pixi development environment must provide Node.js"
    source = (ROOT / "sing_box_manager/web/static/subscription.js").read_text()
    script = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const fixture = JSON.parse(fs.readFileSync(0, 'utf8'));
let handler, copied, focused = false, selected = false;
const button = {hidden: true, dataset: {copyTarget: 'fixture-link'}, addEventListener: (name, callback) => {assert.equal(name, 'click'); handler = callback;}};
const input = {value: 'https://vpn.example.com/sub/synthetic-token?format=mobile&protocol=trojan', focus: () => {focused = true;}, select: () => {selected = true;}};
const status = {textContent: ''};
const navigator = fixture.clipboard === 'missing' ? {} : {clipboard: {writeText: async value => {
  if (fixture.clipboard === 'denied') throw new Error('fixture denial');
  copied = value;
}}};
const document = {querySelectorAll: () => [button], getElementById: id => id === 'copy-status' ? status : input};
vm.runInNewContext(fixture.source, {document, navigator});
assert.equal(button.hidden, false);
(async () => {
  await handler();
  if (fixture.clipboard === 'working') {
    assert.equal(copied, input.value);
    assert.equal(focused, false);
    assert.match(status.textContent, /已复制/);
  } else {
    assert.equal(copied, undefined);
    assert.equal(focused, true);
    assert.equal(selected, true);
    assert.match(status.textContent, /手动复制/);
  }
  assert.ok(!status.textContent.includes('synthetic-token'));
})().catch(error => {console.error(error); process.exitCode = 1;});
"""
    result = subprocess.run([node, "-e", script], input=json.dumps({"clipboard": clipboard, "source": source}), text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
