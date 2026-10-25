# AGENTS.md

Checked on 2026-10-25.

This repo builds `sing-box` releases, serves authenticated downloads, and deploys
the code and an already-built release directory to a VPS. Use Pixi and Python 3.12.

## Working scope

Local edits and fixture-based checks are authorized for the requested change.
Continue through relevant verification, fixes, and doc updates without asking for
approval at each step.

A code or docs task does not imply a live release, deploy, installation, or garbage
collection. For requested operations, use the target and authorization already
supplied. Ask only when a material detail or authorization is missing.

Explicit user instructions take precedence over project and skill guidelines.
Load a skill only when its task applies. If an instruction blocks requested work,
identify the file and exact instruction, explain the conflict, and continue any
unaffected work.

## Design direction

Prefer clean breaks when they reduce long-term complexity, maintenance or runtime cost.
Use mature frameworks and libraries instead of custom machinery with the same purpose.
Preserve useful data and security requirements, not obsolete implementation details.
State the cut list, migration boundary and acceptance evidence. Do not maintain parallel architectures indefinitely.
A design decision does not authorize a production cutover or data removal.

## Find the relevant context

Read the guide for the behavior being changed. A wording fix needs only the
surrounding context.

| Changing                                           | Read                                                                                                                                                                                                                                                                                                  |
| -------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Snell protocol, credentials and rollout            | [SNELL.md](docs/SNELL.md), [SECRET_HANDLING.md](docs/SECRET_HANDLING.md), [CONTROL_PLANE.md](docs/CONTROL_PLANE.md), [DEVELOPMENT.md](docs/DEVELOPMENT.md)                                                                                                                                            |
| Client terminal UI, colors and glyphs              | [DEVELOPMENT.md](docs/DEVELOPMENT.md#terminal-ui-design), [TUI_REFACTOR_PLAN.md](docs/TUI_REFACTOR_PLAN.md), [README_SBC.md](docs/README_SBC.md#terminal-output)                                                                                                                                      |
| CLI or settings                                    | [README.md](README.md)                                                                                                                                                                                                                                                                                |
| CLI message language                               | [README_SBC.md](docs/README_SBC.md#cli-language), [DEVELOPMENT.md](docs/DEVELOPMENT.md)                                                                                                                                                                                                               |
| Inventory, credentials, TLS, config rendering      | [SECRET_HANDLING.md](docs/SECRET_HANDLING.md)                                                                                                                                                                                                                                                         |
| Mobile routing, protocols, import links and QR     | [README_MOBILE.md](docs/README_MOBILE.md), [RELEASE_AND_PORTAL.md](docs/RELEASE_AND_PORTAL.md), [SECRET_HANDLING.md](docs/SECRET_HANDLING.md), [DEVELOPMENT.md](docs/DEVELOPMENT.md)                                                                                                                  |
| Release, portal, client configs, traffic stats     | [RELEASE_AND_PORTAL.md](docs/RELEASE_AND_PORTAL.md), [CONTROL_PLANE.md](docs/CONTROL_PLANE.md)                                                                                                                                                                                                        |
| Deploy, provisioning, SSH, server installers       | [VPS_DEPLOYMENT.md](docs/VPS_DEPLOYMENT.md), [REMOTE_DEPLOYMENT.md](docs/REMOTE_DEPLOYMENT.md)                                                                                                                                                                                                        |
| Unix installer and legacy migration                | [README_SBC.md](docs/README_SBC.md#unix-installation-and-migration), [RELEASE_AND_PORTAL.md](docs/RELEASE_AND_PORTAL.md#subscription-links)                                                                                                                                                           |
| Mixed-to-TUN environment migration                 | [TUN_ENV_MIGRATION.md](docs/TUN_ENV_MIGRATION.md), [README_SBC.md](docs/README_SBC.md#linux-tun), [DEVELOPMENT.md](docs/DEVELOPMENT.md)                                                                                                                                                               |
| Linux TUN                                          | [REFACTOR_SPEC.md](docs/REFACTOR_SPEC.md#m1b-linux-tun), [README_SBC.md](docs/README_SBC.md#linux-tun), [SECRET_HANDLING.md](docs/SECRET_HANDLING.md), [DEVELOPMENT.md](docs/DEVELOPMENT.md)                                                                                                          |
| Client diagnostics: IP, websites and throughput    | [CLIENT_DIAGNOSTICS.md](docs/CLIENT_DIAGNOSTICS.md), [README_SBC.md](docs/README_SBC.md#network-diagnostics), [SECRET_HANDLING.md](docs/SECRET_HANDLING.md), [DEVELOPMENT.md](docs/DEVELOPMENT.md)                                                                                                    |
| Client region matching (time zone and language)    | [README_SBC.md](docs/README_SBC.md#region-matching), [RELEASE_AND_PORTAL.md](docs/RELEASE_AND_PORTAL.md#subscription-links)                                                                                                                                                                           |
| Privacy: Chrome WebRTC, geolocation and language   | [WEBRTC.md](docs/WEBRTC.md), [README_SBC.md](docs/README_SBC.md#chrome-webrtc-protection), [Chrome language matching](docs/README_SBC.md#chrome-language-matching), [Geolocation blocking](docs/README_SBC.md#chrome-geolocation-blocking)                                                            |
| Windows client, including the hosted installer     | [README_WIN.md](docs/README_WIN.md)                                                                                                                                                                                                                                                                   |
| Checks, docstale, agent instructions and skills    | [DEVELOPMENT.md](docs/DEVELOPMENT.md), [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md)                                                                                                                                                                                                 |
| Django control plane and PostgreSQL                | [CONTROL_PLANE.md](docs/CONTROL_PLANE.md), [DEVELOPMENT.md](docs/DEVELOPMENT.md)                                                                                                                                                                                                                      |
| Node agent, multi-node support and node operations | [MULTI_NODE_SPEC.md](docs/MULTI_NODE_SPEC.md), [NODE_ACCEPTANCE.md](docs/NODE_ACCEPTANCE.md), [CONTROL_PLANE.md](docs/CONTROL_PLANE.md), [SECRET_HANDLING.md](docs/SECRET_HANDLING.md), [DEVELOPMENT.md](docs/DEVELOPMENT.md)                                                                         |
| Node traffic wire protocol and codecs              | [NODE_TRAFFIC_PROTOCOL.md](docs/NODE_TRAFFIC_PROTOCOL.md), [NODE_ACCEPTANCE.md](docs/NODE_ACCEPTANCE.md#traffic-wire-codec), [DEVELOPMENT.md](docs/DEVELOPMENT.md#traffic-accounting-checks), [MULTI_NODE_SPEC.md](docs/MULTI_NODE_SPEC.md#7-traffic-accounting-and-synchronization)                  |
| Refactoring milestones, target architecture        | [REFACTOR_SPEC.md](docs/REFACTOR_SPEC.md)                                                                                                                                                                                                                                                             |
| Go client (`sbc`) and its hosted installer         | [README_SBC.md](docs/README_SBC.md), [REFACTOR_SPEC.md](docs/REFACTOR_SPEC.md#m1-client-cli-for-linux-and-macos), its [M2](docs/REFACTOR_SPEC.md#m2-client-cli-for-windows) for Windows, [RELEASE_AND_PORTAL.md](docs/RELEASE_AND_PORTAL.md#one-liner-install), [DEVELOPMENT.md](docs/DEVELOPMENT.md) |

## Project constraints

- Read the secret-handling guide before accessing runtime inventory, passwords, or
  TLS material. Treat inventory, `.env`, keys, and generated releases as sensitive;
  keep their values out of logs and committed templates. Use examples and fixtures
  for development checks. This is public source: never commit real inventory,
  including encrypted inventory, or operator state. Only the example inventory is
  tracked. Keep public PR jobs on GitHub-hosted runners.
- `sbm release` uses the working tree config as-is. Do not add git-based config
  resets. Every `sbm` operation requires `--config PATH` or exported `SBM_CONFIG`.
- Preserve authentication, host validation, and download path checks. The hosted Unix
  installer must stay POSIX sh compatible. Shared Bash scripts and shell integration
  must stay bash 3.2 compatible, including code sourced by zsh.
  Windows client scripts must stay Windows PowerShell 5.1 compatible.
- `sbc` uses Cobra and pflag for its command layer and Charmbracelet (`lipgloss`,
  `bubbletea`, and `bubbles`) for terminal rendering and interactive selection.
  Keep other client code standard-library-only. Build with cgo off.
- Use modern terminal features when supported. Respect the user's theme through
  default foreground/background colors and ANSI palette slots. Keep the UI usable
  in Ubuntu 18.04's default GNOME Terminal without Nerd Fonts or other patched
  fonts. Follow the [terminal UI design requirements](docs/DEVELOPMENT.md#terminal-ui-design).

## Completion

Use the checks in [DEVELOPMENT.md](docs/DEVELOPMENT.md) that fit the change. For
docs-only edits, validate commands, links, and formatting. Pixi style and lint tasks
can rewrite files.

Use `pixi run docstale check` to find docs whose sources changed since their last
review. Exit `2` requests review; update durable facts when needed, without editing
just to silence the reminder, then run `pixi run docstale stamp <document>`. Keep
the context table and `docstale.toml` aligned when adding or moving mapped code or
docs.

Finish when the requested behavior and relevant docs are updated and the chosen
checks pass, or report a concrete blocker. State what changed and what was verified.
