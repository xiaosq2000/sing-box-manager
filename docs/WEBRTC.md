# Chrome privacy in sbc

Checked on 2026-10-21.

sbc manages WebRTC leak protection, website geolocation blocking and language
preferences for **Google Chrome on Linux**. The three components have independent
choices. `sbc privacy browser on|off` attempts all three and reports each result.
Any failed component makes the command exit nonzero. Successful components stay
applied. Geolocation and language matching require explicit activation.

The WebRTC component writes the mandatory Chrome policy
`WebRtcIPHandling=disable_non_proxied_udp` under
`/etc/opt/chrome/policies/managed`. Chrome Beta, Dev and Canary use the same
directory. Language matching changes only this account's personal profiles.

Chromium, Edge, Brave, Firefox, macOS and Windows are not managed. The previous
cross-platform implementation remains in the repository history.

| Command                               | Behavior                                                                                       |
| ------------------------------------- | ---------------------------------------------------------------------------------------------- |
| `sbc privacy browser`                 | Show all three choices, local geolocation policies and saved language profile status           |
| `sbc privacy browser on\|off`         | Attempt all three components independently                                                     |
| `sbc privacy browser geolocation on`  | Block website geolocation for all accounts. May request sudo                                   |
| `sbc privacy browser geolocation off` | Remove only the owned geolocation policy. May request sudo                                     |
| `sbc privacy browser webrtc on`       | Write the Chrome policy and enable automatic repair. May request sudo                          |
| `sbc privacy browser webrtc off`      | Remove the owned policy and disable automatic repair. May request sudo                         |
| `sbc privacy browser language on`     | Replace personal profiles' languages with the node language. Chrome must be closed             |
| `sbc privacy browser language off`    | Restore the language fields sbc still owns. Chrome must be closed                              |
| `sbc desktop on`                      | Enable the desktop proxy and ensure the WebRTC policy unless opted out                         |
| `sbc on`                              | Repair the WebRTC policy when the desktop proxy follows sbc, unless opted out                  |
| `sbc off`, `sbc desktop off`          | Disable the proxy and keep all three Chrome choices                                            |
| `sbc uninstall`                       | Restore owned languages and remove owned WebRTC and geolocation policies before client removal |
| `sbc privacy`                         | Summarize Chrome choices and region matching                                                   |

`privacy browser webrtc on|off` works without a subscription, a running proxy,
a GNOME session, or a default route. Use this explicit subcommand for WebRTC-only
scripts. Language matching needs a cached node region or a subscription that
provides one. Existing WebRTC choices do not enable language matching.

Queries change nothing. Failed cleanup keeps the client and recovery records
available for retry. WebRTC off records the opt-out even if policy removal fails.
Only successful WebRTC on clears that opt-out.

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

## WebRTC limits

- The policy restricts direct UDP. It does not disable the WebRTC API and does
  not route every application through the proxy.
- Administrator policies and extensions can override or bypass the setting.
  `chrome://policy` shows the effective value.
- `sbc desktop on` fails before enabling a new desktop proxy when Chrome already
  has a conflicting WebRTC policy.
- `sbc off` and `sbc desktop off` keep the Chrome policy. This can restrict
  WebRTC calls while the proxy is off.
- If Google Chrome is not installed, `privacy browser webrtc on` records the choice but
  writes no policy. Install Chrome and run it again.
- Chrome's managed policy directory is system-wide, so the policy affects every
  account that uses this system policy. sbc removes only its own file.

## Website geolocation blocking

```sh
sbc privacy browser geolocation       # Inspect the choice and local policies
sbc privacy browser geolocation on    # Block location requests, including saved site grants
sbc privacy browser geolocation off   # Withdraw only sbc's policy
```

`on` configures the mandatory Chrome policy `DefaultGeolocationSetting=2` under
`/etc/opt/chrome/policies/managed`. It requires sudo when a policy write is
needed. The policy affects all accounts and Chrome profiles that use this system
policy directory, including new profiles. It can be configured before Chrome is
installed. Snap and Flatpak packages are outside this scope.

The command needs no subscription, node metadata, running proxy or desktop
session. It does not edit personal preferences or require Chrome to be closed.
Restart Chrome after a change. Inspect `chrome://policy` for the effective policy
and any URL exceptions.

The default choice is off. Only explicit `geolocation on` or `browser on` enables
it. Installation, upgrades, refreshes, desktop setup and proxy toggles do not
enable or repair it. If a policy disappears, status reports the missing setting.
Run `geolocation on` explicitly to repair it.

### Ownership, conflicts and cleanup

The policy file is `sbc-geolocation-<uid>.json`. Its adjacent ownership record is
`.sbc-geolocation-<uid>.owner`. The elevated helper rechecks paths, ownership and
constant policy bytes. It rejects symbolic links. It publishes files atomically
with mode `0644`, regardless of a private umask.

An existing matching policy remains unowned. A different
`DefaultGeolocationSetting`, a nonempty `GeolocationAllowedForUrls`, or invalid
types in either geolocation URL list block activation. sbc reports file paths, not URL values.
It does not overwrite administrator policies or create an allowlist exception.
Malformed or unsafe policy files also prevent activation.

`off` records the disabled choice before cleanup. It removes only its own exact
policy file. Externally changed files remain intact and appear in diagnostics.
It does not set location permission to allow, delete saved site permissions or
change OS location services. Removing the policy lets other Chrome settings
control permission again. External policies can still restrict or allow location.

The private `geolocation-mode` in sbc's config directory contains `on` or `off`.
Its absence means off. Ownership records remain available after failed cleanup.
Uninstall refuses to remove the client if owned-policy cleanup fails. Retry
`geolocation off` or uninstall after correcting the failure. Before downgrading to
a client without this feature, run `sbc privacy browser geolocation off`.

### Diagnostics and limits

Status distinguishes the saved choice, a local blocking value, ownership and
conflicting policy files. It writes nothing and makes no sudo, browser or network
request. It returns nonzero for conflicts, a missing enabled policy or pending
owned cleanup. `off` can succeed while external policies remain, and reports them.

Local files do not prove a running browser's effective policy. Cloud policies,
extensions and other policy sources need separate inspection in Chrome. Verify
that website location requests are denied after restarting Chrome. This feature
does not hide IP-based location estimates, cookies, account data or other browser
fingerprint signals. It neither spoofs coordinates nor manages system location
services.

## Chrome language matching

```sh
sbc privacy browser language       # Show the choice and saved profile status
sbc privacy browser language on    # Exit Chrome before this command
sbc privacy browser language off   # Restore owned fields after exiting Chrome
```

The target is `nodes[].region.language` for the subscription's default node.
The ordered list contains the regional language and its base language, such as
`en-US,en` or `de-DE,de`. sbc does not guess from the exit IP. It changes both
`intl.selected_languages` and `intl.accept_languages` in each profile's
`Preferences`. It also updates these fields under `account_values` when they
already exist. Chrome's account preferences can override the top-level values.
sbc creates no account store or missing account language field. Chrome's [language preference code](https://chromium.googlesource.com/chromium/src/+/main/components/language/core/browser/language_prefs.cc)
derives the effective list from the selected languages and managed language policies. `ForcedLanguages` alone cannot remove other languages.

This feature needs neither sudo nor a generated system locale. It does not enable
`sbc privacy region` or change the system locale, time zone, browser UI language,
translation settings or spell-check settings.

### Profile scope and safe writes

sbc searches `google-chrome`, `google-chrome-beta`, `google-chrome-unstable` and
`google-chrome-canary` below `CHROME_CONFIG_HOME`, then `XDG_CONFIG_HOME`, then
`~/.config`. It manages existing `Default` and `Profile N` personal profiles.
It excludes Guest Profile, System Profile, other accounts and custom data
roots such as `--user-data-dir` or `CHROME_USER_DATA_DIR`. It creates no profiles.
A new profile needs another explicit language on command.

Exit Chrome completely before on, off or uninstall. Do not start Chrome while
the command runs. A `SingletonLock`, `SingletonSocket` or `SingletonCookie`
blocks writes, including a stale singleton file. sbc neither terminates Chrome
nor removes singleton files. It rejects symbolic links, invalid JSON, read-only
preferences and files owned by another account. It rechecks the singleton and
file contents before replacing preferences atomically.

The private `browser-language.json` in sbc's config directory stores the choice,
target and per-profile recovery records. A record saves the original top-level
language fields, their absence state and sbc's written values. It separately
saves any existing account language fields that sbc manages. It contains no
copy of other preferences or account identity data. sbc saves the record before
each profile write and keeps it after interrupted or failed changes.

Account records use recovery format version 2. sbc can extend version 1 records
without replacing their original top-level values. A client that supports only
version 1 refuses version 2 records. Before a downgrade, restore languages with
the client that wrote the record.

Off records the disabled choice before restoration. It restores each field only
if that field still equals a value sbc wrote. It preserves external changes and
reports the affected profiles. It does not recreate removed account fields or
stores, including after sign-out. Other preferences remain intact. Failed restores
keep their records for retry, and prevent uninstall from removing the client.
A damaged record needs manual recovery. Do not discard it before restoring the
language fields.

### Refresh, synchronization and limits

- `sbc refresh` updates the saved target and reports pending application. It never
  writes Chrome preferences. Exit Chrome and run language on to apply the target.
- A refresh without region metadata keeps the last target. A new subscription
  without it marks the target unavailable. Run language off to restore owned fields.
- Proxy toggles and upgrades do not implicitly enable or apply language matching.
- Chrome settings sync can send these languages to other devices or overwrite
  them here. sbc warns but does not change synchronization settings.
- Local `ForcedLanguages` policies block application. Other policies or extensions
  can change effective values. sbc does not bypass administrator settings.
- Status checks saved preferences, not the state of a running browser. Runtime
  changes can remain invisible until Chrome saves them.
- Check `chrome://settings/languages`, `navigator.language`, `navigator.languages`
  and an actual HTTP `Accept-Language` request after starting Chrome. Chrome can
  reduce the exposed list or treat incognito mode differently. Do not expect
  byte-identical lists in every mode.
- This is not a persistent language lock or complete fingerprint protection.
  Other account preferences, cookies and other browser signals remain outside its scope.

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
