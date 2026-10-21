# Official sing-box mobile apps

Checked on 2026-10-21.

Use the latest official stable iOS or Android app. Each account receives one
remote profile with all its enabled protocols. The app selects the protocol and
routing strategy independently. Older app versions are not a compatibility target.
Unified-profile device acceptance is not complete on either platform.

## Import a remote profile

1. Install the official [iOS](https://sing-box.sagernet.org/clients/apple/)
   or [Android](https://sing-box.sagernet.org/clients/android/) app.
2. Open the subscription page supplied by an operator.
3. Select **在 sing-box 中打开** on the phone. Confirm the remote-profile import.
4. Alternatively, scan the page's QR code from another screen. Use the iOS
   system camera or the Android app's scanner.
5. If the browser cannot open the app, copy the remote-profile URL into the app.
6. Download or refresh the profile before starting the connection. Review the
   app's automatic refresh setting.
7. Start the profile. Select an enabled protocol from the `proxy` outbound group.
8. Select a routing strategy. The default is `china-direct`.

Steps 3–5 are alternative import methods. Clipboard failure selects the URL for
manual copying. The URL remains visible without JavaScript.

**下载 JSON** saves a static configuration. It does not receive subscription
updates. Prefer the remote profile. App refresh settings are independent of the
`sbc` scheduler on Linux, macOS and Windows.

## Strategies and protocols

| Strategy       | Behavior                                                                                |
| -------------- | --------------------------------------------------------------------------------------- |
| `direct`       | Direct all allowed traffic, including AI. Use platform local DNS.                       |
| `ai-only`      | Proxy community-listed AI destinations. Direct the rest.                                |
| `gfw-proxy`    | Proxy GFWList and AI destinations. Direct the rest.                                     |
| `china-direct` | Direct recognized China destinations. Proxy the rest. AI and GFW matches take priority. |
| `global`       | Proxy allowed traffic except LAN and configured direct exceptions.                      |

Mobile and PC configs share routing rules, rule sources and DNS policy. Mobile
shows only these five names, without PC aliases. All modes retain AdGuard,
DNS interception, configured direct exceptions and the transport restrictions.
AI matches precede AdGuard. In `direct`, allowed AI traffic and its DNS go direct.
This mode can expose the local network's public IP to AI services.

DNS returns real answers, not FakeIP addresses. The local resolver uses the
mobile platform interface, not Linux's `127.0.0.53`. The app manages rule storage.
Seven remote rule sets update daily through the selected proxy protocol. Mobile
has no bundled snapshots, so first-start rule downloads need a working proxy.
`direct` does not disable rule downloads through the proxy.

The `proxy` selector contains only enabled account protocols. Its default uses
`default_protocol`, or the account's first enabled protocol. Changing the selector
needs no core restart and interrupts existing inbound connections. A route change
clears the core DNS cache but does not promise to close existing connections.
The core cache retains mode and protocol choices across restarts. Native app
refresh and persistence still need device acceptance.

The Clash-compatible API supports `GET /proxies/proxy` and
`PUT /proxies/proxy` with `{"name":"hysteria2"}`. Route selection uses
`PATCH /configs` with `{"mode":"china-direct"}`. Mobile apps can use native
libbox controls instead. The profile opens no HTTP controller listener.

### Replace a per-protocol profile

Per-protocol mobile profiles are not supported. Requests with `protocol=`,
including an empty value, return `400`. Open the subscription page and import
its unified profile. Disable the old profile. Keep it until the new profile
passes connection checks. This change does not rotate protocol credentials.

The core accepts `Direct` and `Global` case-insensitively. A cached `Rule`
selection is not offered and falls back to `china-direct`. No compatibility
entry appears in the new mode list.

## Credentials and revocation

- URLs, import links and QR codes are credentials. Do not share screenshots or
  upload them to online QR readers.
- Segno generates QR images locally. No QR provider or third-party script
  receives the subscription.
- Each QR encodes an import URI, not a full configuration or browser-page URL.
- Page and configuration responses retain `private, no-store` and `no-referrer`.
- Account disablement, portal disablement and subscription counter changes reject
  the affected URLs. A copied QR grants no account-management permission.
- Downloaded profiles contain protocol credentials. Link rotation alone does not
  revoke protocol access from a downloaded profile.

## Import URI contract

The official libbox format stores the profile name in the fragment:

```text
sing-box://import-remote-profile?url=<percent-encoded-remote-url>#<percent-encoded-name>
```

The unified remote URL has no protocol parameter:

```text
https://vpn.example.com/sub/<token>?format=mobile
```

The name is `<username> - sing-box`. The link has no trailing slash before the
query. `/sub/<token>/` is the browser page, not the configuration. The JSON
response retains ETag and usage headers. Without `format`, the endpoint returns
the same unified mobile profile.

Source references:

| Component          | Checked source                                                                                                                                                                                                                    |
| ------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Core               | [sing-box v1.14.3, `remote_profile.go`](https://github.com/SagerNet/sing-box/blob/v1.14.3/experimental/libbox/remote_profile.go)                                                                                                  |
| Apple URL handler  | [`0b84ea47`, `MainView.swift`](https://github.com/SagerNet/sing-box-for-apple/blob/0b84ea472fbc9e1a7e11992b1f2daadbe2d30556/SFI/MainView.swift)                                                                                   |
| Android QR handler | [`092aefd1`, `ProfileImportHandler.kt`](https://github.com/SagerNet/sing-box-for-android/blob/092aefd125420a9862a461093b6ce6be65b9afa6/app/src/main/java/io/nekohasekai/sfa/compose/screen/configuration/ProfileImportHandler.kt) |

These references establish the URI format, not device behavior. App and embedded
core versions are separate values.

## Acceptance boundary

Fixture tests decode QR images with ZXing and exercise clipboard fallback.
PostgreSQL tests check one import entry, enabled protocols, credentials, ETag,
usage headers, host validation and revocation. Core tests check all five modes,
selector paths, DNS changes, proxy failures and restart persistence with local
stand-ins. They use a mixed inlet instead of native TUN capture.

| Platform | Unified import | Five modes | Protocol switching | Refresh and restart |
| -------- | -------------- | ---------- | ------------------ | ------------------- |
| iOS      | Not tested     | Not tested | Not tested         | Not tested          |
| Android  | Not tested     | Not tested | Not tested         | Not tested          |

For device acceptance, record the device, OS, app version, embedded core version,
network, tested commit and date. Mark unavailable values as unknown.

1. Use an authorized private test subscription containing the code under review.
2. Check button import, QR import and manual URL import separately.
3. Check connectivity with each enabled protocol and all five strategies.
4. Check actual TUN domain routing with real DNS answers, including AI and GFW
   priority over China classification. Check IPv4 and IPv6 where available.
5. Check that `direct` sends AI traffic and DNS directly without needing the proxy
   for application traffic after rule files are available.
6. Check protocol switching, DNS changes after mode switching, refresh and restart.
7. Check that disabled protocols disappear after refresh and cannot remain selected.

An exit IP proves only the tested route. Do not infer whole-device capture from it.
Keep subscriptions, QR images, full configs and private screenshots outside public
Git. Use a private preview or request deployment authorization. These checks do
not authorize a release or deploy.
