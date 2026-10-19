# Official sing-box mobile apps

Checked on 2026-10-19.

The subscription page provides remote-profile import buttons, QR codes, copyable
URLs, and JSON downloads for enabled protocols. Use the latest official stable
iOS or Android app. Older app versions are not a compatibility target.

## Import a remote profile

1. Install the official [iOS](https://sing-box.sagernet.org/clients/apple/)
   or [Android](https://sing-box.sagernet.org/clients/android/) app.
2. Open the subscription page supplied by an operator.
3. Choose one protocol.
4. On the same phone, select **在 sing-box 中打开**. Confirm the remote-profile
   import in the app.
5. From another screen, scan the protocol's QR code. Use the iOS system camera
   or the Android app's scanner.
6. If the browser does not open the app, copy the protocol URL. Add a remote
   profile in the app and paste that URL.
7. Download or refresh the profile before starting the connection. Review the
   app's automatic refresh setting.

Steps 4–6 are alternative import methods. The browser's copy button needs clipboard
permission. If copying fails, the page selects the URL for manual copying.
Each copy button displays feedback beside its own URL. The URL remains visible
when JavaScript is unavailable.

**下载 JSON** saves the current configuration as a local file. This file does not
receive subscription updates. Remote profiles are preferred.

Mobile refresh settings belong to the mobile app. They are independent of the
`sbc` scheduler on Linux, macOS, and Windows.

## iPhone acceptance procedure

Use an authorized private subscription. Install the latest stable app through the
[official Apple client page](https://sing-box.sagernet.org/clients/apple/).
Do not use a TestFlight build for stable-app acceptance.

Before testing, record the iPhone model, iOS version, app version, and embedded core
version. App and core versions are different values. Record an unavailable value
as unknown, not as the repository's core version.

1. Disable another VPN temporarily. Keep its profile for recovery.
2. Open the test subscription page in Safari.
3. Start with Trojan if the account provides it.
4. Select **在 sing-box 中打开**. Confirm import and check the remote URL and protocol.
5. Start the profile. Allow the iOS VPN configuration prompt.
6. Open a known reachable HTTPS page. Check the app's connection status and traffic.
7. Open an IP-check page through a proxy route. Confirm the expected proxy exit.
8. Stop the profile. Refresh it in the app. Record the refresh result or timestamp.
9. Start it again and repeat the connection check.
10. Test a second import using the iPhone Camera and the QR on another screen.
11. Test a third import by copying the protocol URL into a new remote profile.
12. Repeat refresh and connection checks for each import method.

Use separate names for the three test profiles. Only one profile needs to run at
any time. Test other enabled protocols separately. Record protocol-specific
failures rather than attributing every connection failure to import.

For the manual method, use `/sub/<token>?format=mobile&protocol=trojan`.
Do not use the browser page `/sub/<token>/` or a downloaded static JSON file.
Check that the copy result appears beside the selected URL on the phone.
An IP result proves the selected test route, not that every app uses the tunnel.

The test page must contain the code under review. If the server does not have
these changes, manual remote-URL tests can check the existing configuration
endpoint, but cannot accept the new import button, QR, or clipboard behavior.
Use an authorized private preview or request separate deployment approval.
Do not expose credentials through a public preview or third-party tunnel.

Record each result as pass, fail, or not tested. Include the tested commit, date,
network type, protocol, import method, and a redacted error for failures.
Do not include tokens, QR images, full profiles, or private screenshots in the
public result. An unavailable Android device leaves Android acceptance pending.
iOS results alone do not complete the two-platform acceptance gate.

## Credentials and revocation

- URLs, import links, and QR codes are credentials. Do not share screenshots or
  upload them to online QR readers.
- The service generates PNG QR images locally with Segno. It does not contact a
  QR provider. No third-party script or image service receives the subscription.
- Each QR encodes an import URI, not a full configuration or browser-page URL.
- The page and configuration responses retain `private, no-store` and `no-referrer`.
- Account disablement, portal disablement, and subscription counter changes reject
  the affected URLs. A copied QR grants no account-management permission.
- A downloaded profile contains protocol credentials. Link rotation alone does
  not revoke an already downloaded profile's protocol access.

## Import URI contract

The official libbox format stores the profile name in the fragment:

```text
sing-box://import-remote-profile?url=<percent-encoded-remote-url>#<percent-encoded-name>
```

The remote URL uses the existing authenticated configuration endpoint:

```text
https://vpn.example.com/sub/<token>?format=mobile&protocol=trojan
```

The link has no trailing slash before the query. `/sub/<token>/` is the browser
page, not the configuration. The protocol query retains the user's selection.
The API's existing ETag and usage headers remain available.

Source references:

| Component                 | Checked source                                                                                                                                                                                                                    |
| ------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Core stable release       | [sing-box v1.14.3, `remote_profile.go`](https://github.com/SagerNet/sing-box/blob/v1.14.3/experimental/libbox/remote_profile.go)                                                                                                  |
| Apple stable URL handler  | [`0b84ea47`, `SFI/MainView.swift`](https://github.com/SagerNet/sing-box-for-apple/blob/0b84ea472fbc9e1a7e11992b1f2daadbe2d30556/SFI/MainView.swift)                                                                               |
| Android stable QR handler | [`092aefd1`, `ProfileImportHandler.kt`](https://github.com/SagerNet/sing-box-for-android/blob/092aefd125420a9862a461093b6ce6be65b9afa6/app/src/main/java/io/nekohasekai/sfa/compose/screen/configuration/ProfileImportHandler.kt) |

Application version numbers and core version numbers are separate. These source
references do not establish which build an app store supplies to a device.

## Acceptance boundary

Fixture checks decode generated PNG images with ZXing. They verify the URI,
profile name, selected protocol, authenticated JSON response, ETag, and revocation.
Clipboard tests cover success, denied permission, and an unavailable clipboard API.

**Physical-device acceptance is pending.** Source inspection and QR decoding do
not prove that a specific installed app imports and runs the profile.
Before claiming device acceptance, record both platform rows:

| Platform | App version | Embedded core | Import button | QR scan | Manual URL | Refresh and connection |
| -------- | ----------- | ------------- | ------------- | ------- | ---------- | ---------------------- |
| iOS      | Pending     | Pending       | Pending       | Pending | Pending    | Pending                |
| Android  | Pending     | Pending       | Pending       | Pending | Pending    | Pending                |

Use a private test account or authorized subscription. Keep its links, QR images,
configuration, and screenshots outside public Git. Do not deploy or publish a
release merely to complete these checks without authorization.
