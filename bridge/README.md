# RelayTerm bridge

`relay_bridge.py` 同时提供旧命令接口和 profile PTY 接口。直接启动：

```powershell
$env:RELAYTERM_TOKEN = "读取 PC DPAPI token"
$env:RELAYTERM_PROFILE_PATH = "$env:LOCALAPPDATA\RelayTerm\profiles.json"
python bridge\relay_bridge.py
```

生产依赖：

```powershell
python -m pip install -r bridge\requirements.txt
```

## HTTP

- `GET /health` 返回服务状态和 `bridgeGeneration`，供启动器区分当前与待排空代际。
- bearer 鉴权的 `GET /v1/profiles` 返回 `version`、`revision` 和 profile 数组。
  catalog 版本为 3；每个 profile 带 `launchMode`、结构化 `codexArgs`、`pinned` 和可选
  `lastOpenedAt`（UTC ISO-8601）。数组
  顺序、`pinned` 和活动字段都参与 `revision`，响应中不再包含数字排序字段。
- bearer 鉴权的 `GET /v1/sessions` 返回每个 session 的 `profileId`、PID、running/state、
  cwd、created/lastActivity 时间、`lastOpenedAt`、desktopConnected、`desktopState`、
  `desktopClosedAt`、controller 和 attachments。
- `POST /v1/sessions/{profileId}/terminate` 停止 PTY；`/v1/exec` 保留旧的 shell/cwd 语义。
- bearer 鉴权的 `GET /v1/profiles/{id}/codex-sessions`、
  `PUT /v1/profiles/{id}/codex-binding` 和 `POST /v1/profiles/{id}/codex-sessions`
  分别查询候选、更新自动/锁定绑定和创建新 thread。候选不包含完整 prompt、历史或 rollout 路径。
- `POST /v1/pairing/challenges` 创建两分钟 challenge；公开的
  `POST /v1/pairing/exchange` 只接受一次 challenge，并返回 endpoint、token 和 catalog。

## WebSocket

`GET /v1/pty` 的 `open` 有两种形式：

```json
{"type":"open","profileId":"PROJECT_ID","codexThreadId":"THREAD_UUID","clientId":"desktop-1","clientType":"desktop","cols":120,"rows":40,"resume":true}
```

这是 profile shell 模式；bridge 从 catalog 解析工作目录、shell 和一次性启动命令。旧客户
端继续发送 `sessionId`、`startupCommand` 和 `cwd`，进入 legacy 包装命令模式。

`codexThreadId` 可省略，由自动/锁定绑定解析；运行中 profile 收到不同 UUID 时返回
`codex_thread_conflict`。`ready` 增加 `profileId`、`role`、实际 `codexThreadId` 和可选
`lastOpenedAt`。PTY 字节向所有 attachment 广播，每个连接拥有独立
有界发送队列；队列溢出只触发该连接的 `resync_required`。输入二进制帧或 `signal` 会原子
切换 controller，并广播：

```json
{"type":"control_changed","controllerClientId":"android-1","controllerClientType":"android"}
```

controller 断开时优先选择仍在线的 desktop；新的 desktop attach 替换旧 desktop，旧窗口
收到关闭并退出。session 在 socket 断开后继续保留，直到空闲回收。
桌面 attachment 的最后入站帧用于 heartbeat；显式发送
`{"type":"close","reason":"terminal_closed"}`、TCP 断开或约 15 秒无帧会将
`desktopState` 置为 `closed`，但不会关闭后台 PTY。Android attachment 不按 heartbeat 清理。

若 `RELAYTERM_DRAIN_STATE_PATH` 指向的原子状态文件仍将 profile 标记在旧 bridge，新版在
该 profile 尚未创建本地 session 时返回 `profile_draining`。已存在的新版 session 仍可正常
重连；状态文件更新后无需重启 bridge。

运行测试：

```powershell
python -m unittest discover -s bridge -p "test_*.py" -v
```
