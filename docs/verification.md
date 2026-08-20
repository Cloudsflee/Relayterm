# Verification Record

验证对象：RelayTerm PC 启动器、bridge session catalog、多 attachment WebSocket、Android
catalog 同步和一次性配对实现。记录日期：2026-08-11。

`Agent` 仅保留在内部模块、PID 字段和日志文件名中；用户界面和启动提示统一称为“启动器”。

## Preserved Baseline

PC 启动器改动前的源码归档保存在 `artifacts/20260810-pre-pc-agent.zip`。校验命令和
literal output：

```powershell
Get-FileHash -Algorithm SHA256 artifacts\20260810-pre-pc-agent.zip
```

```text
Hash : 15858FB886A4D8160BA714829082FEDDF896BFEAB5DAA3B8DFE6DF7B82589315
exit status: 0
```

`artifacts/pre-agent-state.txt` 保存了归档说明；`artifacts/original-state.txt` 仍是更早的
空工作区历史记录。所有修改在当前工作区完成，未覆盖上述基线。

## Hotkey and Live Launcher

快捷键已统一为 `Ctrl+Alt+Shift+R`。Win32 注册使用
`MOD_CONTROL | MOD_ALT | MOD_SHIFT | MOD_NOREPEAT`，低级 hook 同时跟踪左右 Shift，
避开录屏工具占用的默认组合。

静态检查：

```powershell
rg -n "Ctrl\+Alt\+Shift\+R" README.md pc start_relayterm.cmd docs\architecture.md
```

输出包含 `README.md:4`、`pc\agent.py:1`、`pc\agent.py:165`、
`start_relayterm.cmd:20` 和 `docs\architecture.md:6,84`；exit status `0`。

在启动器已运行时的直接注册探针：

```text
new: registered=False error=1409
old: registered=False error=1409
exit status: 0
```

新组合的 `1409 (ERROR_HOTKEY_ALREADY_REGISTERED)` 来自当前启动器的注册；旧组合仍由
桌面录屏工具占用。使用 Win32 `keybd_event` 发送 `Ctrl+Alt+Shift+R` 后，窗口枚举得到：

```text
window: RelayTerm 项目
exit status: 0
```

本轮重启只处理 `%LOCALAPPDATA%\RelayTerm\processes.json` 中记录的进程。当前记录和健康
检查：

```text
agentPid=51368  bridgePid=32088  cloudflaredPid=0
GET http://127.0.0.1:18766/health -> 200 {"ok":true,"service":"relayterm"}
```

原有的 `127.0.0.1:18765` listener 未停止；当前启动器使用备用端口 `18766`。

## Python Tests

Bridge/API/PTY/session suite：

```powershell
python -m unittest discover -s bridge -p "test_*.py" -v
```

```text
Ran 17 tests in 16.830s
OK
exit status: 0
```

覆盖 bearer 鉴权、旧 `/v1/exec`、profiles/sessions/terminate、pairing challenge 单次交换、
真实 Windows PTY、profile shell 返回提示符、输出重放、多 attachment 广播、控制权切换、
resize 隔离、桌面替换和慢客户端隔离。

PC 配置、迁移、原子写入、损坏恢复和 DPAPI 往返：

```powershell
python -m unittest discover -s pc -p "test_*.py" -v
```

```text
Ran 4 tests in 0.079s
OK
exit status: 0
```

Python 编译检查（PowerShell 展开路径，避免 Windows Python 将 `*.py` 当作字面参数）：

```powershell
Get-ChildItem pc,bridge -Filter *.py -File |
  ForEach-Object { python -m py_compile $_.FullName }
```

```text
PY_COMPILE=OK
exit status: 0
```

## Android Build

```powershell
.\gradlew.bat --offline assembleDebug lintDebug :app:testDebugUnitTest
```

```text
BUILD SUCCESSFUL in 1s
49 actionable tasks: 1 executed, 48 up-to-date
exit status: 0
```

APK 比对：

```text
app/build/outputs/apk/debug/app-debug.apk
  bytes=1462667
  SHA256=1E39FBAB2AD5B72337104AC545A6701B7CDA61317E066F3E603B8AB7851DDC04
artifacts/RelayTerm-debug.apk
  bytes=1462667
  SHA256=1E39FBAB2AD5B72337104AC545A6701B7CDA61317E066F3E603B8AB7851DDC04
APK_MATCH=True, exit status: 0
```

签名检查：

```powershell
& "$env:ANDROID_HOME\build-tools\35.0.0\apksigner.bat" verify --verbose `
  artifacts\RelayTerm-debug.apk
```

输出包含 `Verifies` 和 `Verified using v2 scheme (APK Signature Scheme v2): true`，exit
status `0`。Android 离线单元测试和 lint 均已在同一 Gradle 命令中完成。

## Runtime and Pairing Checks

profile shell 黑盒测试确认：工作目录正确，启动命令执行一次后回到 PowerShell 提示符，
只有 shell 自身退出才发送 session `exit`；再次 attach 返回 `resumed=true` 并复用原 PTY。
Windows Terminal attach CLI 使用 `ws://127.0.0.1:18766`、DPAPI token、VT 输入输出和终端
resize。

Quick Tunnel 使用现有 `cloudflared --protocol http2` 子进程；本地 challenge 创建、单次
exchange、过期清理和二维码文件生成均通过 bridge/PC 测试。当前网络环境对临时公网域名的
TLS 握手受代理影响，公网请求未作为本轮成功条件；面板会显示隧道错误并隐藏 token、challenge。

## Patch and Reconstruction

`artifacts/implementation.diff` 从临时空 Git 仓库生成，排除 `build/`、`app/build/`、APK
和 patch 自身。验证命令：

```powershell
git apply --check --whitespace=error-all artifacts\implementation.diff
git apply --whitespace=error-all artifacts\implementation.diff
```

两条命令均 exit status `0`。验证仓库显式设置 `core.autocrlf=false` 后，重建目录的
literal 结果为 `SOURCE_FILES=69 REBUILT_FILES=69 MISSING=0 EXTRA=0 HASH_MISMATCHES=0`，
Gradle wrapper 为 `43705` 字节；具体命令、输入和状态同步保存在
`artifacts/verification-record.txt`。

## Launcher UI Verification

启动器 UI 更新的基线/修改后行为探针、`sv-ttk` 与 `vista` 降级测试、100%/125%/150%
截图、最小窗口检查、真实热键唤出、`18765` 进程保护和源码回滚执行记录位于
`artifacts/ui-verification-record.txt`。对应产物为：

- `artifacts/ui-baseline-20260811.zip`
- `artifacts/RelayTerm-ui-modified.zip`
- `artifacts/ui-implementation.diff`
- `artifacts/ui-rollback.ps1`

当前真实环境为 150% DPI；窗口报告 Per-Monitor awareness，热键唤出后搜索框取得焦点，
方向键选择会展开详情，Treeview Enter 单次打开并将新 Windows Terminal 窗口置前，Esc 会
重新隐藏窗口。PC 22 项测试和 bridge 17 项测试均通过。

## Rollback

默认回滚命令：

```powershell
.\artifacts\rollback.ps1
```

脚本只按 `processes.json` 中的 PID 和命令行 marker 停止启动器自有进程，移除当前用户
Run 项，并保留个人配置。`-PurgeConfig` 经过路径校验后才删除
`%LOCALAPPDATA%\RelayTerm`；`-UninstallAndroid` 单独处理 APK 卸载。模拟 `adb` 的已安装和
已移除分支均返回 exit status `0`，结果分别为 `Rolled back: com.relayterm.app` 和
`Already absent: com.relayterm.app`。

## Current State

真实启动器 PID `38436` 保持隐藏运行，bridge PID `40460` 的 endpoint 为
`http://127.0.0.1:18766`。旧 PID `25192` 的 `18765` bridge 未被本轮停止。临时测试目录和
日志已清理；个人配置、DPAPI token 和进程记录保留以便继续使用。
