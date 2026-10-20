# Chrome WebRTC protection in sbc

Checked on 2026-10-20.

sbc manages WebRTC leak protection for **Google Chrome on Linux**. It writes the
mandatory Chrome policy `WebRtcIPHandling=disable_non_proxied_udp` under
`/etc/opt/chrome/policies/managed`. Chrome Beta, Dev and Canary use the same
directory.

Chromium, Edge, Brave, Firefox, macOS and Windows are not managed. The previous
cross-platform implementation remains in the repository history.

| Command                      | Behavior                                                               |
| ---------------------------- | ---------------------------------------------------------------------- |
| `sbc privacy browser`        | Show the saved choice                                                  |
| `sbc privacy browser on`     | Write the Chrome policy and enable automatic repair. May request sudo  |
| `sbc privacy browser off`    | Remove the owned policy and disable automatic repair. May request sudo |
| `sbc desktop on`             | Enable the desktop proxy and ensure the policy unless opted out        |
| `sbc on`                     | Repair the policy when the desktop proxy follows sbc, unless opted out |
| `sbc off`, `sbc desktop off` | Disable the proxy and keep the Chrome choice                           |
| `sbc uninstall`              | Remove the owned Chrome policy before removing the client              |
| `sbc privacy`                | Summarize this choice and region matching                              |

`privacy browser on|off` works without a subscription, a running proxy, a GNOME
session, or a default route. `sbc privacy` does not change anything. A failed
cleanup leaves the client available for retry. A failed `privacy browser off`
still records the opt-out. Only a successful `privacy browser on` clears it.

## Ownership and saved choice

The policy file is `sbc-webrtc-<uid>.json` in the Chrome policy directory, with
an ownership record `.sbc-webrtc-<uid>.owner` beside it. Both are written through
`sudo`; the elevated helper rechecks the fixed directory, this account's UID and
the constant policy data before each change. Matching policies that already
exist remain unowned. A conflicting `WebRtcIPHandling` value causes an error.
Cleanup removes only the owned file.

The explicit opt-out is:

```text
$XDG_CONFIG_HOME/sbc/webrtc-mode
```

or `~/.config/sbc/webrtc-mode` when `XDG_CONFIG_HOME` is unset. The file contains
`off`. Its absence means automatic repair is enabled. Existing opt-outs survive
upgrades.

Off and uninstall also remove an old `webrtc.json` in the Chrome policy directory
when its entire contents match exactly what older sbc versions wrote:

<!-- prettier-ignore -->
```json
{"WebRtcIPHandling": "disable_non_proxied_udp"}
```

The file must include the original trailing newline. The elevated helper rechecks
its contents before deletion. Other filenames, changed contents, additional keys
and symbolic links are not removed.

Restart Chrome after a policy change and check `chrome://policy`. A fresh profile
is not required. Chrome packages from Snap or Flatpak can use a different policy
location and are not covered by this setup.

## Limits

- The policy restricts direct UDP. It does not disable the WebRTC API and does
  not route every application through the proxy.
- Administrator policies and extensions can override or bypass the setting.
  `chrome://policy` shows the effective value.
- `sbc desktop on` fails before enabling a new desktop proxy when Chrome already
  has a conflicting WebRTC policy.
- `sbc off` and `sbc desktop off` keep the Chrome policy. This can restrict
  WebRTC calls while the proxy is off.
- If Google Chrome is not installed, `privacy browser on` records the choice but
  writes no policy. Install Chrome and run it again.
- Chrome's managed policy directory is system-wide, so the policy affects every
  account that uses this system policy. sbc removes only its own file.

## Manual cleanup after the scope change

New sbc versions do not manage Chromium, Edge, Brave, Firefox, macOS or Windows.
They also do not clean settings that an older sbc wrote there. If you used those
targets, remove the old sbc-owned settings manually.

The simplest migration is to run `sbc webrtc off` with the older client before
upgrading. If the old command is already gone, use the locations below.

### Linux Chromium, Edge and Brave

Remove only sbc-owned files from the relevant directory:

| Browser          | Directory                                |
| ---------------- | ---------------------------------------- |
| Chromium         | `/etc/chromium/policies/managed`         |
| Chromium-browser | `/etc/chromium-browser/policies/managed` |
| Edge             | `/etc/opt/edge/policies/managed`         |
| Brave            | `/etc/brave/policies/managed`            |

The files are `sbc-webrtc-<uid>.json`, `.sbc-webrtc-<uid>.owner` and the exact
legacy `webrtc.json`. Remove the entire browser policy key only if it contains
nothing else.

### macOS

The old client wrote managed plists under `/Library/Managed Preferences/` for
Chrome, Chromium, Edge and Brave, with `.sbc-webrtc-<uid>.plist` ownership files.
It also edited Firefox profile preferences. Remove only the sbc-owned plists and
preferences; keep administrator-managed settings.

### Windows

The old client wrote per-user policy values under:

- `HKCU\Software\Policies\Google\Chrome`
- `HKCU\Software\Policies\Microsoft\Edge`
- `HKCU\Software\Policies\BraveSoftware\Brave`
- `HKCU\Software\Policies\Mozilla\Firefox\Preferences`
- `HKCU\Software\Policies\sbc\WebRTC` (ownership records)

Remove only values that sbc created. Do not delete an entire browser policy key.

### Firefox

Plain Firefox cleanup is no longer provided. The old client reset these four
preferences in `prefs.js` and `user.js`:

- `media.peerconnection.ice.no_host`
- `media.peerconnection.ice.default_address_only`
- `media.peerconnection.ice.proxy_only`
- `media.peerconnection.ice.proxy_only_if_behind_proxy`

Reset them manually in each profile, or close Firefox and remove the declarations
from both files. Unrelated preferences and administrator policies remain yours to
keep.

The old scope, ownership rules and platform details are in the repository
history before the Linux Chrome-only change.
