# Security policy

## Report vulnerabilities privately

Use GitHub's **Security → Report a vulnerability** for this repository when private
vulnerability reporting is enabled. Do not include vulnerabilities, credentials,
subscription links, private inventories, or user data in a public issue or pull
request.

If that reporting option is unavailable, contact the maintainer at the email
address listed in `pixi.toml` to arrange a confidential report. Send a description
of the issue first; do not email production credentials or user data.

Include the affected commit or version, impact, and reproduction steps using
synthetic data. No formal security-support window is currently promised. Check
the current main branch before reporting a problem with an older version.

## If a credential is exposed

1. Revoke or rotate the credential immediately. Do not rely on deleting the file.
2. Determine which systems and users the credential can affect.
3. Remove it from reachable Git history and any affected logs, artifacts, or
   attachments. Coordinate history changes with maintainers and collaborators.
4. Check for copies in forks and caches. Public copies cannot be reliably retracted.
5. Re-run secret scans and investigate possible unauthorized use.

A client signing-key rotation needs a compatible client trust update. Subscription
and session secret rotations invalidate links or tokens. See
[Secret handling](docs/SECRET_HANDLING.md) and the relevant deployment guide before
changing production keys.

## Repository boundaries

The public repository contains source, examples, and synthetic fixtures. Real
inventory, including encrypted inventory, belongs outside public source control.
Generated releases can contain protocol credentials and TLS private keys. User
traffic databases contain sensitive domain and destination information. Neither
belongs in public GitHub assets or CI artifacts.

CI uses disposable GitHub-hosted runners, read-only repository permissions, and
no production credentials. Do not run untrusted public pull requests on persistent
self-hosted runners, even when the runner is containerized. Do not execute PR code
in a privileged `pull_request_target` workflow.

The repository's secret scanner and forbidden-path checks reduce accidental
exposure. They do not prove that all sensitive data or application vulnerabilities
have been found. Review fixtures, documentation, and generated outputs manually.
