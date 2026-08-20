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

- `GET /health` 保持 `{ "ok": true, "service": "relayterm" }`。
- bearer 鉴权的 `GET /v1/profiles` 返回 `version`、`revision` 和 profile 数组。
- bearer 鉴权的 `GET /v1/sessions` 返回每个 session 的 `profileId`、PID、running/state、
  cwd、created/lastActivity 时间、desktopConnected、controller 和 attachments。
- `POST /v1/sessions/{profileId}/terminate` 停止 PTY；`/v1/exec` 保留旧的 shell/cwd 语义。
- `POST /v1/pairing/challenges` 创建两分钟 challenge；公开的
  `POST /v1/pairing/exchange` 只接受一次 challenge，并返回 endpoint、token 和 catalog。

## WebSocket

`GET /v1/pty` 的 `open` 有两种形式：

```json
{"type":"open","profileId":"PROJECT_ID","clientId":"desktop-1","clientType":"desktop","cols":120,"rows":40,"resume":true}
```

这是 profile shell 模式；bridge 从 catalog 解析工作目录、shell 和一次性启动命令。旧客户
端继续发送 `sessionId`、`startupCommand` 和 `cwd`，进入 legacy 包装命令模式。

`ready` 增加 `profileId` 和 `role`。PTY 字节向所有 attachment 广播，每个连接拥有独立
有界发送队列；队列溢出只触发该连接的 `resync_required`。输入二进制帧或 `signal` 会原子
切换 controller，并广播：

```json
{"type":"control_changed","controllerClientId":"android-1","controllerClientType":"android"}
```

controller 断开时优先选择仍在线的 desktop；新的 desktop attach 替换旧 desktop，旧窗口
收到关闭并退出。session 在 socket 断开后继续保留，直到空闲回收。

运行测试：

```powershell
python -m unittest discover -s bridge -p "test_*.py" -v
```
