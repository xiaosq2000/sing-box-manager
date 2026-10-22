# Snell v6

Checked on 2026-10-21.

## Scope

| Item        | Behavior                                                                 |
| ----------- | ------------------------------------------------------------------------ |
| Core        | sing-box 1.14.3, with native Snell inbound and outbound support          |
| Protocol    | v6 only, `mode: default`, connection reuse off                           |
| Server      | One additional TCP listener in `sing-box.service`                        |
| Clients     | Linux, macOS and Windows `sbc`, Linux TUN, official iOS and Android apps |
| Credentials | One shared infrastructure PSK and one independent userkey per account    |
| Defaults    | Trojan stays the default. Existing client selections stay unchanged      |
| Routing     | Existing five strategies and transport restrictions stay unchanged       |

Snell carries TCP and UDP through its TCP listener. Do not open a Snell UDP
port. Linux TUN still rejects non-private UDP other than intercepted DNS and
allowed NTP. Snell does not use the deployment TLS certificate. This feature
adds no Surge format, v4/v5 listener, QUIC transport or independent server.

Sources: [pinned inbound documentation](https://github.com/SagerNet/sing-box/blob/v1.14.3/docs/configuration/inbound/snell.md),
[pinned outbound documentation](https://github.com/SagerNet/sing-box/blob/v1.14.3/docs/configuration/outbound/snell.md),
and [inbound authentication](https://github.com/SagerNet/sing-box/blob/v1.14.3/protocol/snell/inbound.go).
Snell requires core 1.14.0 or later. A mobile app version and its embedded core
version are separate values. Older apps are not compatibility targets.

## Infrastructure and account settings

Keep the real PSK in the operator-owned encrypted inventory. Configure both
fields together. Choose an unused TCP port that differs from the other
configured TCP services and the portal's ports 80 and 443. The port below is an example, not a default.

```yaml
deployment:
  snell_port: 10443
snell:
  psk: replace-with-a-random-private-psk
  users: []
```

The v6 PSK must contain 12 to 255 UTF-8 bytes. Userkeys must contain 1 to 255
UTF-8 bytes. Neither permits outer whitespace or control characters.
New accounts receive independent random protocol secrets, including a Snell
userkey. Without Snell infrastructure, that credential remains dormant and
subscriptions omit Snell. Old inventories can omit both Snell fields.

PostgreSQL owns accounts in authoritative mode. Do not import inventory again.
For initial inventory import only, `snell.users` uses the existing protocol
record shape: `username`, `password`, and `enabled`. The `password` value becomes
the wire `userkey`, not the shared PSK. Userkeys must be unique.

An enabled account and enabled Snell credential are both necessary for access.
Portal disablement blocks subscriptions, not credentials already downloaded.
No enabled Snell users means no Snell listener. An empty upstream `users` list
would enable PSK-only authentication, so the renderer must never publish it.
Counters use `snell:<username>` internally. Existing collectors map this label
to the account and retain a separate Snell protocol bucket.

## Authorized rollout

Code verification does not authorize a production release or deployment.
For an authorized rollout:

1. Back up PostgreSQL, runtime settings, the active node configuration, SQLite
   usage history, and matching code and environment. Retain the backups.
2. Add the shared PSK and port with SOPS. Keep Trojan as the default.
3. Build and deploy with that inventory using the normal deployment procedure.
   Migration `0005_snell` adds the credential choice and a unique userkey
   constraint. It does not create credentials or rotate existing values.
4. Admit the selected TCP port in the VPS and provider firewalls. Bootstrap
   configures ufw. An ordinary deployment does not change firewall rules.
5. Preview the account operation on the deployed host:

   ```sh
   sudo -u sbm sbm control-plane backfill-snell --dry-run
   ```

6. Review the counts, then add missing credentials:

   ```sh
   sudo -u sbm sbm control-plane backfill-snell
   ```

   This includes database-only and disabled accounts. Master disablement remains
   effective. Existing Snell secrets and switches stay unchanged. Repeated runs
   create nothing. Output contains counts, not usernames or keys.

7. Check node sync. The operation requests sync after its transaction commits.
   A request is not proof that the node applied the change. If necessary, run
   `sudo sbm control-plane sync`. A failed trigger retains the saved keys.
8. Refresh subscriptions. Use `sbc protocol snell` or the mobile selector.
   Check each original protocol too. Measure each proxy interruption against
   the deployment limit of less than 60 seconds.

Workstation commands support `control-plane backfill-snell --remote vpn-host`
and the same `--dry-run` option. This preview contacts the host but does not
write credentials or request sync. `--dry-run` is rejected for other
control-plane actions.

To revoke one user, disable its credential or account and confirm node sync.
Removing subscription access or rotating a link alone does not revoke a
previously downloaded proxy credential. Every Snell user receives the shared
PSK, but the server also requires that user's active userkey.

For a rollback, restore matching code, infrastructure settings and node config.
Stop advertising Snell before returning clients to an older configuration.
Retain PostgreSQL credentials, usage history and recovery files. Do not rotate
other protocol passwords or subscription links as rollback steps.

## Verification and remaining acceptance

| Check             | Coverage                                                                                                        |
| ----------------- | --------------------------------------------------------------------------------------------------------------- |
| Configuration     | All five strategies on desktop, mobile and Linux TUN, checked with real 1.14.3                                  |
| Authentication    | Two real users, wrong PSK, wrong or missing userkey, disabled user                                              |
| Transport         | Real Snell TCP and UDP-over-TCP against loopback origins                                                        |
| Routing           | Desktop and mobile core policy in all five modes, including proxy failure without direct fallback               |
| Server egress     | Real Snell private-address and DNS-answer rejection, including UDP association checks                           |
| Selection         | Real Snell selector path, restart persistence and removal of an unavailable selection                           |
| Accounts          | PostgreSQL backfill preview, repeat execution, dormant credentials, revocation, ETag and subscription isolation |
| Accounting        | One real four-protocol node, two Snell users, per-user counters and connection stream                           |
| Native desktop CI | Real Snell loopback check in the Linux, macOS and Windows lifecycle suites                                      |

Fixture checks change no operator services or settings. Mobile policy tests use
a mixed inlet, not device TUN capture. Linux TUN configuration checks do not
establish native Snell capture. Cross-compilation does not establish native
Windows or macOS behavior.

iOS unified-profile device acceptance is operator-reported complete. Device,
OS, app and embedded core versions are not recorded. That report is not Snell
acceptance. Snell-specific iOS connection, selection, refresh and restart checks
remain pending. Android device acceptance is optional for this feature's code
completion and remains unverified. Production rollout is not part of local
verification.
