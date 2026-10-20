# sing-box-manager

Checked on 2026-10-20.

Deploy, package and distribute [sing-box](https://github.com/SagerNet/sing-box).

The primary deployment target is a single VPS that runs:

- one `sing-box.service` with Trojan, Hysteria2 and, when users are enabled, Naive inbounds
- the authenticated download portal behind `nginx`
- `certbot-nginx` for CA-signed certificates

One Django/PostgreSQL service provides the portal and control plane. It manages
operators, proxy accounts (`Account`), and protocol credentials (`Credential`).
Users open their subscription pages. Operators manage accounts and traffic through
SSH-tunnelled Django Admin. There is no separate public password-login portal.
See [the control-plane guide](docs/CONTROL_PLANE.md) for enablement and recovery.

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

The code-built configurations target sing-box **1.14.3**. Pixi provides Python 3.12
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
config shape. Select infrastructure settings and legacy inventory with `--config <path>`
or `SBM_CONFIG`. In authoritative mode, PostgreSQL owns proxy accounts and credentials.
Manage them in Django Admin, not in the inventory.

`deployment.client_region` optionally sets the time zone and language clients
present for the node; see
[the client guide](docs/README_SBC.md#region-matching). It affects only clients
that opt in with `sbc privacy region on`.

`sbm serve` runs Django on loopback at `control_plane.port` (default `47071`).
It requires `control_plane.enabled` and `control_plane.secret_key`.
Retain `web.session_secret` for installed legacy clients' machine tokens.
See [server configuration](docs/VPS_DEPLOYMENT.md#2-server-configuration).

`web.subscription_secret` keys the subscription links the `sbc` client fetches its
config from. Without it, `sbm serve` turns those links off.

The Trojan, Hysteria2, and Naive renderers build configurations in code and keep real
deployment secrets in an operator-owned encrypted runtime inventory. See
[`docs/SECRET_HANDLING.md`](docs/SECRET_HANDLING.md) before editing any user
credentials, obfs secrets, or TLS values.

`default_protocol` controls which protocol a new client install starts with when
no protocol is chosen. The server installer enables all packaged protocol inbounds
by default. A Naive inbound is omitted when its roster is empty or entirely disabled.
Subscriptions fall back to an enabled protocol when the account lacks the configured default.

If `traffic_stats.enabled` is turned on in the config file, `sbm release` builds a
custom Linux server `sing-box` binary from the upstream source tag with
`with_v2ray_api`, while client download archives continue using the official upstream
release assets.

Desktop mixed mode offers `china-direct` (the default), `gfw-proxy`, `ai-only`,
`global`, and `direct` routes. `china-direct` proxies unknown destinations.
`gfw-proxy` proxies GFWList and AI destinations and sends other traffic directly.
`direct` sends all allowed application traffic and its DNS directly, including AI.
The old names `china`, `gfw`, and `ai` remain aliases. See the
[CLI guide](docs/README_SBC.md#route-strategies) for route descriptions and diagnostics. Every client profile keeps the exact
`deployment.host` and optional `deployment.direct_domain_suffixes` direct, with
matching direct DNS rules. Suffixes cover the apex and subdomains.

Desktop proxy strategies and Android profiles proxy community-listed Anthropic
and AI destinations before China-direct rules. Desktop `direct` sends them directly.
Android retains AI priority over its mode overrides. The dedicated Anthropic
set and broader AI set replace local domain maintenance. See
[community AI rules](docs/RELEASE_AND_PORTAL.md#community-ai-rules) for sources and
coverage limits.

The `sbc` downloads include initial rule files, so the proxy
can start before rule downloads succeed. DNS answers persist across service restarts,
with normal DNS expiry. See the
[release guide](docs/RELEASE_AND_PORTAL.md#build-the-release) for build requirements.

`sbc` enables automatic configuration refresh and signed binary upgrades by default,
with separate 6h intervals. Use `sbc refresh auto off` or `sbc upgrade auto off` to
disable either policy. See [automatic maintenance](docs/README_SBC.md#automatic-maintenance)
for intervals, saved preferences, and migration from older clients.

`sbc lang` saves the language of `sbc` messages as `en`, `zh`, or `auto`;
`SBC_LANG` overrides it. See [CLI language](docs/README_SBC.md#cli-language).

`sbm release` can also read a repo-root `.env` for local operator secrets such as
`SOPS_AGE_KEY` and `GITHUB_TOKEN`; the secret-handling doc covers the tradeoffs.

`sbm release` uses the current files in `config/` as-is; it does not pin, reset,
or check out config state with git.

## Docs

- [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md): focused checks, docstale, and agent instruction maintenance
- [`docs/SECRET_HANDLING.md`](docs/SECRET_HANDLING.md): inventory creation, credentials, and TLS handling
- [`docs/VPS_DEPLOYMENT.md`](docs/VPS_DEPLOYMENT.md): primary end-to-end VPS deployment guide
- [`docs/RELEASE_AND_PORTAL.md`](docs/RELEASE_AND_PORTAL.md): build releases, run the portal, and download client packages
- [`docs/README_SBC.md`](docs/README_SBC.md): client installation, legacy migration, commands, help, and shell completion
- [`docs/WEBRTC.md`](docs/WEBRTC.md): Linux Chrome WebRTC protection and cleanup
- [`docs/README_WIN.md`](docs/README_WIN.md): Windows installer, scheduled-task layout, and uninstall
- [`docs/README_MOBILE.md`](docs/README_MOBILE.md): official iOS/Android import links, local QR codes, and device acceptance
- [`docs/REMOTE_DEPLOYMENT.md`](docs/REMOTE_DEPLOYMENT.md): sync the active release artifacts to a remote host and install them
- [`docs/CONTROL_PLANE.md`](docs/CONTROL_PLANE.md): Django portal, PostgreSQL accounts, private admin, and recovery
- [`docs/REFACTOR_SPEC.md`](docs/REFACTOR_SPEC.md): target architecture and refactoring milestones

## Common Commands

Every `sbm` operation requires `--config <path>` or exported `SBM_CONFIG`. Put the
global `--config` option before the subcommand. `--help` works without a config.
Use the same inventory for a release build and its deploy.
When the control plane is enabled, deployment installs `/usr/local/bin/sbm` on
the VPS. This wrapper sets `SBM_CONFIG` to the deployed runtime inventory if unset,
changes to the deployment root, and disables Python bytecode writes.
An explicit `--config` still overrides `SBM_CONFIG`.

Build release artifacts:

```sh
pixi run sbm --config config/inventory/runtime.sops.yaml release
```

Start Django after database migration and static-file collection:

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

Django operators with `control_plane.view_traffic` can open `/admin/traffic/`
and `/admin/traffic/connections/` through an SSH tunnel. This permission does not
permit account edits. The legacy inventory `admin` flag grants no Django permissions.

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

Manage the control plane on the VPS after deployment:

```sh
sudo -u sbm sbm control-plane migrate
sudo -u sbm sbm control-plane createsuperuser
# Initial import only, before enabling authoritative mode:
sudo -u sbm sbm control-plane import
sudo sbm control-plane sync
```

For import restrictions, account switches, and deployment modes, see
[the control-plane guide](docs/CONTROL_PLANE.md).

The full action names `create-admin`, `import-inventory`, and `sync-node` also work.
Other actions are `collect-static`, `serve`, `health`, and `check`.
Omitting the action runs `check`. Existing `control-plane --action ACTION` commands
remain supported. Use one action form per command.

From a workstation, both concise and full action names support remote execution:

```sh
pixi run sbm --config /path/to/runtime.sops.yaml control-plane sync --remote vpn-host --remote-root /opt/sing-box-manager
```

See [the control-plane guide](docs/CONTROL_PLANE.md) for permissions and recovery.

## Development

```sh
pixi install
pixi run test tests/test_cli.py
```

See [DEVELOPMENT.md](docs/DEVELOPMENT.md) for checks appropriate to the change.
