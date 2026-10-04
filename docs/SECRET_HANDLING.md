# Secret Handling and Runtime Inventory

Do not put real passwords, obfs secrets or inline TLS material into configuration
builders or checked-in fixtures.

## Why this changed

The old layout mixed secret data with hand-edited JSON config:

- Trojan users lived directly in `config/server/trojan-server.json`
- archived Hysteria2 config carried live user passwords, the shared obfs secret, and
  inline TLS material
- client configs carried real endpoints and protocol credentials
- the web portal depended on the same credential source as the proxy configs

The current layout separates those jobs:

- `sing_box_manager/release/profiles.py`, `release/renderers.py` and
  `desktop_config.py` build configurations in code; no JSON templates are loaded
- an operator-owned `*.sops.yaml` inventory is the encrypted source of truth for
  deploy facts, portal auth, and protocol credentials; it is not tracked in this
  public repository
- `config/generated/auth-users.json` is a generated auth snapshot for the web portal
- `sbm serve` verifies portal credentials against the generated auth snapshot;
  it also loads app settings from the file selected by `--config` or `SBM_CONFIG`
- `sbm release` decrypts the inventory, renders Trojan, Hysteria2, and Naive configs, and
  refreshes the auth snapshot

## Files you should care about

- `.sops.example.yaml` is the SOPS creation-rule template; copy it to the ignored
  repo-root `.sops.yaml` and set your own public Age recipient
- `config/inventory/example.yaml` is the full config+inventory schema example
- `config/inventory/runtime.sops.yaml` is an ignored local config path, not a
  bundled inventory; select it explicitly with `--config` or `SBM_CONFIG`. Keeping
  the inventory outside the checkout or in a separate private repository is preferred
- `config/generated/auth-users.json` is generated output; do not hand-edit it
- `sing_box_manager/settings.py` validates application settings and inventory with
  one strict Pydantic root model; unknown keys fail validation
- `tests/fixtures/profiles/` holds golden outputs with example credentials only

If you operate multiple VPS or domain variants, pass the specific `*.sops.yaml` file
via `--config` or `SBM_CONFIG` for the environment you want `sbm` to use.

## Config file shape

The unified config file contains both app settings and inventory data:

```yaml
sing_box_version: 1.14.2

deployment:
  host: vpn.example.com
  direct_domain_suffixes: []
  ip: 203.0.113.42
  trojan_port: 8443
  hysteria2_port: 4443
  naive_port: 9443
  tls:
    enabled: true
    server_name: vpn.example.com
    key_path: /run/secrets/trojan.key
    certificate_path: /run/secrets/trojan.crt
    self_signed_cert: false

web_portal:
  users:
    - username: alice
      password_hash: $argon2id$...
      enabled: true
      admin: true

trojan:
  users:
    - username: alice
      password: change-me
      enabled: true

hysteria2:
  obfs_password: change-me-obfs
  users:
    - username: alice
      password: change-me
      enabled: true

naive:
  users:
    - username: alice
      password: change-me
      enabled: true
```

Important distinctions:

- `deployment.host` is deployment metadata and an exact direct exception in every client profile
- `deployment.direct_domain_suffixes` defaults to `[]`; each explicit domain covers its apex and subdomains for direct routing and DNS. Names are lowercased, optional outer dots removed, and duplicates removed. URLs, wildcards, IP suffixes, and malformed names are rejected. Parent domains are never inferred from the host
- `deployment.ip` is the client connection target rendered into outbound `server`
- `deployment.hysteria2_port` is the rendered Hysteria2 server and client port
- `deployment.naive_port` is the rendered Naive server and client port
- `deployment.tls.enabled` controls whether rendered protocol configs keep TLS enabled
- `deployment.tls.server_name` is rendered into Trojan, Hysteria2, and Naive `tls.server_name`
- `deployment.tls.key_path` and `deployment.tls.certificate_path` are rendered only into server configs
- `deployment.tls.self_signed_cert` defaults to `false`; set it to `true` when the referenced certificate and key files are self-signed PEM sources that should be inlined into rendered sing-box configs
- `web_portal.users[].password_hash` is only for portal login verification
- `web_portal.users[].admin` defaults to `false`; set it to `true` only for users who can view all users' traffic stats at `/admin`
- `client_upgrade.policy` and `client_upgrade.message` are public portal metadata;
  keep the optional message short and never include credentials or private details
- `trojan.users[].password` is only for Trojan config rendering
- `hysteria2.users[].password` is only for Hysteria2 config rendering
- `naive.users[].password` is only for Naive config rendering
- an empty or entirely disabled `naive.users` roster omits the Naive server inbound; sing-box 1.14.2 requires at least one user for it
- `hysteria2.obfs_password` is the shared Hysteria2 salamander secret

The configuration builders target sing-box 1.14.2. Hysteria2 keeps its default Chrome QUIC
parroting, salamander obfuscation, and any configured bandwidth or port hopping.
Chrome parroting requires an RSA or ECDSA server certificate; Ed25519 certificates
are incompatible. Check the certificate used by the target server before its
upgrade. See the [Hysteria2 outbound documentation](https://sing-box.sagernet.org/configuration/outbound/hysteria2/).

Desktop DNS cache files contain queried domains and answers. They stay in the
client's private state directory and are separate for each protocol and route.
Release packages contain public rule snapshots, never a client's DNS cache.

Usernames may appear in one section, two sections, or all protocol sections. The sections are
related by username, but they are stored independently.

## What Argon2id means

`argon2id` is a password hashing algorithm.

- it protects the portal login without storing the plaintext password
- it is one-way, so you cannot recover the original password from the hash
- the app verifies a submitted password against the stored hash during login

Format illustration (the salt and hash below are placeholders):

```text
$argon2id$v=19$m=65536,t=3,p=4$<salt>$<hash>
```

If you forget the plaintext portal password, you cannot recover it from
`web_portal.users[].password_hash`. Generate a new hash and replace it.

## Where the real secrets live

- the real portal login verifiers live under `web_portal.users[].password_hash`
- the admin portal flag lives under `web_portal.users[].admin`; it is copied into `config/generated/auth-users.json`
- the real Trojan passwords live under `trojan.users[].password`
- the real Hysteria2 passwords live under `hysteria2.users[].password`
- the real Naive passwords live under `naive.users[].password`
- the shared Hysteria2 obfs secret lives under `hysteria2.obfs_password`
- KiwiVM credentials live under `vps_info.kiwi_veid` and
  `vps_info.kiwi_api_key`; ordinary deploys copy them into the server runtime
  config, mode `0640` and owned by `root:sbm`, so the portal can publish VPS
  information. The collector also
  reads them to derive the billing cycle boundary from the VPS's own reset date
- `web.session_secret` signs portal sessions and installer machine tokens, and
  `web.subscription_secret` keys subscription links, so anyone who holds it can
  compute every user's link. Deploy generates either one on the server when the
  inventory leaves it unset, and keeps the server's value afterwards
- `client_signing_key` is the base64 ed25519 private key that signs the manifest of
  `sbc` downloads. It is a top-level setting rather than inventory, so deploy never
  copies it to the server. `sbc` carries the matching public keys in `internal/trust/trust.go`, so replacing
  the key means shipping an `sbc` that trusts the new one first. Keep a backup. Generate one,
  and print its public key, with:

  ```sh
  pixi run python -c "from sing_box_manager.release.client_downloads import generate_key; print(generate_key())"
  pixi run python -c "import sys; from sing_box_manager.release.client_downloads import public_key; print(public_key(sys.argv[1]))" "$KEY"
  ```

- `traffic_stats.connection_api_secret` is the bearer token for the shared
  connection stream. It is written into `server/config.json`, so treat it like
  the protocol passwords beside it. Empty disables authentication and is accepted
  only while `connection_api_listen` stays on loopback. The unauthenticated
  `api_listen` counter listener must always stay on loopback
- per-user traffic history is not a secret but is user data; it lives outside the
  deploy root at `traffic_stats.database_path`, which defaults to
  `/var/lib/sing-box-manager/traffic-stats.sqlite3` and is created by systemd as
  a mode `0750` `StateDirectory` owned by the `sbm` service user
- the same database now also records which domains and destination IPs each user
  reached. That is a real step up in sensitivity from byte totals: keep the file
  where it is, and note that `traffic_stats.domain_top_n` bounds how much of it is
  retained by name
- TLS certificate and key source files live at `deployment.tls.certificate_path` and `deployment.tls.key_path`
- when `deployment.tls.self_signed_cert` is `true`, `sbm release` reads those files and inlines their PEM lines into the rendered sing-box JSON
- use `pixi run sops <your config file>` to edit the encrypted inventory
- `config/generated/auth-users.json` stores only the generated portal auth snapshot

## TLS handling now

The repo models both CA-signed and self-signed TLS with path-based inventory values
and keeps real PEM material out of checked-in code and fixtures.

That means:

- `deployment.tls` now carries `enabled`, `server_name`, `key_path`, `certificate_path`, and `self_signed_cert`
- rendered Trojan, Hysteria2, and Naive server configs use `key_path` and `certificate_path` directly for CA-signed mode
- rendered Trojan, Hysteria2, and Naive client configs read only `server_name` for CA-signed mode
- when `deployment.tls.self_signed_cert` is `true`, both client and server rendering read the PEM files from `certificate_path`, and server rendering also reads the PEM file from `key_path`
- self-signed rendered client configs inline `tls.certificate` line arrays and strip path fields
- self-signed rendered server configs inline `tls.certificate` and `tls.key` line arrays and strip path fields
- Naive supports the same self-signed rendering path, but upstream sing-box docs discourage self-signed Naive deployments for production because it changes Naive traffic behavior
- client renderers never emit server private keys or TLS path fields
- checked-in configuration builders and golden fixtures contain no real PEM material
- generated server packages become more sensitive in self-signed mode because they contain the rendered private key

## Age and sops setup

Never commit a real inventory or an Age private key to this public repository,
including encrypted inventories. Only `config/inventory/example.yaml` is tracked.
The local `.sops.yaml` is ignored; `.sops.example.yaml` contains no operator recipient.

Generate a local Age key pair if you do not have one yet:

```sh
umask 077
mkdir -p ~/.config/sops/age
pixi run age-keygen -o ~/.config/sops/age/keys.txt
```

The command reports the public recipient. Copy the template, then replace its
placeholder with that public recipient before encrypting anything:

```sh
cp .sops.example.yaml .sops.yaml
editor .sops.yaml
```

Never put the private key in `.sops.yaml`. To edit an existing inventory, you need
a private key for one of its recipients; generating a new key does not grant access.

For a new inventory, encrypt the example before adding real values. Run the command
only when the output file does not already exist:

```sh
pixi run sops --encrypt \
  --filename-override config/inventory/runtime.sops.yaml \
  --output config/inventory/runtime.sops.yaml \
  config/inventory/example.yaml
```

The filename override selects the `.sops.yaml` creation rule while reading the
plaintext example. Then edit the encrypted file:

```sh
pixi run sops config/inventory/runtime.sops.yaml
```

Check decryption without printing credentials:

```sh
pixi run sops --decrypt config/inventory/runtime.sops.yaml > /dev/null
```

If `sops` cannot decrypt the file, check that your private key is available through the
default Age key location, `SOPS_AGE_KEY`, or `SOPS_AGE_KEY_FILE`.

Every `sbm` operation needs the selected config path. The filename does not create
an automatic default:

```sh
export SBM_CONFIG=config/inventory/runtime.sops.yaml
```

For local development, a plaintext `.yaml` outside the checkout is supported. Keep
its file permissions restrictive and select it with `--config` or `SBM_CONFIG`.
Keep production inventory in an operator-owned location or a separate private
repository. Public CI uses only example inventory and generated test fixtures.
When `serve` receives a `.sops.yaml` config, it needs decryption access to load app
settings even though it uses the generated snapshot for authentication. Deploy
avoids that requirement on the VPS by supplying a derived plaintext runtime config.

## Repo-local `.env` support

`sbm` loads a repo-root `.env` file on demand before it:

- runs `sops --decrypt` for a `*.sops.yaml` inventory
- reads `GITHUB_TOKEN` for upstream release metadata downloads

That makes these variables useful in `.env`:

- `SOPS_AGE_KEY` for `sbm release`
- `GITHUB_TOKEN` for `sbm release`

Create a local secret file with restrictive permissions, then edit it locally:

```sh
umask 077
touch .env
chmod 600 .env
editor .env
```

Important limits:

- `.env` is plaintext on disk even when it is gitignored; treat it like a local secret file
- exported shell variables still win over `.env`
- raw `sops ...` commands do not read `.env` automatically; they still need an exported `SOPS_AGE_KEY`, `SOPS_AGE_KEY_FILE`, or the default Age key file

If Bitwarden is your source of truth, keep the Age private key in Bitwarden and treat
`.env` as a local convenience cache rather than the canonical secret store.

## Migrating from the old plaintext model

The current split schema maps the old plaintext data like this:

- old Trojan users -> `trojan.users`
- old Hysteria2 users -> `hysteria2.users`
- old Naive users -> `naive.users`
- old Hysteria2 obfs secret -> `hysteria2.obfs_password`
- old Trojan client endpoint values -> `deployment.host`, `deployment.ip`, and
  `deployment.trojan_port`
- old Hysteria2 client endpoint values -> `deployment.host`, `deployment.ip`, and
  `deployment.hysteria2_port`
- old Naive client endpoint values -> `deployment.host`, `deployment.ip`, and
  `deployment.naive_port`
- old rendered TLS `server_name` -> `deployment.tls.server_name`
- old server certificate and key paths -> `deployment.tls.certificate_path` and
  `deployment.tls.key_path`
- old inline TLS PEM blocks are not part of the inventory schema; self-signed mode regenerates them from the referenced PEM files during release rendering
- old portal login verifier data -> `web_portal.users[].password_hash`

## Editing rules for future maintainers

- keep real inventory, encrypted or plaintext, outside public source control
- edit the encrypted inventory with `sops`
- keep `GITHUB_TOKEN` in your shell or secret manager, not in the config file
- treat a repo-root `.env` as sensitive plaintext if you use it for `SOPS_AGE_KEY` or `GITHUB_TOKEN`
- keep `config/inventory/example.yaml` safe to commit
- keep config builders and golden fixtures free of real passwords, endpoints, obfs secrets and TLS material
- keep server-only TLS paths in inventory, never in client configurations
- keep `deployment.tls.self_signed_cert` at `false` for CA-signed TLS and set it to `true` only when the referenced PEM files should be embedded into rendered sing-box configs
- treat generated server packages as sensitive when `deployment.tls.self_signed_cert` is `true`
- do not hand-edit `config/generated/auth-users.json`
- do not reintroduce shared secret fields across portal, Trojan, Hysteria2, and Naive sections
- do not hardcode per-client certificate blobs in Hysteria2 or Naive configuration builders

## Common tasks

Generate a new portal password hash with a hidden prompt, keeping the password out
of command arguments and shell history:

```sh
pixi run python -c 'from getpass import getpass; from argon2 import PasswordHasher; print(PasswordHasher().hash(getpass("Portal password: ")))'
```

Rebuild rendered artifacts after an inventory change:

```sh
pixi run sbm --config config/inventory/runtime.sops.yaml release
```

If you prefer a repo-local `.env`, `pixi run sbm release` loads `SOPS_AGE_KEY` and
`GITHUB_TOKEN` from it automatically when those variables are not already exported.

The upstream `SagerNet/sing-box` repository is public, so `pixi run sbm release` still works
without `GITHUB_TOKEN`. Maintainers should still export the token before release runs to
avoid the lower anonymous GitHub API rate limits.

Start the web service after the auth snapshot exists:

```sh
pixi run sbm --config config/inventory/runtime.sops.yaml serve
```
