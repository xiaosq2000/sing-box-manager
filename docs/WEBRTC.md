# WebRTC protection in sbc

Checked on 2026-10-06.

These commands have the same meaning on Linux, macOS and Windows.
`on` enables leak protection. It does not enable or disable the WebRTC API.

| Command                      | Behavior                                                               |
| ---------------------------- | ---------------------------------------------------------------------- |
| `sbc webrtc on`              | Enable browser protection and automatic repair                         |
| `sbc webrtc off`             | Remove owned policies, reset Firefox WebRTC preferences, disable setup |
| `sbc desktop on`             | Enable the desktop proxy and ensure protection, unless opted out       |
| `sbc on`                     | Repair protection when the desktop proxy follows sbc, unless opted out |
| `sbc off`, `sbc desktop off` | Disable the proxy and retain the WebRTC choice                         |
| `sbc uninstall`              | Perform the same browser cleanup before removing the client            |

Run commands from your ordinary user account. Linux and macOS policy changes can
request `sudo` approval. Windows can request UAC approval. Firefox profile edits
run as the original user. Close Firefox before cleanup; sbc refuses to rewrite a
profile that Firefox has locked. If cleanup reports an active profile, close
Firefox and retry the command. No `about:config` edits are needed for the four
supported preferences, including leftovers from older installations. Restart
other open browsers after changing protection.

`webrtc on|off` works without a configured subscription, a running proxy, a GNOME
session, or a default network route. A failed cleanup leaves the client available
for retry. A failed `webrtc off` still disables automatic setup. Only a successful
`webrtc on` clears that choice.

## Browsers and scope

| Browser                 | Configuration                                        |
| ----------------------- | ---------------------------------------------------- |
| Chrome, Chromium, Brave | `WebRtcIPHandling=disable_non_proxied_udp`           |
| Edge                    | `WebRtcLocalhostIpHandling=disable_non_proxied_udp`  |
| Firefox                 | Managed `user.js` preferences in discovered profiles |
| Safari                  | Not managed by this implementation                   |

The Chromium policies restrict direct UDP. They do not route every application
through the proxy. Protection stays configured while the proxy is off and can
restrict calls. Extensions, administrator policies and browser packaging can affect
which settings the browser applies. Check the browser's policy page after restart.

Linux uses mandatory JSON policies in the installed browser's system policy
directory. These affect every account on the machine. Snap and Flatpak Chromium
packages can use different policy locations and are not covered by this setup.
Firefox discovery includes traditional, Snap and Flatpak profile locations.

macOS uses mandatory plists under `/Library/Managed Preferences/`. These also affect
all accounts. Ordinary user `defaults` entries are recommended policies and are
insufficient for policies that require mandatory enforcement. Existing user entries
are preserved. After a managed policy change, sbc restarts the macOS preference
service to clear cached policies. Unchanged automatic checks do not restart it.
A pending `webrtc-mode.reload` marker permits retry after a failed cache refresh.
Restart open browsers after the command completes. See the [Chromium policy guide](https://www.chromium.org/administrators/mac-quick-start/).

Edge requires a browser restart. Microsoft documents that its policy does not apply
to profiles signed in with a personal Microsoft account. See the
[Edge policy reference](https://learn.microsoft.com/en-us/deployedge/microsoft-edge-policies/webrtclocalhostiphandling).
Windows details are in the [Windows guide](README_WIN.md#system-and-desktop-proxy).

## Ownership and saved choice

Matching browser policies that already exist remain unowned. Conflicting Chromium
policies cause an error. Policy cleanup removes only recorded values that still
match; policies changed by another tool remain in place. Firefox profile cleanup
has a broader reset scope, described below. `webrtc off` and uninstall report only
actual remaining settings, not every discovered Firefox profile.
Preserved settings from another source can keep protection active after this
account opts out.

Linux cleanup also removes a legacy `webrtc.json` when its entire contents match
exactly what older sbc versions wrote:

<!-- prettier-ignore -->
```json
{"WebRtcIPHandling": "disable_non_proxied_udp"}
```

The file must include the original trailing newline. The elevated helper rechecks
its contents before deletion. Other filenames, changed contents, additional keys
and symbolic links are not removed. Older versions stored no ownership record for
this file: an independently created file with identical bytes at the same location
is indistinguishable and is also removed.

For Firefox, plain `sbc webrtc off` resets these four preferences to browser
defaults in both `prefs.js` and `user.js` across discovered profiles:

- `media.peerconnection.ice.no_host`
- `media.peerconnection.ice.default_address_only`
- `media.peerconnection.ice.proxy_only`
- `media.peerconnection.ice.proxy_only_if_behind_proxy`

The reset removes their declarations regardless of value or origin. It also
removes the sbc marker. This intentionally resets pre-existing user choices for
these four preferences, including orphaned values left by an older cleanup.
Unrelated preferences, the WebRTC API setting and administrator-enforced policies
remain untouched. No extra reset flag is needed. Uninstall uses the same cleanup.

New setup no longer creates per-profile snapshots. Cleanup deletes an old
`.sbc-webrtc.json` after resetting the profile; it never restores values from that
file, even when its contents are corrupt. Cleanup writes `prefs.js` before
`user.js` and removes obsolete state last, so a failed operation can retry. Clean
profiles that need no changes are not rewritten or locked.

| Platform | Explicit opt-out                                                   | Policy ownership                                              |
| -------- | ------------------------------------------------------------------ | ------------------------------------------------------------- |
| Linux    | `$XDG_CONFIG_HOME/sbc/webrtc-mode`, or `~/.config/sbc/webrtc-mode` | `.sbc-webrtc-<uid>.owner` beside each `sbc-webrtc-<uid>.json` |
| macOS    | `~/Library/Application Support/sbc/webrtc-mode`                    | `/Library/Managed Preferences/.sbc-webrtc-<uid>.plist`        |
| Windows  | `%LOCALAPPDATA%\sbc\webrtc-mode`                                   | `HKCU\Software\Policies\sbc\WebRTC`                           |

Linux ownership files and macOS managed plists are written through `sudo`.
Ownership records remain available after an interrupted operation. Empty macOS
plists can remain after key cleanup. The shared lifecycle serializes policy and
profile changes, including concurrent commands from the same account.

## Manual WebRTC cleanup

Start with `sbc webrtc off`. Restart open browsers. Preserve policies required by
your administrator.

This section is only for policies outside sbc's automatic cleanup scope. The four
Firefox profile preferences and exact legacy Linux policy files are cleaned
automatically.
Linux policies that do not match the legacy file and macOS user-level defaults
remain untouched. Inspect these locations before removing them.
An Edge `WebRtcIPHandling` entry uses the wrong policy name. If you created that
entry, remove only that entry. The supported name is `WebRtcLocalhostIpHandling`.

For a user-level Edge entry on macOS:

```sh
defaults read com.microsoft.Edge WebRtcIPHandling
# Run only if this entry is yours:
defaults delete com.microsoft.Edge WebRtcIPHandling
```

The macOS anchor `com.xiaosq2000.sbc.webrtc` can remain from an installation that
used the port filter. This version neither creates nor claims that filter.
Inspect it with `sudo pfctl -a com.xiaosq2000.sbc.webrtc -sr`. If it contains only
the sbc rule for UDP ports 3478, 19302 and 5349, you can clear that anchor:

```sh
sudo pfctl -a com.xiaosq2000.sbc.webrtc -F rules
```

Do not disable PF or flush other anchors. Blocking selected ports does not prove
WebRTC protection because STUN servers can use other ports.

## Verification

Check `chrome://policy`, `edge://policy`, or `brave://policy` after restarting the
browser. The policy value must be `disable_non_proxied_udp`, with status **OK**.
Test direct STUN traffic before enabling protection, after enabling it, and after
cleanup. A browser exposing the WebRTC API does not by itself indicate a leak.

Fixtures cover ownership, legacy Linux cleanup, Firefox preference resets,
orphaned profiles, stale snapshots, active-profile locks, failures, opt-out,
repair and concurrent operations.
Disposable CI runners exercise real browser STUN traffic. Chrome is required on
Linux and macOS. Edge and Brave are also checked when installed. Windows checks
Chrome and Edge. Firefox currently has profile fixtures, without a native STUN
check. Safari protection is not verified.
