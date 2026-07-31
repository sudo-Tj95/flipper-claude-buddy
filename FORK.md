# Fork changes

This is a fork of [jxw1102/flipper-claude-buddy](https://github.com/jxw1102/flipper-claude-buddy)
(MIT). Upstream is excellent and unmodified in every respect not listed here. Five
things differ, mostly for the same reason: this Flipper drives Claude Code sessions
that have write access to a home-automation stack, so the failure modes worth guarding
against are "approved something I couldn't read", "typed into the wrong window", and
"the decision quietly went back to a laptop I am nowhere near".

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

#### Choosing what prompts show — on the device

Long-press **RIGHT** → menu → **"Prompt: …"**. Pressing OK cycles it:

```
Prompt: Description   (default)
Prompt: Command
Prompt: Desc+Cmd
```

The setting is persisted on the Flipper (settings V3) and pushed to the host —
on `hello` at connect, and via a `pref` message when you change it mid-session —
because the bridge is what formats the text. So the choice belongs to whoever is
holding the device and about to approve something, and it survives restarts and
plugin reinstalls. The row is hidden in Claude Desktop mode, which carries its
own prompt text and does not involve the bridge.

The `permissionDetail` plugin option remains as a fallback for a `.fap` too old
to report a preference; the device always wins when it reports one.

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
  `claude-vscode.focus` command ("Claude Code: Focus input"). The bridge activates VS
  Code and presses a hotkey bound to it, so the caret is in a known place rather than in
  one of your source files. The binding **must** carry a `when` clause — see below.
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
  "command": "claude-vscode.focus",
  "when": "activeWebviewPanelId == 'claudeVSCodePanel'"
}
```

To use a different key, set the `vscodeFocusHotkey` plugin option (or
`FLIPPER_VSCODE_FOCUS_HOTKEY`) to match. Single characters and `F1`–`F20` are
supported; `F19` is a good choice if you have it, since nothing else claims it.

#### The `when` clause is not optional

`claude-vscode.focus` is not a pure focus command. Reading the shipped
`extension.js` (v2.1.220):

```js
registerCommand("claude-vscode.focus", async () => {
  if (!r.hasVisibleWebview())
    await commands.executeCommand("claude-vscode.editor.openLast");
  let n = window.activeTextEditor;
  if (!n) { t.fire(""); return }
  ...
  t.fire(`@${o}#${a}-${c}`)
})
```

Two side effects follow from that, and an unguarded binding hits both:

- **It navigates.** With no Claude webview visible it runs
  `claude-vscode.editor.openLast`, opening the *last* session in an editor tab.
  If you keep several Claude conversations open, that is not necessarily the
  session the Flipper is bound to — so a button press could be delivered to the
  wrong conversation. Observed in practice: pressing ENTER while a source file
  was the active tab jumped to an unrelated Claude tab.
- **It can inject an @-mention.** When a text editor is active with a non-empty
  selection it fires `@path#start-end` into the Claude input. ENTER then submits
  that. Harmless but wasteful — it costs a turn.

`activeWebviewPanelId == 'claudeVSCodePanel'` is the extension's own context key
(it uses it for its `cmd+n` binding). Gating on it makes the `openLast` branch
unreachable, because a Claude tab being active implies a visible webview.

**The residual limitation is architectural.** VS Code exposes no command to
focus a *specific* Claude session, and the bridge cannot read which tab is
active — the window title shows the conversation name, which is
indistinguishable from a filename. So when no Claude tab is active the hotkey
now correctly does nothing, and the keystroke goes wherever focus already is.
Leave the Claude tab you want to drive as the active tab when you walk away
from the machine. Permission Allow/Deny is unaffected either way: it runs
through hooks over the unix socket and never touches the keyboard.

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

## 5. Concurrent permission prompts queue instead of being dropped

Claude Code issues tool calls in parallel, so permission prompts arrive in
bursts. Upstream holds a single pending-permission slot and answers `busy` to
anything that arrives while a prompt is on screen:

```
14:09:21  Permission request: Bash tail -25 /tmp/claude-flipper-bridge.log
14:09:22  Permission request: Bash cat "/Users/tonyjoy/Library/Application …
14:09:22  Permission busy, rejecting
```

The rejected hook exits non-zero and Claude falls back to the desktop dialog —
the one place you cannot reach when the entire point of the device is approving
things away from the machine. The second decision silently stops being yours to
make from the Flipper.

Prompts now queue on a lock, one on screen at a time (the device has a single
permission view), with three bounds in `daemon.py`:

| Constant | Value | Meaning |
|---|---|---|
| `PERM_MAX_QUEUED` | 2 | May be waiting behind the visible one; past this, `busy` as before |
| `PERM_DISPLAY_TIMEOUT` | 60s | Budget once actually on screen |
| `PERM_QUEUE_WAIT_TIMEOUT` | 120s | Budget waiting to reach the screen |

The queue is deliberately shallow. A deep one means clicking through prompts
long after the context that produced them has scrolled away, which is worse
than falling back to the laptop.

Two details that are easy to get wrong, both covered by
`host-bridge/tests/test_permission_queue.py`:

- **The display timeout starts when the prompt reaches the screen**, not when
  it was enqueued. Otherwise a queued request burns its budget waiting and the
  user is shown a prompt already doomed to time out.
- **The hook's socket timeout must outlast the bridge's worst case**
  (`on-permission-request.py: TIMEOUT`, raised 60s → 185s > 120 + 60). Left at
  60s the hook hangs up on a prompt the user is still reading: they press ALLOW
  and the answer has nowhere to go. The test asserts the inequality so the two
  numbers cannot drift apart.

### Running the tests

The repo has no test framework and the bridge has no test-only dependencies:

```bash
python3 plugin/host-bridge/tests/test_permission_queue.py
```

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
