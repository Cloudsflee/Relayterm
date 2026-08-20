# RelayTerm Architecture

## Runtime topology

```text
Ctrl+Alt+Shift+R / Windows Terminal
        │
        ├── pc.agent (RelayTerm launcher: hidden Tk process, profile editor, hotkey, tunnel)
        │       ├── %LOCALAPPDATA%\RelayTerm\profiles.json
        │       ├── token.dpapi (Windows DPAPI)
        │       └── wt.exe -w 0 -> pc.attach_cli
        │
        └── bridge.relay_bridge (loopback HTTP/WebSocket)
                └── SessionManager -> PtyBackend/ConPTY -> pwsh/powershell/cmd/wsl

Android MainActivity -> CatalogClient + PtyClient -> HTTPS/WSS bridge
```

The PC is the sole writer of the project catalog. Android writes only its own
Keystore-backed endpoint/token and a cache of server-managed records. A manual
Android profile remains a connection entry and can be used with an older bridge.

## Profile and launch boundaries

The validated profile schema is:

```text
id, name, workingDirectory, shell, startupCommand, order, enabled
```

`ProfileStore` accepts the legacy list shape, migrates it to a versioned object,
sorts by order/name, and writes through a same-directory temporary file plus
`os.replace`. Invalid paths, duplicate IDs, unsupported shells and overlong
names/commands are rejected. A damaged file is renamed with a timestamp before
the default profile is recreated.

`PtyLaunchSpec` separates `legacy` command wrapping from `profile` interactive
shell mode. In profile mode the shell starts directly in the validated project
directory and the optional startup command is sent once. The shell remains
interactive after that command returns; only the shell's own exit emits the
session `exit` event. Legacy `/v1/exec` and WebSocket fields retain their prior
cwd and exit-code behavior.

## Session catalog and attachments

One enabled profile maps to one `PtySession`, keyed by its profile ID. A session
keeps a bounded 512 KiB output ring, wall-clock creation/activity timestamps,
PID, cwd and exit state. `GET /v1/sessions` exposes these values without exposing
command output or credentials.

Every WebSocket attachment has its own bounded transport queue and metadata:
`clientId`, `clientType`, dimensions and role. PTY output is broadcast to all
attachments. `write_from` and `signal_from` share an input lock: the operation
first claims controller ownership, applies that attachment's dimensions, emits
`control_changed`, then writes/signals. A resize from an observer updates only
its stored dimensions. On controller disconnect, an online desktop is preferred
for fallback; otherwise the newest remaining attachment is selected. A new
desktop attachment closes the old desktop socket, while Android observers stay
connected. A failed/slow queue is removed independently.

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
