# sing-box-manager

Deploy, package and distribute [sing-box](https://github.com/SagerNet/sing-box).

The primary deployment target is a single VPS that runs:

- one `sing-box.service` with Trojan, Hysteria2 and, when users are enabled, Naive inbounds
- the authenticated download portal behind `nginx`
- `certbot-nginx` for CA-signed certificates

## Public source and private operations

The public GitHub repository is `xiaosq2000/sing-box-manager`; the Go module
uses the matching path. The project and its commands remain `sing-box-manager`,
`sbm`, and `sbc`.

This repository contains code, example configuration, and synthetic test fixtures.
It does not include a runtime inventory or generated releases. Keep deployment
inventory, credentials, TLS keys, signing keys, and user data in an operator-owned
location or a separate private repository. See
[secret handling](docs/SECRET_HANDLING.md) and
[contributing](CONTRIBUTING.md) before adding configuration or test data.

CI uses standard GitHub-hosted runners on Linux, macOS, and Windows. Do not route
untrusted public pull requests to a persistent self-hosted runner.

## Setup

The build and deploy environment targets Linux x86-64. Install Pixi and run commands
from the repository root.

The code-built configurations target sing-box **1.14.2**. Pixi provides Python 3.12
and Go 1.25.5 or later in the 1.25 series for custom server builds and the `sbc`
client.

```sh
pixi install
```

Copy `.sops.example.yaml` to the ignored `.sops.yaml` and set your own public Age
recipient. Create or edit your encrypted inventory using the
[secret-handling guide](docs/SECRET_HANDLING.md#age-and-sops-setup). Copying the
example to a `.sops.yaml` filename does not encrypt it. For local development, use
a plaintext `.yaml` outside the checkout and pass its path with `--config`.

This repo tracks bundled font files with Git LFS. If your clone was created on a host
that does not already have Git LFS configured, run this once per checkout before
starting the app or building releases:

```sh
git lfs install --local
git lfs pull
```

`git lfs install --local` stores the filter configuration in `.git/config` instead of
your user-level `~/.gitconfig`.

This applies to the machine you build and deploy _from_. The server needs neither git
nor Git LFS: `sbm deploy` ships the real font bytes over `rsync`, and refuses to start
if this clone still holds LFS pointer stubs.

## Configuration

See [`config/inventory/example.yaml`](config/inventory/example.yaml) for the full
config shape. All settings and deployment/user data live in one file, passed via
`--config <path>` or the `SBM_CONFIG` environment variable.

`sbm serve` refuses to start without `web.session_secret`, which signs browser
sessions and installer machine tokens. Any value works for local development. For a
deployed portal, see [server configuration](docs/VPS_DEPLOYMENT.md#2-server-configuration).

`web.subscription_secret` keys the subscription links the `sbc` client fetches its
config from. Without it, `sbm serve` turns those links off.

The Trojan, Hysteria2, and Naive renderers build configurations in code and keep real
deployment secrets in an operator-owned encrypted runtime inventory. See
[`docs/SECRET_HANDLING.md`](docs/SECRET_HANDLING.md) before editing any user
credentials, obfs secrets, or TLS values.

`default_protocol` controls which protocol a new client install starts with when
no protocol is chosen. The server installer enables all packaged protocol inbounds
by default. A Naive inbound is omitted when its roster is empty or entirely disabled.
`sbm release` requires every release user to have access to that default protocol.

If `traffic_stats.enabled` is turned on in the config file, `sbm release` builds a
custom Linux server `sing-box` binary from the upstream source tag with
`with_v2ray_api`, while client download archives continue using the official upstream
release assets.

Desktop mixed mode offers `china` (the default), `gfw`, `ai`, and `global` routes.
`china` proxies unknown destinations; `gfw` proxies GFWList and AI destinations
and sends other traffic directly. Every client profile keeps the exact
`deployment.host` and optional `deployment.direct_domain_suffixes` direct, with
matching direct DNS rules. Suffixes cover the apex and subdomains.

The `sbc` downloads include initial rule files, so the proxy
can start before rule downloads succeed. DNS answers persist across service restarts,
with normal DNS expiry. See the
[release guide](docs/RELEASE_AND_PORTAL.md#build-the-release) for build requirements.

`sbm release` can also read a repo-root `.env` for local operator secrets such as
`SOPS_AGE_KEY` and `GITHUB_TOKEN`; the secret-handling doc covers the tradeoffs.

`sbm release` uses the current files in `config/` as-is; it does not pin, reset,
or check out config state with git.

## Docs

- [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md): focused checks, docstale, and agent instruction maintenance
- [`docs/SECRET_HANDLING.md`](docs/SECRET_HANDLING.md): inventory creation, credentials, and TLS handling
- [`docs/VPS_DEPLOYMENT.md`](docs/VPS_DEPLOYMENT.md): primary end-to-end VPS deployment guide
- [`docs/RELEASE_AND_PORTAL.md`](docs/RELEASE_AND_PORTAL.md): build releases, run the portal, and download client packages
- [`docs/README_LINUX.md`](docs/README_LINUX.md): the bash client on Linux, which releases no longer pack
- [`docs/README_MACOS.md`](docs/README_MACOS.md): the bash client on macOS, which releases no longer pack
- [`docs/WEBRTC.md`](docs/WEBRTC.md): cross-platform `sbc webrtc` behavior and browser cleanup
- [`docs/README_WIN.md`](docs/README_WIN.md): Windows installer, scheduled-task layout, and uninstall
- [`docs/TUN_MODE.md`](docs/TUN_MODE.md): Linux TUN design, ownership, and verification limits
- [`docs/REMOTE_DEPLOYMENT.md`](docs/REMOTE_DEPLOYMENT.md): sync the active release artifacts to a remote host and install them
- [`docs/REFACTOR_SPEC.md`](docs/REFACTOR_SPEC.md): target architecture and refactoring milestones

## Common Commands

Every `sbm` operation requires `--config <path>` or exported `SBM_CONFIG`. Put the
global `--config` option before the subcommand. `--help` works without a config.
Use the same inventory for a release build and its deploy.

Build release artifacts:

```sh
pixi run sbm --config config/inventory/runtime.sops.yaml release
```

Start the authenticated download portal after a successful release build:

```sh
pixi run sbm --config config/inventory/runtime.sops.yaml serve
```

Collect per-user traffic stats from the local sing-box node service:

```sh
pixi run sbm --config config/inventory/runtime.sops.yaml stats-collect
```

Collect the per-domain breakdown. This subscribes to the sing-box 1.14.0 API service and
runs until stopped, because sing-box drops connection events while nobody is listening:

```sh
pixi run sbm --config config/inventory/runtime.sops.yaml stats-stream
```

Portal users marked with `web_portal.users[].admin: true` can open `/admin` after login
to view all collected users' current billing-cycle traffic totals and top destinations,
and `/admin/connections` for a live view of open connections.

After building a release, deploy to a fresh VPS. Replace `vpn-host` with your SSH
host alias:

```sh
pixi run sbm --config config/inventory/runtime.sops.yaml deploy --hostname vpn-host --bootstrap
```

Deploy subsequent releases to an already-provisioned host:

```sh
pixi run sbm --config config/inventory/runtime.sops.yaml deploy --hostname vpn-host
```

Deploy uses `rsync` over SSH, and Pixi provides the local `rsync` binary. It ships
the code and existing release; it does not build a release. See the
[deploy options](docs/REMOTE_DEPLOYMENT.md#command) for protocol selection, custom
paths, bootstrap email, and dry runs.

## Development

```sh
pixi install
pixi run test tests/test_cli.py
```

See [DEVELOPMENT.md](docs/DEVELOPMENT.md) for checks appropriate to the change.
