# RelayTerm Architecture

## Runtime topology

```text
Ctrl+Alt+Shift+R / Windows Terminal
        │
        ├── pc.agent (RelayTerm launcher: hidden Tk process, profile editor, hotkey, tunnel)
        │       ├── %LOCALAPPDATA%\RelayTerm\profiles.json
        │       ├── %LOCALAPPDATA%\RelayTerm\codex_bindings.json
        │       ├── %LOCALAPPDATA%\RelayTerm\drain_state.json
        │       ├── token.dpapi (Windows DPAPI)
        │       └── wt.exe -w 0 -> pc.attach_cli
        │
        └── bridge.relay_bridge (loopback HTTP/WebSocket)
                ├── CodexSessionService -> codex app-server --stdio
                └── SessionManager -> PtyBackend/ConPTY -> pwsh/powershell/cmd/wsl

Android MainActivity -> CatalogClient + PtyClient -> HTTPS/WSS bridge
```

The PC is the sole writer of the project catalog. Android writes only its own
Keystore-backed endpoint/token and a cache of server-managed records. A manual
Android profile remains a connection entry and can be used with an older bridge.

## Side-by-side bridge draining

The launcher probes the configured bridge before opening a `ProfileStore`. If
the listener is an older generation with running sessions, the launcher copies
the untouched legacy catalog to `profiles.v3.json`, migrates only that shadow,
starts the current bridge on the next available port, and atomically records the
topology in `drain_state.json`. This ordering prevents an old v1 reader from
seeing and recovering a v3 catalog as if it were damaged.

Session polling merges the primary bridge with every drain endpoint. A running
drain session owns its profile route for desktop attach, stop, delete, and
explicit Codex switching; profiles without an old session use the primary
bridge. The current bridge reads the topology on every new profile open and
returns `profile_draining` when the same profile still belongs to an old bridge,
preventing duplicate PTYs across generations.

An empty old bridge must remain empty for three polls. The launcher then resolves
the listening PID again, reads its Windows command line, and terminates it only
when both the saved PID and `bridge.relay_bridge` marker match. A launcher restart
reloads the topology and reconstructs routes before its panel can open.

When an old launcher still owns the standard single-instance mutex and `R`
hotkey, a newer generation starts under a versioned sidecar mutex and uses `D`.
The persisted settings port remains on the old bridge so legacy attach clients
continue to work; every new attach receives an explicit routed host/port. After
the old bridge and its verified parent launcher stop, the sidecar acquires the
standard mutex, switches to `R`, and commits the primary port to settings.

## Profile and launch boundaries

The validated profile schema is:

```text
id, name, workingDirectory, shell, startupCommand, launchMode, codexArgs, pinned, enabled
```

`ProfileStore` writes catalog version 3 and treats array position as the manual
position. During a v1 or legacy-list migration it applies the old numeric sort
once, removes that field, and persists the resulting array. Every later save
uses a stable partition with pinned profiles first and unpinned profiles second,
preserving relative array order inside both groups. Writes use a same-directory
temporary file plus `os.replace`. Invalid paths, duplicate IDs, unsupported
shells and overlong names/commands are rejected. A damaged file is renamed with
a timestamp before the default profile is recreated.

During v2-to-v3 migration, the existing array order is retained. Only the exact
legacy command `codex resume --last --yolo` becomes `launchMode=codex` with
`codexArgs=["--yolo"]`; ambiguous commands stay in command mode. Dynamic
auto/locked UUID state is stored separately in atomically replaced
`codex_bindings.json`, so mobile updates never race PC profile edits.

`PtyLaunchSpec` separates `legacy` command wrapping from `profile` interactive
shell mode. In profile mode the shell starts directly in the validated project
directory and the optional startup command is sent once. The shell remains
interactive after that command returns; only the shell's own exit emits the
session `exit` event. Legacy `/v1/exec` and WebSocket fields retain their prior
cwd and exit-code behavior.

Windows ConPTY children deliberately drop a host-injected `NO_COLOR`, replace a
missing or `dumb` TERM with `xterm-256color`, and advertise `COLORTERM=truecolor`.
PowerShell 7 starts with `$PSStyle.OutputRendering='Ansi'`. These values describe
the actual Windows Terminal frontend and prevent automation-host environment
variables from silently reducing Codex and pwsh output to monochrome.

## Codex thread catalog

`CodexAppServerClient` lazily owns one `codex app-server --stdio` JSONL process,
performs `initialize`/`initialized`, pages `thread/list`, applies a short cache,
bounds every request, and restarts the process once after transport, timeout, or
protocol failure. It uses only the public app-server protocol and never reads
Codex SQLite or rollout paths directly.

Discovery requests only `cli`, `vscode`, and `appServer` sources and excludes
archived, ephemeral, parent/sub-agent, `exec`, and unknown threads. Paths are
normalized before exact comparison. A Git-root comparison supplies a separate
same-repository list but never authorizes silent cross-directory resume. Exact
candidates use `recencyAt` and then `updatedAt` for newest-first selection. API
views contain only UUID, truncated title, source, cwd, update time, match type,
and status.

Auto mode selects the newest exact candidate, asks for an explicit choice when
only same-repository candidates exist, and calls `thread/start` when no candidate
exists. Locked mode never falls through when its UUID is missing or archived.
An already running RelayTerm PTY always wins an ordinary open. Explicitly opening
a different UUID returns `codex_thread_conflict`; PC and Android confirmation
flows then remove the old PTY and reconnect with that UUID.

New threads request the experimental `historyMode=legacy` contract explicitly:
the local CLI's default paginated thread may expose metadata without a resumable
rollout. `thread/name/set` materializes the named empty legacy history; a full
read of this new thread verifies it without adding a prompt or model turn. The
catalog process then closes to release its writer lock before CLI resume (an
unsubscribe alone leaves a grace period). Subsequent queries restart it lazily.
Existing threads use a fresh listing and a metadata-only read before launch.
Create requests are never replayed after timeout because they may have allocated
a UUID even if the response was lost.

The Windows PTY reader polls child liveness while waiting on pywinpty's socket.
Exit notifications are idempotent and late input is discarded. CLI startup
failures carry `fatal=true`; desktop and Android clients stop input and automatic
reconnection while retaining the original terminal output and binding.

The resume command is generated as argv-equivalent quoted PowerShell or cmd text
from the validated UUID and `codexArgs`; `--last` is not used. A nonzero resume
exit emits `codex_launch_failed` and closes the shell after streaming the native
CLI error. A normal Codex exit returns to the interactive profile shell.

## Session catalog and attachments

One enabled profile maps to one `PtySession`, keyed by its profile ID. A session
keeps a bounded 512 KiB output ring, wall-clock creation/activity timestamps,
PID, cwd and exit state. `GET /v1/sessions` exposes these values without exposing
command output or credentials.

Successful profile opens are recorded separately in the versioned,
atomically-written `recent_activity.json` beside `profiles.json`. Catalog and
`ready` responses expose `lastOpenedAt`. Pinned profiles display first in
persisted array order and ignore activity for positioning. Unpinned profiles
with valid activity display newest first; missing or invalid activity remains
at the end in catalog order, with name/ID only as final deterministic fallbacks.
Opening a pinned profile still updates its activity record.

The PC project tree changes this persisted order through its context menu.
Moving an unpinned profile up appends it to the pinned group, moving a pinned
profile up swaps it with the preceding pin, pinning places it first, and
unpinning returns it to recent ordering. Android caches the bridge array without
pre-sorting it and applies the same display ordering to the cached copy; local
Android profiles remain unpinned by default and keep their stored array order.

Every WebSocket attachment has its own bounded transport queue and metadata:
`clientId`, `clientType`, dimensions and role. PTY output is broadcast to all
attachments. `write_from` and `signal_from` share an input lock: the operation
first claims controller ownership, applies that attachment's dimensions, emits
`control_changed`, then writes/signals. A resize from an observer updates only
its stored dimensions. On controller disconnect, an online desktop is preferred
for fallback; otherwise the newest remaining attachment is selected. A new
desktop attachment closes the old desktop socket, while Android observers stay
connected. A failed/slow queue is removed independently.

Desktop liveness is independent from PTY lifetime. The session reports
`desktopState=never|connected|closed`, optional `desktopClosedAt`, and the actual
`codexThreadId`; a clean
desktop close, TCP disconnect, or heartbeat expiry removes only that desktop
attachment. Android attachments are not heartbeat-reaped, and shell exit keeps
the existing `state=exited`/exit-code precedence.

## Control and data planes

JSON text frames carry `open`, `ready`, `resize`, `signal`, `close`, `ping`,
`exit`, `error`, `control_changed` and `resync_required`. Binary WebSocket frames
carry raw terminal bytes in both directions. Bearer authentication is required
for catalog, exec, PTY and pairing-challenge creation; `/health`, the pairing
page and one-use exchange are intentionally separate routes.

## Pairing and transport

Quick Tunnel is an optional `cloudflared --protocol http2` child owned by the PC
launcher. The launcher parses the temporary HTTPS address, asks the bridge for a
128-bit two-minute challenge, and renders a QR image with Pillow/qrcode. The QR
contains only the HTTPS page. The page links to `relayterm://pair` with endpoint
and challenge; Android exchanges that challenge over HTTPS to receive the
DPAPI-origin token. Challenges live in memory, are atomically removed on
exchange, and are absent from logs. Tunnel stop, error and URL changes are shown
in the launcher panel.

## Process lifecycle and rollback

The launcher owns the bridge/tunnel PIDs and records them in `processes.json`.
`SingleInstance` uses a per-user named mutex; `HotKeyListener` registers
`Ctrl+Alt+Shift+R` with Win32 while Tkinter owns the UI loop. The default Run key entry
starts the `pc.agent` launcher hidden. `artifacts/rollback.ps1` verifies command-line markers
before stopping recorded children, removes the Run entry, and preserves personal
configuration unless `-PurgeConfig` is explicitly supplied.

## Compatibility and limits

The stdlib HTTP server remains the default executable path; `create_app` exposes
the same routes for deployments that choose aiohttp. Existing short-command
clients continue to work. PTYs are process-backed and in-memory, so a bridge
restart ends Windows sessions; reconnect and output replay apply while the
bridge remains alive. SSH/tmux and arbitrary existing Windows Terminal tabs are
future adapters rather than implicit imports.
