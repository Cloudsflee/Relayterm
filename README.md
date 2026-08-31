# RelayTerm

RelayTerm 是一个个人 PC 启动器：项目配置只在 Windows 电脑写入，启动器隐藏运行并由
`Ctrl+Alt+Shift+R` 唤出项目搜索面板。选择项目后，启动器复用该项目的 bridge PTY，并在新的
`140 x 42` Windows Terminal 窗口中打开原始 attach 客户端；手机可以同时观察并在输入
时取得 controller 角色。

## 启动

双击项目根目录的 `start_relayterm.cmd`。脚本检查 Python、pywinpty、qrcode、Pillow、
WebSocket 和 sv-ttk 依赖，然后隐藏启动 `pc.agent`。界面在启动时跟随 Windows 的浅色/深色
应用主题；若主题依赖不可用则回退到原生 `vista`。默认 bridge 地址是
`http://127.0.0.1:18765`；若端口已被占用，启动器保留现有进程并在面板显示冲突。默认只
启用启动器自启动，Quick Tunnel 通过面板的“远程访问”按需启动。

配置文件位于 `%LOCALAPPDATA%\RelayTerm`：

- `profiles.json`：原子写入的 PC 项目 catalog，字段为 `id`、`name`、`workingDirectory`、
  `shell`、`startupCommand`、`launchMode`、`codexArgs`、`pinned`、`enabled`。catalog v3
  以 profile 数组顺序保存手动置顶位置；旧 v1 的数字排序值只在首次迁移时使用一次，v2
  升级时保留现有数组顺序。精确的 `codex resume --last --yolo` 会迁移为 Codex 模式和
  `codexArgs=["--yolo"]`，其他命令继续作为自定义命令。
- `codex_bindings.json`：独立原子写入的 profile 自动/锁定 thread UUID 绑定；Android 只通过
  bridge 更新这个文件，不写 PC catalog。
- `drain_state.json`：原子写入的新旧 bridge 端口、PID、profile 路由和排空计数。首次从旧版
  切换时，新 bridge 使用 `profiles.v3.json` 影子 catalog，旧 bridge 继续读取原文件，避免
  v1/v3 并发读取触发 catalog 恢复。
- `recent_activity.json`：与 catalog 同目录的版本化活动记录，按 profile ID 保存最近一次
  成功收到 `ready` 的 UTC `lastOpenedAt`，独立原子写入。
- `settings.json`：端口、自启动和桌面 client ID。
- `token.dpapi`：Windows DPAPI 保护的长期 bearer token。
- `agent.log`、`processes.json`、`pairing.png`：轮转日志、进程归属和最近二维码。

支持的 shell 是 `pwsh`、`powershell`、`cmd` 和 `wsl`；Codex 会话识别首版限前三种 Windows
原生 shell，WSL 继续使用自定义命令。配置校验要求目录存在；启动命令
只在 profile shell 启动后写入一次，命令结束后回到 shell 提示符。
Windows ConPTY 子进程会清除宿主工具注入的 `NO_COLOR`/`TERM=dumb`，声明 truecolor，并将
PowerShell 7 的 `$PSStyle.OutputRendering` 设为 `Ansi`，因此 RelayTerm 不会把 Codex 或
PowerShell 的颜色降级为黑白。
项目列表中置顶项始终在前并保持手动顺序；其他项目按最近打开时间显示。右键项目可选择
“上移一格”“置顶”或“取消置顶”，搜索状态下执行这些操作会恢复完整列表。

## Bridge API

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/health` | 无鉴权健康检查 |
| GET | `/v1/profiles` | bearer 鉴权的 PC catalog |
| GET | `/v1/sessions` | profile、PID、状态、cwd、活动时间和 attachment 状态 |
| GET | `/v1/profiles/{id}/codex-sessions` | 自动/锁定状态、当前 UUID、精确及同仓库候选 |
| PUT | `/v1/profiles/{id}/codex-binding` | 设置自动模式或经验证的锁定 UUID |
| POST | `/v1/profiles/{id}/codex-sessions` | 创建并锁定一个新 Codex thread |
| POST | `/v1/sessions/{profileId}/terminate` | 停止 profile session |
| POST | `/v1/exec` | 保留旧的短命令/cwd 协议 |
| GET | `/v1/pty` | WebSocket 交互 PTY |

WebSocket `open` 可携带 `clientId`、`clientType=desktop|android`、`profileId` 和可选
`codexThreadId`；`ready` 与 session status 返回实际 UUID。不同 UUID 命中运行中的 PTY
会返回 `codex_thread_conflict`，由界面确认后显式终止并重连。同一
profile 固定映射到一个 session；输出广播给每个连接，各连接有独立有界队列。初始连接的
`ready.role` 为 controller 或 observer，并带可选 `lastOpenedAt`；任一端发送二进制输入或 `signal` 时原子接管，所有
连接收到 `control_changed`。observer 的 resize 只记录尺寸，接管时才应用到 PTY。新的
desktop attach 会替换旧 desktop attach，手机连接保持在线。

session status 另外公开 `desktopState=never|connected|closed` 和可选 `desktopClosedAt`。
桌面窗口关闭只移除 desktop attachment，后台 PTY 保留；shell 自身退出仍以 `state=exited`
和退出码为准。桌面客户端每约 5 秒发送 heartbeat，bridge 约 15 秒清理失联的 desktop，
Android attachment 不参与该清理。

启动器发现配置端口上运行的是旧代际 bridge 且仍有 session 时，会在下一个可用端口启动
新版。旧 session 的打开、停止和删除继续路由到旧端口；其他 profile 进入新版。新版读取
`drain_state.json` 并以 `profile_draining` 阻止仍在旧版运行的 profile 被重复启动。旧版连续
三次轮询没有运行 session 后，启动器再次核对监听端口 PID 与 `bridge.relay_bridge` 命令行，
匹配后结束旧进程。首次切换中旧 launcher 继续占用 `Ctrl+Alt+Shift+R`，新版 sidecar 使用
`Ctrl+Alt+Shift+D`；排空完成后新版核验并结束旧 launcher，再接管 `R`。全局设置端口在
排空期保持旧值，现有旧 launcher 因而仍能重新 attach；接管完成后才更新到新版端口。
启动器重启时会从排空状态继续，不会丢失路由。

Codex 模式懒启动 `codex app-server --stdio`，通过官方 `thread/list` 统一发现 CLI、VS Code
和 app-server thread。自动模式只恢复规范化目录完全相同的最新非归档主 thread；同 Git
仓库的其他目录必须由用户明确选择。没有候选时通过 `thread/start` 创建 UUID。锁定 UUID
失效或归档时保持绑定并提示重选；CLI 恢复错误保留绑定、显示原始终端输出且不自动 fork。

## 手机同步与配对

Android 保留原有 manual profile；连接成功后从 `/v1/profiles` 和 `/v1/sessions` 缓存 PC
managed 条目及其 `pinned`、Codex 启动字段和服务端数组顺序。managed 条目在手机上只读，展示时使用与 PC
相同的置顶/最近打开规则，token 继续由 Android Keystore 加密保存。启动器
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

Codex 协议依据：[Codex CLI reference](https://developers.openai.com/codex/cli/reference/) 与
[Codex app-server](https://developers.openai.com/codex/app-server/)。
