# TUN Mode (`proxy tun`)

Design spec for a `proxy tun on|off` toggle that runs the Linux client as a
transparent TUN tunnel instead of a local mixed inbound.

Status: implemented in the bash client. Releases no longer pack that client or
render the Linux TUN profiles, so only machines that already run it have TUN mode,
and TUN in `sbc` is deferred. Scope: native Linux/systemd. macOS, Windows, WSL2,
containers, and remote activation are follow-ups (Section 15). Section 16
records where the implementation diverges from this document.

Where the code lives:

- `scripts/lib/tun.sh` — managed paths, manifest and attestation readers, the
  safely-off predicate, the transaction lock, and the unit template. Shared by
  `setup.sh`, `client-install.sh`, and `client-uninstall.sh`, because all three
  need the same answers about who owns the tunnel and whether it is running.
- `scripts/setup.sh` — the `proxy tun` command surface, preflight, the staged
  transaction, rollback, guards, and `proxy check tun`.

## 1. Motivation

Today the desktop client listens on a mixed inbound at `127.0.0.1:1080` (`proxy on`
moves to the next free port when 1080 is occupied, keeps the port an already-running
client is serving, and honours `--port` to pin one).
`proxy on` starts the client and points the shell, Git, and desktop proxy at that
inbound. `proxy docker on` separately configures Docker. Programs that ignore
those settings, including programs that resolve DNS themselves, bypass the
proxy.

TUN mode captures traffic at the network layer, so applications do not need to
opt in. The trade-off is privilege: a self-managed TUN needs `CAP_NET_ADMIN`,
while the existing client is deliberately user-scoped.

## 2. Constraints and trust model

These constraints are load-bearing.

- **The normal client remains non-root.** `client-install.sh` continues to
  reject root execution, and mixed units remain user-scoped.
- **The TUN is machine-wide.** One TUN changes routing for every local user, so
  there can be only one active TUN service and one owning UID at a time.
- **The sudo-authenticated user is trusted to provision TUN mode.** This is the
  same trust granted by unrestricted sudo. The long-running sing-box process is
  still isolated under a dedicated system account rather than UID 0.
- **The system service cannot execute from `$HOME`.** A user-writable executable
  could be replaced after activation, and an encrypted home may not be mounted
  at boot.
- **Mixed proxy settings are wrong under TUN.** The TUN profile has no mixed
  inbound, so commands that point applications at the mixed inbound must be
  blocked while TUN is active.
- **Client shell remains bash 3.2 compatible.** `tests/test_shell_compat.py`
  continues to ban associative arrays, `mapfile`, namerefs, `wait -n`, `coproc`,
  `&>>`, and case-modifying expansions.
- **Every user-facing string goes through `_t`.** New English strings need
  corresponding `_translate_zh()` cases and direct translation tests.

## 3. Packaging prerequisites

The release and user-install changes land before the privileged toggle. Releases no
longer render these profiles or pack Linux archives; this section records the layout
that existing installs have.

### 3.1 A separate Linux TUN profile

Android/SFA mobile profiles are built in `sing_box_manager/release/profiles.py`,
where the platform owns the tunnel. M3 removed the old JSON template directory
without changing their tunnel semantics.

Existing Linux installations keep separate self-managed rendered files:

```
trojan-linux-tun-client.json
hysteria2-linux-tun-client.json
naive-linux-tun-client.json
```

Mixed, mobile TUN and Linux self-managed TUN are distinct profiles. Releases now
ship mobile profiles only; the bash client's Linux TUN layout below remains until M1b. Linux TUN rendering passes `set_system_proxy=None`, so the renderer
does not add `set_system_proxy` to a TUN inbound. Android remains TUN-only.

### 3.2 Linux packages ship both profiles

Every allowed protocol in a Linux client package contains both rendered files:

```
<protocol>-client.json
<protocol>-linux-tun-client.json
```

`client-install.sh` must install both. It is not untouched by this feature.

Installed layout, using the existing XDG-aware config root:

```
${XDG_CONFIG_HOME:-$HOME/.config}/sing-box/<protocol>/config.json
${XDG_CONFIG_HOME:-$HOME/.config}/sing-box/<protocol>/config-tun.json
```

Protocol directories are `0700`; both configs are `0600`. The installer applies
those modes explicitly rather than relying on the archive mode or caller's
umask.

### 3.3 Mixed services load one exact file

The current user unit uses `sing-box run -C <protocol-directory>`, which would
load both profiles from the layout above. Before the second file is installed,
the Linux mixed unit changes to:

```
sing-box run -D <state-directory> -c <protocol-directory>/config.json
```

The release and installer tests must run `sing-box check` against each generated
profile independently.

## 4. Privileged architecture

TUN mode uses one fixed system service, not one service per protocol. A
singleton prevents two protocol TUNs from becoming active at once and keeps
systemd as the authority for active/enabled state.

### 4.1 Managed paths

| Path                                                   | Mode   | Owner                       | Purpose                                                            |
| ------------------------------------------------------ | ------ | --------------------------- | ------------------------------------------------------------------ |
| `/usr/local/libexec/sing-box-manager-tun/`             | `0755` | `root:root`                 | Dedicated binary directory; does not collide with server installs  |
| `/usr/local/libexec/sing-box-manager-tun/sing-box`     | `0755` | `root:root`                 | Privileged copy of the selected client binary                      |
| `/usr/local/libexec/sing-box-manager-tun/libcronet.so` | `0644` | `root:root`                 | Optional Naive runtime library                                     |
| `/etc/sing-box-manager/tun/`                           | `0750` | `root:sing-box-tun`         | Managed TUN configuration root                                     |
| `/etc/sing-box-manager/tun/config.json`                | `0640` | `root:sing-box-tun`         | Active TUN config; contains protocol credentials                   |
| `/etc/sing-box-manager/tun/manifest`                   | `0600` | `root:root`                 | Ownership and provenance metadata, including secret-derived hashes |
| `/etc/sing-box-manager/tun/rules/`                     | `0755` | `root:root`                 | Optional packaged, non-secret rule sets                            |
| `/etc/systemd/system/sing-box-manager-tun.service`     | `0644` | `root:root`                 | Singleton system unit                                              |
| `/var/lib/sing-box-manager-tun/`                       | `0700` | `sing-box-tun:sing-box-tun` | sing-box runtime state                                             |
| `/var/lib/sing-box-manager-tun-control/`               | `0700` | `root:root`                 | Durable transaction journal and previous managed generation        |
| `/var/lib/sing-box-manager-tun.transaction`            | `0644` | `root:root`                 | Non-secret transition marker checked by mixed-mode guards          |
| `/var/lib/sing-box-manager-tun.status`                 | `0644` | `root:root`                 | Non-secret root attestation of `safe-off`, `active`, or `dirty`    |

The manifest contains at least:

```
schema_version=1
owner_uid=<numeric uid>
protocol=<trojan|hysteria2|naive>
generation=<opaque generation id>
binary_sha256=<sha256>
cronet_sha256=<sha256|absent>
config_sha256=<sha256>
unit_sha256=<sha256>
unit_version=<integer>
runtime_uid=<numeric uid>
runtime_gid=<numeric gid>
runtime_account_created=<true|false>
rule.<name>.sha256=<sha256>  # zero or more packaged rule sets
```

It contains no credentials or rendered config values, but the config hash is a
verifier derived from secret-bearing content and therefore remains root-only.
It is provenance metadata, not a second source of runtime state.

### 4.2 Runtime account

Provisioning creates a locked `sing-box-tun` system account when it does not
exist. If an existing account with that name is not the expected system account,
provisioning refuses to continue. Uninstall removes the account only when the
manifest proves this client created it and no managed files or units still use
it.

### 4.3 Unit template

The unit starts with the minimum known capability. Integration testing may add
`CAP_NET_RAW` only if a supported profile cannot run without it.

```ini
[Unit]
Description=sing-box-manager TUN client
Documentation=https://sing-box.sagernet.org
After=network-online.target nss-lookup.target
Wants=network-online.target
StartLimitIntervalSec=60
StartLimitBurst=3

[Service]
Type=exec
User=sing-box-tun
Group=sing-box-tun
UMask=0077
StateDirectory=sing-box-manager-tun
StateDirectoryMode=0700
CapabilityBoundingSet=CAP_NET_ADMIN
AmbientCapabilities=CAP_NET_ADMIN
NoNewPrivileges=true
PrivateTmp=true
ProtectControlGroups=true
ProtectHome=true
ProtectKernelModules=true
ProtectKernelTunables=true
ProtectSystem=strict
DevicePolicy=closed
DeviceAllow=/dev/net/tun rw
ExecStartPre=/usr/local/libexec/sing-box-manager-tun/sing-box check -c /etc/sing-box-manager/tun/config.json
ExecStart=/usr/local/libexec/sing-box-manager-tun/sing-box run -D /var/lib/sing-box-manager-tun -c /etc/sing-box-manager/tun/config.json
Restart=on-failure
RestartSec=5s
TimeoutStartSec=30s
TimeoutStopSec=30s
LimitNOFILE=infinity

[Install]
WantedBy=multi-user.target
```

`CAP_NET_BIND_SERVICE` is not granted because the TUN profile has no low-port
listener. The service account cannot modify its binary, config, manifest, unit,
or rollback copy.

## 5. Provisioning model

`client-install.sh` remains non-root, but it changes as described in Section 3.
The privileged half is provisioned lazily by `proxy tun on`, so the sudo prompt
appears only when the user asks for machine-wide routing.

Provisioning follows these rules:

- source binary, optional `libcronet.so`, config, and rule-set paths are resolved
  through existing XDG-aware helpers
- sources must be regular files and not symlinks
- user-readable sources are opened by the unprivileged shell and streamed into
  root-owned temporary files created on each destination filesystem; privileged
  commands do not dereference a user-controlled source path
- the unit is generated from a fixed template, not copied from a user-writable
  unit file
- all live files are installed with explicit owner and mode
- all operations use a root-owned lock shared by `tun on`, `tun off`, protocol
  switching, and uninstall
- existing live files are classified through the manifest before overwrite or
  removal; binary, Cronet library, config, rule-set, unit, owner, and mode
  mismatches cause a refusal rather than deletion
- a durable root-only journal records transaction phase and previous-generation
  paths; the manifest is replaced last and acts as the publication commit marker

Provisioning stages and validates on every `proxy tun on`, including when the
service is already active. Content hashes, not mtimes, determine whether files
match. An active service is restarted so new credentials, endpoints, config,
binary, and runtime library actually take effect.

## 6. Command surface

```
proxy tun on [--persist]    Provision, validate, and start TUN mode
proxy tun off               Disable and stop TUN mode
proxy check tun             Capability, ownership, and state report
```

`proxy tun off` does not restore mixed mode. The user runs `proxy on` when they
want to return to the mixed inbound. Silently enabling a service or proxy
setting the user did not request is worse than one extra command.

`_proxy_complete_candidates` gains `tun`, `on`, `off`, `--persist`, and the
`check tun` candidate as appropriate. Linux-only commands are not advertised on
macOS.

## 7. State and ownership model

Runtime state comes from the fixed system unit:

```
systemctl is-active sing-box-manager-tun.service
systemctl is-enabled sing-box-manager-tun.service
```

The manifest answers different questions: who owns the machine-wide TUN, which
protocol and generation were provisioned, and whether the managed files still
match their recorded hashes.

The state resolver distinguishes at least:

- unit absent
- installed and inactive
- activating
- active
- deactivating
- failed or restart-looping
- active with missing, foreign, or mismatched manifest
- owned by the current UID
- owned by another UID
- interrupted transaction requiring recovery

Only the manifest owner may mutate an existing managed installation. A different
UID receives an error identifying that TUN mode is owned by another local user;
automatic takeover is not supported.

The existing per-user `selected-protocol` file remains the desired protocol for
mixed mode and for the next TUN activation. While TUN is active, the manifest's
protocol is the authoritative active protocol.

The per-user `selected-port` file applies only to the mixed inbound. `proxy port`
may display or change that selection while TUN is active; a change updates the
installed mixed configs for the next mixed activation without restarting or
otherwise modifying the running TUN service.

Mixed mode may start only when TUN is **safely off**. A never-installed TUN is
safely off when the unit and public transaction marker are both absent. An
installed TUN is safely off only when the singleton unit is inactive and
disabled, with no activating/deactivating job or restart loop, no public
transaction marker, and a root-maintained `safe-off` status attesting that its
interface and managed route/rule artifacts were removed. The privileged path
sets that attestation only after cleanup verification and changes it to `dirty`
before every mutation. Failed or ambiguous states require `proxy tun off`
recovery rather than being treated as inactive.

## 8. Activation and rollback

Ordering is designed to avoid tearing down a working mixed proxy before the TUN
payload has been validated and to recover when route changes terminate the
controlling process.

### 8.1 Preflight and staging

Before changing services, routes, or live payload files, `proxy tun on` performs
cheap user-scope checks, authenticates sudo, acquires the root transaction lock,
and repeats every authoritative ownership, path, unit, and network check under
that lock. It verifies:

1. the host is native Linux, not macOS, WSL2, or a container
2. a functioning systemd system manager and system bus are available
3. `/dev/net/tun` exists and the intended runtime account is either absent or an
   expected locked system account
4. sudo authentication succeeds before the network is changed
5. the selected protocol resolves and its mixed and TUN configs exist
6. the binary and TUN config are regular, non-symlink files
7. the fixed TUN interface name and address ranges do not conflict with foreign
   interfaces, routes, or another VPN; artifacts belonging to the currently
   active managed generation are recognized as ours during re-sync
8. no SSH or other detected remote shell is controlling activation
9. an existing manifest is either owned by the current UID or absent
10. existing managed paths are absent or match the manifest
11. no mixed service or legacy sing-box client owned by another local UID is
    active; the transaction manages only the provisioning user's user manager

The first implementation refuses WSL2, containers, and remote shells rather than
claiming partial support. It does not use the presence of an `nft` executable as
a proxy for kernel capability.

After the locked non-mutating checks, the command writes the durable journal and
public transaction marker, records the prior runtime-account state, and creates
the account when absent as the first reversible mutation. It then proves the
actual capability boundary by creating and removing a disposable TUN under that
UID, capability set, and device policy. Probe failure removes an account created
by this transaction and clears the journal/marker.

The command then streams the binary, optional Cronet library, config, and
optional rule sets into root-owned temporary files on their respective
destination filesystems. It writes the fixed unit and runs the staged binary's
`sing-box check` against the staged config before changing live payload or
network state.

### 8.2 Transaction

```
1. snapshot    record current TUN generation and active/enabled state;
               record every manager-owned mixed service's active/enabled state;
               record exact manager-owned proxy values that may be cleared
2. safeguard   extend the durable journal with the snapshots; mark status dirty;
               arm a root-owned transient timer that stops the TUN unit on timeout
3. quiesce     temporarily disable and stop the old TUN; stop every manager-owned
               mixed service; refuse unexpected services or network artifacts;
               clear only shell/Git/desktop/Docker values classified as ours
4. publish     save the previous root generation; rename each staged file on its
               own destination filesystem; install the manifest last as the
               commit marker; run systemctl daemon-reload and reset-failed;
               repeat the no-mixed check immediately before starting TUN
5. start       start sing-box-manager-tun.service without enabling it yet
6. verify      poll for a stable PID and restart count; verify the expected TUN
               interface, routes/rules, DNS, proxy-free egress, and increasing TUN
               packet counters with all application proxy variables disabled
7. persist     enable only after verification when --persist was requested or
               the previous TUN was enabled; otherwise leave it disabled
8. commit      cancel and confirm the safeguard; mark status active; remove the
               journal, transaction marker, and rollback files; release the lock
```

The TUN template uses a fixed interface name such as `sbm-tun0`, allowing the
verification step to prove that the egress probe traversed the TUN rather than a
working direct route. Verification uses bounded polling because `is-active`
alone can race startup and remote initialization.

Mixed and TUN services never overlap. The TUN's route capture could otherwise
capture the mixed instance's own outbound and loop.

Publication is not described as one cross-filesystem atomic rename: `/usr/local`,
`/etc`, and `/var/lib` may be separate filesystems. Each file is staged beside
its destination and renamed atomically while both proxy paths are quiesced. The
root-only journal records every completed rename, the old files remain available
until verification succeeds, and the manifest is written last. A later TUN
command detects an interrupted journal and restores the previous generation
before doing new work.

The TUN unit is temporarily disabled before the first live-file change, even
during re-sync of a persistent installation. Therefore an interruption or
reboot cannot start an unverified generation. The transient safeguard handles a
lost controlling process; the durable journal handles a reboot. Previous active
and enabled states are restored independently.

### 8.3 Rollback

Rollback runs on every error or interruption after the first mutation, not only
on start or egress failure.

Rollback order is mandatory:

1. disable and stop the attempted TUN unit
2. wait for it to become inactive and verify its interface, routes, and rules
   are gone
3. restore the complete previous root generation, including binary, optional
   Cronet library, config, optional rule sets, manifest, and unit
4. run `systemctl daemon-reload` after restoring or removing the unit and
   `systemctl reset-failed` before any restored start
5. restore the previous runtime-account state; when this transaction created the
   account and no previous generation needs it, remove its runtime state and the
   account after confirming that no unit references it
6. restore the previous TUN enabled state, then restore its active state
   independently
7. only when the previous TUN was absent or safely off, restore every mixed
   service's previous active and enabled states independently
8. restore the exact manager-owned proxy values changed by the transaction
9. after the restored path is healthy, cancel the safeguard and confirm that its
   timer can no longer stop the singleton
10. write the correct `active`, `safe-off`, or conservative `dirty` status, then
    remove the durable journal, public transaction marker, and rollback files
11. report the original failure and any rollback failure separately

If the shell or process disappears, the root-owned safeguard still stops the
new TUN and returns the machine to direct networking. It does not attempt to
reconstruct user-scoped systemd or shell state; the normal rollback does that
when the process remains alive.

### 8.4 `proxy tun off`

```
1. acquire the root transaction lock and recover any interrupted transaction
   owned by the current UID
2. verify that the manifest belongs to the current UID
3. disable and stop sing-box-manager-tun.service
4. wait for inactivity and verify interface/route/rule cleanup
5. write the root-maintained safe-off status
6. leave managed files and the manifest in place
7. report that mixed mode was not restored and print the proxy on hint
```

`off` is a toggle, not an uninstall. A failure to stop or clean up is an error,
not a warning followed by deletion.

## 9. Guards and diagnostics

### 9.1 Existing commands unless TUN is safely off

| Command                                  | Behaviour                                                             |
| ---------------------------------------- | --------------------------------------------------------------------- |
| `proxy on`                               | Refuse and print `proxy tun off` guidance                             |
| `proxy service on`                       | Refuse; it would start the mixed instance under TUN                   |
| `proxy shell\|env on`                    | Refuse, including `--force`                                           |
| `proxy git on`                           | Refuse for global and local scope, including `--force`                |
| `proxy desktop on`                       | Refuse, including `--force`                                           |
| `proxy docker on`                        | Refuse, including `--force`                                           |
| `proxy route <china\|gfw\|ai\|global>`   | Refuse; route variants apply only to the mixed instance               |
| `proxy port [<PORT>]`                    | Show or stage the mixed port; leave the running TUN unchanged         |
| `proxy off` and component `off` commands | Clear only the surfaces they own, then report that TUN remains active |
| `proxy protocol <p>`                     | Run the TUN switching transaction from Section 10                     |
| `proxy check service`                    | Report the singleton TUN unit separately from mixed user units        |

The guard is centralized and applies the safely-off predicate from Section 7,
not merely `is-active` for the selected protocol. `--force` does not override
it. Every mixed-mode command checks the public transaction marker, and generated
mixed units run the same guard in `ExecStartPre` so a delayed or direct
`systemctl --user start` cannot race a TUN transaction. The TUN transaction
rechecks and stops all manager-owned mixed units immediately before starting the
system service.

### 9.2 Proxy ownership

TUN activation does not indiscriminately clear proxy settings. Each surface gets
an ours/other/off classifier like the existing system-proxy classifier:

- shell variables are ours only when their values match this client's endpoint
- Git values are compared exactly and all values are preserved for rollback
- desktop mode and hosts are cleared only when they point at this client
- Docker client JSON and daemon drop-in are changed only when their values and
  managed-file marker identify this client

Foreign corporate or manually configured proxy settings are left unchanged and
reported. Successful `proxy tun on` intentionally does not retain a rollback
snapshot after commit; only settings owned by this client were removed.

Process-local state has an explicit limitation: one shell cannot rewrite proxy
variables inherited by other already-running shells or processes, and the
manager cannot enumerate repository-local Git settings in arbitrary working
trees. Activation clears the invoking shell and known current scopes, warns that
older processes may still reference the stopped mixed inbound, and recommends a
new shell or an explicit component `off` command in an affected repository. It
does not crawl the user's home directory or inspect unrelated process
environments. `proxy check tun` reports this limitation.

### 9.3 `proxy check tun`

The report uses `_proxy_report_section` and `_proxy_report_row` and includes:

- supported or unsupported runtime kind
- systemd system-manager availability
- `/dev/net/tun` presence and privileged open probe result
- fixed interface/address conflict status
- unit installed, active, substate, enabled, MainPID, and restart count
- public status attestation and transaction-marker state
- manifest present, owner UID, protocol, generation, and content-hash status;
  root-only fields are reported only when non-interactive sudo access is already
  available, and diagnostics never prompt solely to display them
- source config path resolved through the XDG-aware helper
- provisioned config path
- whether the current user may mutate the installation
- warning that other process-local shell and repository-local Git settings
  cannot be enumerated
- nftables status only when the selected Linux profile uses `auto_redirect`

No diagnostic prints credentials or rendered config content.

## 10. Protocol switching

When TUN is safely off, `proxy protocol <p>` keeps the existing mixed-mode
behavior. When TUN is active, enabled, transitioning, or recovering an
interrupted transaction, it uses the same staged transaction as `proxy tun on`:

1. verify the target TUN source config is installed for the current user
2. verify the current UID owns the active TUN
3. stage and validate the target binary/config before stopping the old TUN
4. preserve the old TUN's enabled state
5. arm the safeguard, stop the old TUN, publish the target, and start it
6. verify interface, routes, DNS, packet counters, and egress
7. save `selected-protocol` only after successful verification
8. on failure, restore and restart the exact previous TUN generation

Switching never starts a mixed user unit under an active TUN and never turns a
non-persistent TUN persistent implicitly.

## 11. Upgrade and uninstall

### 11.1 Portal reinstall

The current installer invokes `client-uninstall.sh --yes` before every install.
That cleanup must not remove or overwrite the privileged TUN installation.

The revised behavior is:

1. `client-install.sh` evaluates the TUN safely-off predicate before uninstalling
   anything
2. if TUN is active, enabled, transitioning, or dirty, installation refuses with
   a `proxy tun off` hint
3. installer-driven cleanup calls `client-uninstall.sh --preserve-tun`
4. inactive privileged files remain in place while user files are replaced
5. the next `proxy tun on` re-stages and re-syncs the new binary/config by hash

This explicit stop-before-upgrade rule avoids running stale privileged state and
avoids starting mixed mode beneath an active tunnel.

The installed shell's `proxy upgrade` command follows the same rule: it checks the
safely-off predicate and refuses with `proxy tun off` guidance before downloading or
running the hosted installer. After TUN is safely off, it preserves mixed-mode client
state across the reinstall; it deliberately does not reactivate the privileged TUN.

### 11.2 Manual uninstall

`proxy uninstall` runs the installed `client-uninstall.sh` with the same TUN
ownership and removal checks. It accepts `--preserve-tun` when the privileged
installation should remain in place.

Manual `client-uninstall.sh` removes privileged TUN state unless
`--preserve-tun` is passed. It uses lazy sudo and must:

1. acquire the root transaction lock and recover an interrupted transaction
2. verify that the manifest owner equals the current UID
3. disable and stop the unit and verify complete network cleanup
4. remove only the exact binary, optional Cronet library, config, optional rule
   sets, manifest, root-only control directory, public transaction marker,
   public status attestation, and unit recorded by this feature
5. run `systemctl daemon-reload` and `reset-failed`
6. remove the runtime state and dedicated account only when ownership is proven
   and no managed unit references them

It must not glob `/etc/systemd/system/sing-box-*.service`, remove all of
`/etc/sing-box`, or touch `/usr/local/bin/sing-box`. Existing legacy cleanup
must be narrowed so file existence alone is not treated as proof that a client
owns a server or third-party sing-box installation.

If stopping the unit fails, uninstall leaves the unit, executable, config, and
manifest intact and reports an incomplete uninstall. It never removes the
management path while a root-managed tunnel may still be running.

## 12. Linux TUN profile requirements

The Linux self-managed profile is an acceptance requirement, not just a new
filename. Each protocol profile must:

- use a fixed interface name such as `sbm-tun0`
- set `auto_route` and one tested loop-avoidance mechanism, initially
  `route.auto_detect_interface`
- route the selected server endpoint outside the tunnel
- reject startup when the TUN addresses conflict with an existing interface or
  route
- send public traffic through the protocol outbound and preserve private/LAN
  routes according to documented policy
- make DNS capture and resolver detours explicit
- have no first-start dependency on a mutable remote rule-set download
- use local packaged rule sets when regional direct-routing rules are retained
- configure a cache/state path under `/var/lib/sing-box-manager-tun`
- state explicitly whether `auto_redirect` is enabled

For the first implementation, omitting regional rule sets and proxying all
non-private traffic is preferable to a remote rule-set dependency that can make
startup fail. If `auto_redirect` is enabled, its nftables requirements and
cleanup checks become mandatory; otherwise nftables is not a preflight
requirement.

## 13. Decisions

**One service, one owner.** TUN is a machine-wide singleton owned by the first
UID that provisions it. Automatic takeover is not supported.

**Trusted provisioner, unprivileged runtime.** The sudo-authenticated user is
trusted to select the payload, but sing-box runs as `sing-box-tun` with only the
required capability.

**Boot persistence defaults to off.** A new `proxy tun on` starts without
enabling. `--persist` enables only after verification. Re-sync and protocol
switch preserve an existing enabled state.

**No automatic mixed restore on `proxy tun off`.** The user explicitly runs
`proxy on` to return to mixed mode.

**Systemd state plus provenance metadata.** systemd determines runtime state;
the manifest determines owner, protocol, generation, and file identity.

**Content hashes over mtimes.** Provisioning stages and validates every time,
and diagnostics compare hashes instead of timestamps.

**Safe refusal over partial platform support.** Initial support is native local
Linux/systemd only.

## 14. Testing

### 14.1 Release and installer tests

- Linux packages contain mixed and Linux TUN configs for every allowed protocol
- Android continues to receive only the existing mobile TUN config
- TUN rendering does not inject `set_system_proxy`
- user units use exact `-c config.json`, not `-C <directory>`
- installed config directories are `0700` and configs are `0600` under a hostile
  umask
- real `sing-box check` validates each generated profile independently
- installer refuses while TUN is active and internal cleanup preserves inactive
  TUN files

### 14.2 Command and transaction tests

Extend `tests/test_setup_proxy_command.py` with PATH-shimmed `sudo`, `systemctl`,
and install primitives for:

- first provisioning creates exact paths, owners, modes, account, unit, and
  manifest
- Naive provisioning, rollback, and uninstall include `libcronet.so`; other
  protocols record it as absent
- re-sync uses content hashes and restarts an active service
- unchanged, changed, equal-mtime, and older-mtime source files
- owner mismatch and foreign/modified managed-file refusal
- source and destination symlink attacks
- lock contention between on, off, switch, and uninstall
- active/disabled, active/enabled, inactive/enabled, failed, activating,
  deactivating, masked, and restart-looping unit states
- publication and rollback run `daemon-reload` and clear start-limit state before
  starting the staged or restored generation
- the safely-off predicate and mixed-unit `ExecStartPre` transaction guard
- root status is marked dirty before mutation and safe-off only after privileged
  interface/route/rule cleanup succeeds
- snapshot, stop, and restoration of every manager-owned mixed service
- refusal when an unexpected legacy or foreign proxy service remains active
- rollback from every mutation point, including signals and failed cleanup
- failure after each destination-filesystem rename and before/after the manifest
  commit marker
- durable interrupted-transaction recovery after process loss and simulated
  reboot
- first-run account creation and removal are journaled around capability-probe
  success and failure
- safeguard creation, cancellation, and expiry
- disposable TUN creation/removal under the intended UID, capability, and device
  policy rather than a simple `/dev/net/tun` open check
- protocol switch success and restoration of the previous generation on failure
- persistence is enabled only after verification and preserved during re-sync
- every mixed/component enable command refuses under TUN, including `--force`
- exact preservation of foreign shell, Git, desktop, and Docker settings
- `proxy tun off` leaves provisioned files in place
- uninstall aborts deletion when stop or route cleanup fails

### 14.3 Compatibility and integration tests

- `tests/test_setup_completion.py` covers Linux-only command completion
- direct zh_CN tests cover help, preflight, ownership, rollback, and uninstall
  messages
- `tests/test_shell_compat.py`, `pixi run lint-shell`, and
  `pixi run format-shell` pass
- `systemd-analyze verify` validates the generated unit
- disposable native-Linux VMs run real systemd, sing-box, `/dev/net/tun`, DNS,
  routes, nftables when applicable, Docker networking, interface changes, and
  suspend/resume
- integration tests deliberately terminate the controlling process and verify
  that the safeguard returns the machine to direct networking

## 15. Out of scope

- **Remote activation:** SSH and other detected remote shells are refused in the
  first implementation. Supporting them requires preserving the control peer's
  route and proving watchdog recovery.
- **WSL2 and containers:** network namespaces, capabilities, and system-manager
  behavior require separate designs and integration tests.
- **macOS:** needs a LaunchDaemon in `/Library/LaunchDaemons` rather than the
  current user LaunchAgent.
- **Windows:** needs the equivalent in `scripts/sing-box-proxy.psm1` as a SYSTEM
  service and must remain Windows PowerShell 5.1 compatible.
- **Mobile:** iOS and Android GUI clients continue to manage their own tunnels;
  the shell `proxy` command does not exist there.

## 16. Implementation notes

Where the shipped code differs from the design above, or resolves something the
design left open.

**`SBM_TUN_ROOT` prefixes every managed path.** Set it and the whole layout moves
under a scratch directory. That is how the test suite drives a full transaction
without a real `/etc`, and it is what the release and installer smoke runs use.
It grants nothing: these paths are only ever written through the caller's own
sudo, and Section 2 already treats the provisioning user as sudo-capable.

**The lock is a root-owned directory, not an `flock`.** It has to survive across
many separate `sudo` invocations, so an flock on one shell's file descriptor
would not hold. `mkdir` is atomic, and the owner stamp records the boot id plus
the holder's process start time, so a lock left behind by a killed shell is
reclaimed rather than blocking every later run, while a live holder is not.

**Staging happens beside each destination, not in the control directory.**
`tun_staged_path` produces `<dir>/.<name>.staged` so the publishing rename is
always same-filesystem, which is the point of Section 8.2. The control directory
holds only the journal and the previous generation.

**The safeguard is armed after staging, immediately before quiesce.** Section 8.2
lists it in step 2. Arming it before the payload exists would only schedule a
stop for a unit that is not there; arming it immediately before the first network
change covers exactly the window that matters. Its timeout is 300 seconds, or
`SBM_TUN_SAFEGUARD_SECONDS`.

**No packaged rule sets, so no `rule.<name>.sha256` manifest lines.** Section 12
prefers omitting regional rule sets over a remote dependency that can make first
start fail, and the shipped profiles take that option. `/etc/sing-box-manager/tun/rules/`
is still created and still removed on uninstall, so adding one later needs no
layout change.

**`auto_redirect` is `false` in every shipped profile, stated in the config
itself.** nftables is therefore not a preflight requirement, and `proxy check tun`
reports the setting rather than an nftables status.

For sing-box 1.14.0, Linux TUN profiles explicitly set `dns_mode: disabled` to keep
the existing route-based DNS hijack behavior. This avoids enabling the new native
TUN DNS handling. Desktop rule snapshots and persistent DNS answers belong to the
mixed profiles; the Linux TUN profiles retain their separate runtime state.

**Packet-counter verification degrades to a warning, not a pass.** Verification
reads `/sys/class/net/sbm-tun0/statistics/tx_packets` before and after the egress
probe and fails when it has not moved. When the counter cannot be read at all,
the command warns that throughput was not verified instead of claiming it was.

**Mixed user units carry the transaction guard in `ExecStartPre`.** The generated
unit refuses to start while the public transaction marker exists, so a delayed or
manual `systemctl --user start` cannot race a TUN transaction — the same marker
the shell guard checks.

**Legacy cleanup now needs proof.** `client-uninstall.sh` no longer globs
`/etc/systemd/system/sing-box-*.service`; it looks for the three exact unit names
this client has ever written, and only claims `/usr/local/bin/sing-box`,
`/usr/local/etc/sing-box`, and `/var/lib/sing-box` when one of those units is
actually present. File existence alone no longer makes a server or third-party
install look like ours.

**The sourced scripts avoid zsh-reserved names.** `setup.sh` and its libraries
are sourced into the user's interactive shell, which is frequently zsh, where
`status` is read-only and `path` is tied to `PATH`. A `local` of either aborts
the function or silently rewires the shell for the length of the call. zsh also
does not word-split an unquoted `$var`, so the mixed-service snapshot is read a
line at a time rather than iterated with `for entry in $snapshot`.
`tests/test_shell_compat.py` bans the reserved names statically, and
`tests/test_setup_tun_command.py` runs the whole transaction under zsh.

**Admin tools are resolved through sbin explicitly.** `ip`, `useradd`, and
`userdel` live in sbin, which many interactive `PATH`s omit for a non-root user.
`tun_resolve_admin_command` falls back to `/usr/local/sbin`, `/usr/sbin`, and
`/sbin` (overridable with `SBM_TUN_SBIN_DIRS`). This matters most for cleanup
verification: an unresolvable `ip` must fail closed rather than report that no
routing artifacts remain.

**The runtime account is created before the managed directories.**
`/etc/sing-box-manager/tun` is group-owned by `sing-box-tun`, so `install -g`
fails outright until that group exists. The test shims validate `-o`/`-g` the
way the real tool does rather than dropping them, because dropping them is what
let this ordering reach a real run.

**A rolled-back transaction leaves no installation behind.** It writes the
`safe-off` attestation and removes the journal, the rollback copies, and the now
empty control directory. `client-uninstall.sh` treats a lone `safe-off` marker
as nothing to remove, so a transaction that published nothing cannot make a
later uninstall escalate to sudo.

**Integration testing is still outstanding.** Section 14.3 asks for disposable
native-Linux VMs running real systemd, sing-box, `/dev/net/tun`, DNS, routes,
Docker networking, interface changes, and suspend/resume, plus a deliberate kill
of the controlling process to exercise the safeguard. The unit suite covers the
transaction through PATH-shimmed privileged tools and a redirected root; it does
not exercise the kernel, and `systemd-analyze verify` is not yet wired into CI.

All TUN profiles include the exact deployment host and configured
`deployment.direct_domain_suffixes` as direct exceptions. Suffixes cover their
apex and subdomains; IP-valued hosts use an exact IP exception. Matching DNS
queries and direct outbound resolution use Aliyun directly. Mobile exceptions
precede FakeIP. DNS interception and transport restrictions remain in effect.
The four desktop route strategies do not change the TUN policy.
