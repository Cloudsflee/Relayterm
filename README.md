# RelayTerm

RelayTerm 是一个个人 PC 启动器：项目配置只在 Windows 电脑写入，启动器隐藏运行并由
`Ctrl+Alt+Shift+R` 唤出项目搜索面板。选择项目后，启动器复用该项目的 bridge PTY，并在新的
Windows Terminal 窗口中打开原始 attach 客户端；手机可以同时观察并在输入
时取得 controller 角色。

## 启动

双击项目根目录的 `start_relayterm.cmd`。脚本检查 Python、pywinpty、qrcode、Pillow、
WebSocket 和 sv-ttk 依赖，然后隐藏启动 `pc.agent`。界面在启动时跟随 Windows 的浅色/深色
应用主题；若主题依赖不可用则回退到原生 `vista`。默认 bridge 地址是
`http://127.0.0.1:18765`；若端口已被占用，启动器保留现有进程并在面板显示冲突。默认只
启用启动器自启动，Quick Tunnel 通过面板的“远程访问”按需启动。

配置文件位于 `%LOCALAPPDATA%\RelayTerm`：

- `profiles.json`：原子写入的 PC 项目 catalog，字段为 `id`、`name`、`workingDirectory`、
  `shell`、`startupCommand`、`order`、`enabled`。
- `settings.json`：端口、自启动和桌面 client ID。
- `token.dpapi`：Windows DPAPI 保护的长期 bearer token。
- `agent.log`、`processes.json`、`pairing.png`：轮转日志、进程归属和最近二维码。

支持的 shell 是 `pwsh`、`powershell`、`cmd` 和 `wsl`。配置校验要求目录存在；启动命令
只在 profile shell 启动后写入一次，命令结束后回到 shell 提示符。

## Bridge API

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/health` | 无鉴权健康检查 |
| GET | `/v1/profiles` | bearer 鉴权的 PC catalog |
| GET | `/v1/sessions` | profile、PID、状态、cwd、活动时间和 attachment 状态 |
| POST | `/v1/sessions/{profileId}/terminate` | 停止 profile session |
| POST | `/v1/exec` | 保留旧的短命令/cwd 协议 |
| GET | `/v1/pty` | WebSocket 交互 PTY |

WebSocket `open` 可携带 `clientId`、`clientType=desktop|android` 和 `profileId`。同一
profile 固定映射到一个 session；输出广播给每个连接，各连接有独立有界队列。初始连接的
`ready.role` 为 controller 或 observer；任一端发送二进制输入或 `signal` 时原子接管，所有
连接收到 `control_changed`。observer 的 resize 只记录尺寸，接管时才应用到 PTY。新的
desktop attach 会替换旧 desktop attach，手机连接保持在线。

## 手机同步与配对

Android 保留原有 manual profile；连接成功后从 `/v1/profiles` 和 `/v1/sessions` 缓存 PC
managed 条目。managed 条目在手机上只读，token 继续由 Android Keystore 加密保存。启动器
生成两分钟、单次使用的 128 位 pairing challenge；二维码只包含 HTTPS 配对页。点击 Android
标题栏的相机按钮可在应用内直接扫码配对；也可用系统相机打开配对页，再通过
`relayterm://pair` 深链回到应用。两条入口都会通过 HTTPS exchange 取得 token，因此 token
不进入 URL 或日志。

## 构建和验证

```powershell
python -m unittest discover -s bridge -p "test_*.py" -v
python -m unittest pc.test_config -v
.\gradlew.bat --offline assembleDebug lintDebug :app:testDebugUnitTest
```

APK 输出在 `app/build/outputs/apk/debug/app-debug.apk`。完整黑盒命令、基线哈希、修改产物、
回滚和 patch 记录见 `docs/verification.md` 与 `artifacts/implementation.diff`。

Android 正式版本发布、签名密钥和公开下载仓库配置见 `docs/release.md`。

## 回滚

```powershell
.\artifacts\rollback.ps1                 # 停止启动器/bridge/tunnel，移除自启动，保留配置
.\artifacts\rollback.ps1 -PurgeConfig   # 额外删除 %LOCALAPPDATA%\RelayTerm
.\artifacts\rollback.ps1 -UninstallAndroid
```

旧的 `bridge\start_quick_tunnel.ps1` 继续作为无 UI 回退入口；它复用 DPAPI token，不会把
token 写到 tunnel 参数或日志。

架构取舍记录在 `docs/architecture.md`，市场调研记录在 `docs/research.md`。
