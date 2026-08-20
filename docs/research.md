# RelayTerm 轻量化调研与产品决策

更新时间：2026-08-10。目标用户需要在手机上切换多台终端并执行命令，重点是“远程操控”，而不是聊天客户端。下面的结论把功能拆成可验证的最小闭环。

## 需求拆解

| 需求 | MVP 验收证据 | 取舍 |
| --- | --- | --- |
| 切换不同终端 | 下拉列表、选中项持久化、每个终端独立输出 | 保留 |
| 发送各种终端行为 | 单行命令、回车/发送、退出码与 stderr | 保留 |
| 远程连接 | HTTPS JSON bridge、健康检查、Bearer token | 保留 |
| 轻量 | 仅 Android 平台 API，无 Compose/网络大依赖；单 APK | 保留 |
| 长任务控制 | PTY 持续输出、Ctrl-C/强制终止、512 KiB 环形缓冲 | 保留 |
| GPT 对话/自动代理 | 不影响终端闭环 | 删除 |
| 文件浏览、代码编辑、插件市场、通知中心、团队协作 | 与“手机遥控终端”无直接验收指标 | 删除 |

## 竞品观察

| 产品/项目 | 可借鉴 | 对本需求的负担 |
| --- | --- | --- |
| Termux | 本地 shell、包生态、键盘体验；官方站点说明其为 Android terminal/Linux environment | 包管理与本地环境体积较大，远程多主机切换不是主界面 |
| ConnectBot | SSH 主机列表与会话切换，开源、平台原生 | 以完整 SSH 客户端为中心，需要处理密钥、端口转发等大量设置 |
| JuiceSSH | 主机/身份分离、快速连接 | 账号/插件等扩展会增加配置面；商业维护状态需单独核实 |
| Tailscale SSH | 通过私网身份减少公网暴露；官方文档强调 ACL 与设备身份 | 需要额外网络控制平面，不适合作为 MVP 必选依赖 |
| 对话型 Codex/GPT 移动入口（按需求描述） | 移动端任务入口与对话体验 | 不能替代终端的 stdin/stdout 控制 |

来源核验于 2026-08-07：Termux、ConnectBot、Tailscale 与 Android 安全文档返回 HTTP 200；JuiceSSH 官网本次出现 TLS EOF，相关产品信息应在技术选型前再次核对。

- https://termux.dev/en/
- https://connectbot.org/
- https://juicessh.com/
- https://tailscale.com/kb/1193/tailscale-ssh
- https://developer.android.com/privacy-and-security/security-config

## 建议架构

```text
Android 单屏客户端
  ├─ TerminalStore (SharedPreferences + Android Keystore token)
  ├─ LocalShell (可选的本机沙盒命令)
  ├─ PtyClient (OkHttp WebSocket + binary stdin/stdout)
  └─ BridgeClient (兼容 HTTPS POST /v1/exec)
                  |
                  └─ Windows relay bridge -> ConPTY/WinPTY -> cmd.exe -> codex
```

短命令兼容协议保持不变；交互式协议使用：

```http
GET /health
POST /v1/exec
Authorization: Bearer TOKEN
Content-Type: application/json

{"command":"pwd","sessionId":"mobile-session"}
```

`GET /v1/pty?sessionId=PROFILE_ID` 升级为 WebSocket。客户端发送 `open`、二进制输入、`resize`、`signal` 和 `close`；服务端返回二进制 UTF-8 PTY 输出以及 `ready`、`exit`、`error`、`resync_required` 事件。每个 profile 的 session 在 bridge 内独立保留，输出环上限 512 KiB。

## 技术路线取舍

| 路线 | APK 负担 | 终端完整性 | 外部条件 | 结论 |
| --- | --- | --- | --- | --- |
| APK 内直连 SSH | 需要 SSH/密钥/PTY 库与更多设置页 | 高 | 每台主机开放 SSH | 第二选择；适合不愿部署 bridge 的用户 |
| HTTPS/WebSocket bridge | 客户端最小，协议只保留会话与输入输出 | 完整 PTY/TUI | 电脑运行一个小服务 | 推荐；最符合“轻量 + 多终端切换” |
| WebView 打开 ttyd 等网页终端 | APK 最薄 | 取决于网页终端 | 需要部署现成 Web 终端 | 可作为快速验证，不建议作为最终原生体验 |
| 远程桌面/屏幕投送 | 编解码、手势和带宽负担大 | 操作的是桌面而非终端 | 桌面常驻服务 | 删除 |

当前仓库同时保留 `/v1/exec` 兼容路径并提供正式 WebSocket + PTY；Android 使用固定网格 Canvas 渲染 ANSI，不引入 WebView 或 GPT SDK。

完整的方案比较、分层 adapter 设计和架构决策见 [`docs/architecture.md`](architecture.md)。

## 轻量化实测

- Debug APK：1,455,721 bytes（约 1.39 MiB），OkHttp 4.12.0 是唯一运行时网络依赖。
- 静态检查：Android Lint `No issues found`。
- 桥接协议测试：8/8 通过，覆盖 `/v1/exec` 兼容接口、token 校验、PTY ready/输出/输入、resize、中断和重连回放。
- Android 单元测试和 ANSI smoke test 均通过；原生网格覆盖分片 ANSI、SGR、CJK 宽字符、备用屏幕和滚屏。
- 黑盒链路：本机命令、远端命令、配置持久化、加密 token、多终端独立输出、停止命令和 AVD `10.0.2.2` PTY 均通过。

## 安全与运维边界

1. 远端地址强制 HTTPS；仅对回环/模拟器地址放行明文 HTTP，避免把 token 发到普通局域网。
2. bridge 默认绑定 `127.0.0.1`，通过反向代理或私网隧道发布；token 从环境变量读取，不写入仓库。
3. Android 端 token 使用 Keystore AES-GCM 加密；日志限制 200k 字符且不落盘。
4. 第一版不提供任意文件上传、端口转发、后台常驻和多用户权限系统；这些功能应在真实使用场景确认后单独评估。

## 后续验证顺序

1. 在本机启动 `bridge/relay_bridge.py`，用模拟器 `10.0.2.2` 做健康检查与 fake/`codex` PTY 验收。
2. 通过已安装的 Cloudflare Quick Tunnel 验证 TLS、WebSocket 升级和断线重连；本机 `cloudflared 2026.5.2` 使用 HTTP/2 临时隧道已返回 bridge `/health` 200。
3. 记录 APK 体积、冷启动、内存和连续 30 分钟输出；Named Tunnel 长期使用时继续把 origin 绑定在 `127.0.0.1`。

## 市场调研（2026-08-10）

本轮调研把问题定义为：**电脑端持有终端进程，手机端能在多个终端之间切换，并在网络短暂中断后继续操作**。资料来自产品官方站点、项目 README 和公开仓库，访问日期为 2026-08-10。表中的“持久 session”特指服务端能在手机客户端断开后继续持有同一个 shell/PTY；仅保存主机配置不计入。

### 竞品矩阵

| 产品/项目 | 手机平台 | 连接方式 | 持久 session | 多主机/多标签 | Windows 支持 | 原生移动端 | 接管已有终端 | 部署复杂度 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Termius | Android、iOS、桌面端 | SSH/SFTP、Telnet、Serial | 有限；配置跨端同步，长期 shell 通常依赖 tmux/Mosh | 强 | SSH/Telnet 可用 | 有 | 否；创建新的远端连接 | 低（客户端） |
| ConnectBot | Android | SSH | 有限；可重连，session 保活取决于远端 shell | 中 | 作为 SSH 目标可用 | 有 | 否 | 低 |
| Blink Shell | iOS/iPadOS | SSH、Mosh | Mosh 对移动网络友好，服务端 session 仍需 tmux 等保持 | 强 | 作为 SSH 目标可用 | 有（iOS） | 否 | 低 |
| Termux | Android | 本地 Linux 用户空间、SSH | 本地进程可运行，常与 tmux 配合 | 中 | 无；主要是 Android 本地环境 | 有 | 仅能接入自己创建的 tmux/SSH | 低 |
| ttyd | 浏览器/PWA | WebSocket + xterm.js，直接暴露一个命令 | 进程级；可重连，长期保活通常配 tmux | 弱到中（按实例） | 可在 Windows/WSL 等环境运行 | 否 | 否；通常启动新的命令 | 低到中 |
| WeTTY | 浏览器 | WebSocket + SSH | 由 SSH 主机和 tmux 决定 | 中 | 可连接 Windows SSH 服务 | 否 | 否 | 中 |
| GoTTY | 浏览器 | HTTP/WebSocket + CLI | 进程级，可配置重连 | 弱 | 可运行于 Windows 兼容环境 | 否 | 否 | 低到中 |
| sshx | 浏览器 | 中继 URL + WebSocket | 偏临时协作；会话依赖服务端存活 | 弱 | 可分享 Windows/WSL shell | 否 | 否 | 低 |
| ShellHub | 浏览器、移动浏览器 | Agent + SSH gateway | 有；设备和连接由服务器管理 | 强 | Windows 适配有限，重点是 Linux/边缘设备 | 否 | 否；由 agent 创建会话 | 高 |
| MeshCentral | 浏览器、移动浏览器 | 常驻 agent，终端/桌面/文件通道 | 有；agent 在线即可重连 | 强 | 强 | 否 | 通常否；打开 agent 新终端 | 中到高 |
| Apache Guacamole | 浏览器、移动浏览器 | guacd + SSH/RDP/VNC | 有限；网关连接可恢复，shell 保活仍看后端 | 强 | RDP/SSH 支持强 | 否 | 否；连接目标服务 | 高 |
| JumpServer | 浏览器、移动浏览器 | SSH/RDP/Kubernetes 等 PAM 网关 | 有；带审计和会话记录 | 强 | RDP/SSH 可用 | 否 | 否；通过网关新建连接 | 很高 |
| Nexus Terminal | 浏览器、PWA、桌面端 | SSH/RDP/VNC/Web terminal | 依赖后端连接与会话配置 | 强 | 支持 Windows 远程协议 | PWA，非专用原生客户端 | 否 | 中到高 |
| mux-pod | 移动浏览器 | SSH + tmux | 有；核心就是恢复 tmux | 中 | 需可用 SSH/tmux 环境 | 否 | 仅已有 tmux | 中 |
| ChatMux | 浏览器、桌面、移动浏览器 | 自托管 SSH/tmux workspace | 有 | 强 | 取决于 SSH/tmux 层 | 否 | 仅已有 tmux/workspace | 中 |
| web-ttyd-hub | 浏览器、移动浏览器 | ttyd + tmux | 有 | 强 | 取决于 ttyd 运行环境 | 否 | 仅已有 tmux | 中 |
| termstation / session-deck | 浏览器 | WebSocket + tmux/PTY | 有；以 workspace/session 为中心 | 强 | 取决于后端 shell | 否 | 仅已纳管的 session | 中 |
| Adiel-Sharabi/web-terminal | 浏览器、PWA | Windows 多 session Web terminal | 有限；由服务端实例持有 | 强 | 强 | PWA，非专用原生客户端 | 通常否 | 中 |
| cheep | 桌面、浏览器、移动浏览器 | 本地/远程命令 workspace | 项目较新，持久性取决于 adapter | 中 | 有本地 Windows 方向 | 否 | 需通过其 adapter 纳管 | 中 |
| RelayTerm（本项目） | Android 原生 | Windows ConPTY + HTTP/WebSocket；可选 Cloudflare/Tailscale | 有；bridge 断线保留最多 30 分钟 | 规划为强 | 强（原生 ConPTY） | 有 | 否；创建并持有自己的 PTY | 低到中 |

**矩阵读法。** “Windows 支持”只表示能把 Windows 作为连接目标或运行环境，并不代表能控制任意已打开的 Windows Terminal 标签页；公开产品几乎都要求 SSH、RDP、agent、tmux 或自身启动的 PTY。真正同时满足“Windows 本地 ConPTY + 原生 Android + profile 目录 + 断线恢复”的条目目前只有 RelayTerm 这一类自定义组合。

### 产品分层与可借鉴点

1. **移动 SSH 客户端：Termius、ConnectBot、Blink。** 它们把主机、凭据、标签和快捷键做得成熟，适合借鉴 profile 信息架构；session 的生命线仍在远端 shell，通常要配 tmux/Mosh。
2. **Web PTY：ttyd、WeTTY、GoTTY、sshwifty。** 部署快、浏览器覆盖面广，适合作为 RelayTerm 的故障排查入口；它们大多是“一条命令一个实例”，缺少统一 session catalog 和原生 Android 网格。
3. **持久化层：tmux、Mosh、Tailscale SSH。** tmux 负责进程和窗口，Mosh 负责移动网络下的连接，Tailscale 负责设备发现、身份和 ACL。三者解决的问题不同，可作为 RelayTerm 的 Unix/网络适配器，而非替换 PTY 管理器。
4. **设备与企业网关：ShellHub、MeshCentral、Guacamole、JumpServer、Nexus Terminal。** 它们提供审计、RBAC、RDP/VNC 或设备 fleet 能力，但部署、权限模型和资源占用明显超出个人 Codex 工作台的首版需求。
5. **直接相邻的小型项目：mux-pod、ChatMux、web-ttyd-hub、termstation、session-deck、web-terminal、cheep。** 说明“移动端恢复 workspace”有真实需求，但大多围绕 tmux 或浏览器，尚未形成 Windows ConPTY 与原生 Android 的完整产品闭环。

### 采用现成产品还是继续 RelayTerm

| 选项 | 适合场景 | 立即收益 | 主要缺口 |
| --- | --- | --- | --- |
| Termius/ConnectBot + SSH | 已有多台开启 SSH 的主机，只需手机登录 | 成熟的主机目录、密钥和快捷键 | 无法把电脑当前 Codex PTY 原样带到手机；需额外 tmux/服务端配置 |
| ttyd + tmux | 想最快得到浏览器入口，设备数量少 | 部署简单，session 可恢复 | 依赖浏览器；Windows ConPTY、原生 Android 体验和统一 profile 需要自行补齐 |
| MeshCentral/Guacamole | 需要远程桌面、文件、设备管理 | Windows 覆盖面和多设备能力强 | 服务栈重，终端只是众多功能之一 |
| RelayTerm 继续开发 | 核心用户是个人开发者，电脑上运行 Codex/PowerShell | 已有原生网格、ConPTY、重连和轻量 APK；产品差异明确 | 需要补齐多主机目录、Unix/tmux adapter、桌面只读镜像和更成熟的网络配置 |

**决策建议：继续开发 RelayTerm，并采用“兼容现成组件”的路线。** 理由有三点：

- 目标体验不是“从手机新建一个 SSH 连接”，而是“电脑上的工作 session 由 bridge 持有，手机只是随时接管”；现成移动 SSH 客户端没有这个 session registry。
- Windows Codex TUI 需要真实 ConPTY。ttyd、tmux 方案可以作为旁路入口或 Unix adapter，却不能直接替代当前 Windows backend。
- 已有 APK 约 1.39 MiB、bridge 和 Quick Tunnel 已通过黑盒验证，继续扩展的边际成本低于引入完整远程桌面/PAM 平台。

### 建议的产品边界（下一阶段）

**保留核心。** `TerminalProfile -> SessionManager -> PtyBackend` 作为统一抽象；profile 增加主机、项目目录、启动命令、标签和最近活动时间；手机切换只改变前台 socket，不终止 bridge session。

**增加适配器。** Windows 使用 ConPTY；Unix/Linux 使用 tmux adapter（创建、列出、附加、恢复 session）；SSH 作为可选远程 adapter。这样可以汇总本地 Windows、WSL、Linux 主机，而不把所有平台逻辑塞进 Android。

**网络默认值。** Quick Tunnel 只用于首次试用；长期个人使用优先 Tailscale/私网地址，Named Tunnel 作为需要公网 HTTPS 时的选项。所有路径继续由 bridge 校验 Bearer token，token 不进入 URL 或日志。

**先做的四项。**

1. session catalog：名称、主机、项目目录、运行状态、最近输出时间和标签搜索。
2. Unix/tmux adapter：协议字段与 Windows PTY 保持一致，补齐已有 tmux session 的列出/附加。
3. 桌面只读镜像：同一 session 允许一个观察者和一个控制者，明确接管提示，避免输出串线。
4. 诊断页：连接延迟、最近一次重连、PTY 尺寸、bridge 版本和 Cloudflare/Tailscale 路径状态。

**暂缓项目。** 任意 Windows Terminal 标签页附加、RDP/VNC、团队 RBAC、文件管理、审计录像和常驻 Android 服务。这些功能会显著扩大权限面与维护成本，先用真实使用数据确认需求。

### 来源与复核入口

以下链接用于复核产品定位和部署方式；版本、商业条款和维护状态在正式选型前再次检查：

- Termius：<https://termius.com/>；ConnectBot：<https://github.com/connectbot/connectbot>；Blink：<https://blink.sh/>；Termux：<https://termux.dev/en/>。
- ttyd：<https://github.com/tsl0922/ttyd>；WeTTY：<https://github.com/butlerx/wetty>；GoTTY：<https://github.com/yudai/gotty>；sshx：<https://github.com/ekzhang/sshx>；SSHwifty：<https://github.com/nirui/sshwifty>。
- ShellHub：<https://github.com/shellhub-io/shellhub>；MeshCentral：<https://github.com/Ylianst/MeshCentral>；Guacamole：<https://guacamole.apache.org/>；JumpServer：<https://github.com/jumpserver/jumpserver>；Nexus Terminal：<https://github.com/Heavrnl/nexus-terminal>。
- 相关 session 项目：<https://github.com/moezakura/mux-pod>、<https://github.com/binjie09/ChatMux>、<https://github.com/sosopop/web-ttyd-hub>、<https://github.com/kcosr/termstation>、<https://github.com/JesseProjects-LLC/session-deck>、<https://github.com/Adiel-Sharabi/web-terminal>、<https://github.com/jontuk/cheep>。
- 网络层参考：<https://tailscale.com/kb/1193/tailscale-ssh>；Cloudflare Tunnel：<https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/>。
