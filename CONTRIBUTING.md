# Contributing

## Development environment

Use Linux x86-64, Pixi, and the locked environment:

```sh
pixi install --locked
pixi run precommit-install
```

See [Development](docs/DEVELOPMENT.md) for focused checks and native-platform test
requirements. The `sbc` client uses only the Go standard library and builds with
cgo disabled. Shared Unix scripts must remain compatible with bash 3.2; Windows
scripts must remain compatible with Windows PowerShell 5.1.

## Keep operations private

Commit code, documentation, example configuration, and synthetic fixtures only.
Do not commit any real inventory, even when encrypted. Only
`config/inventory/example.yaml` belongs in public source control.

Keep credentials, TLS and signing private keys, generated releases, subscription
links, logs, and user traffic databases outside the public repository. Use a
private operator repository or another restricted location. See
[Secret handling](docs/SECRET_HANDLING.md).

Never copy production credentials into a test. Use documentation domains,
reserved example IP addresses, and clearly synthetic values. Public verification
keys are not private keys. Secret-scanner exceptions must identify both the exact
fixture path and the exact reviewed synthetic value; do not exempt whole test
directories. Inline `gitleaks:allow` comments are not accepted by the scanner.

## Checks before a pull request

Run the checks appropriate to your change, plus the public-file policy:

```sh
pixi run python scripts/dev/check-public-tree.py
pixi run test tests/test_public_repository.py
# Scan changes already staged for commit:
bash scripts/dev/check-secrets.sh --staged
```

After committing, scan all reachable Git history:

```sh
pixi run python scripts/dev/check-public-tree.py --history
bash scripts/dev/check-secrets.sh
```

The scanner downloads a pinned Gitleaks Linux x86-64 archive, verifies its SHA-256,
and redacts findings. A negative control checks that a new synthetic credential
still fails inside a fixture path. A failed check does not print the credential
value. Removing
a secret in a later commit does not remove it from history. Revoke or rotate a
committed credential and follow [Security](SECURITY.md) before sharing it.

## CI and review

CI runs on standard GitHub-hosted Linux, macOS, and Windows runners. Public pull
requests must not run on persistent self-hosted runners. CI needs no production
credentials or private inventory. Its GitHub token is read-only.

Actions are pinned to full commit hashes. Update the version comment and reviewed
hash together. The Linux lint job checks forbidden paths in the index and Git
history, then scans reachable history for secrets. Keep these checks enabled.

Use fixture-based local tests for code changes. Do not run a release, deploy,
server installation, or destructive cleanup merely to validate a pull request.
The native end-to-end tests modify their runner user's services and settings; run
them only in a disposable environment.

Preserve the GPLv3 license and third-party notices, including the bundled fonts'
OFL license. Submit security vulnerabilities through the private reporting path
in [Security](SECURITY.md), not a public issue.
