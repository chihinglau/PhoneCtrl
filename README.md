# PhoneCtrl — PC 端投屏与手机自动化控制

> 复刻参考项目 `MyGameSim`（趣玩匣 GamePouch）的 PC 端工程结构：
> **PowerShell 脚本层 + Dart 零依赖服务层 + ADB 封装层 + Web 监控台**，
> 实现通过 ADB 控制手机**自动打开指定 App → 滑屏 → 点击广告 → 领取奖励**的完整流程；
> 并提供 **PyQt5 Windows 桌面控制台**，把 scrcpy 原生投屏与设备控制整合进同一个窗口，
> 支持**多设备同时投屏、画面按手机竖屏比例均匀分配**。

`版本 v0.3.0` · 平台：Windows 10/11 + Android（adb 可达）· 语言：Dart 3 + Python 3.10 + PowerShell 5.1

设计文档见 [`docs/投屏控制方案设计.md`](docs/投屏控制方案设计.md)。

## 能力矩阵

| 模块 | 能力 |
|------|------|
| 投屏架构 | scrcpy 实时投屏 + `screencap` 独立抓帧，二者解耦；桌面端 Win32 原生窗口嵌入（30~60fps 不卡） |
| 多设备投屏 | 一键「全部投屏」，按手机竖屏比例自动均匀分配画面，互不遮挡，窗口缩放实时重排 |
| ADB 封装 | `Adb` 类（exe 解析 / shell / run / devices / tap / swipe / startActivity），对齐参考项目范式 |
| 桌面控制 | Home / 返回 / 任务 / 电源 / **一键解锁（唤醒+上滑+输 PIN）** / 自动滑屏开关 / 实时日志 |
| 自动化 | openApp / tap / swipe / keyEvent / waitApp / waitStable，全部带重试与耗时统计 |
| 广告识别 | 策略模式：区域坐标 / 模板匹配 / OCR 接口（可插拔），含误触防护 |
| 奖励校验 | 「领取前快照 → 动作 → 领取后校验」三段式（数值 / UI / 副作用） |
| 异常处理 | 设备/指令/识别/逻辑四类异常 + 指数退避 `RetryPolicy` |
| 调度监控 | JSON 剧本驱动、多设备并发、断点续跑、Web 控制台实时状态 |

## 桌面控制台（推荐）

把 scrcpy 原生渲染窗口直接嵌入 Qt 界面，一个窗口完成投屏 + 控制。

### 方式一：一键启动（本机开发）

```powershell
# 1) 安装唯一 Python 依赖（PyQt5）
pip install PyQt5

# 2) 双击 启动控制台.bat，或命令行：
powershell -ExecutionPolicy Bypass -File launch_console.ps1
# 也可在已注入环境（. .\scripts\use-env.ps1）的终端直接：
python win_console\app.py
```

### 方式二：打包为 exe 分发（目标机免装 Python / Dart）

```powershell
powershell -ExecutionPolicy Bypass -File build_console_exe.ps1
```

脚本会先 `dart compile exe` 把后端编译成 `.tools/phonectrl_server.exe`（目标机免装 Dart SDK），
再用 PyInstaller 把桌面控制台 + `.tools`（scrcpy / android-sdk / 原生后端）打成
`dist/PhoneCtrlConsole/` 文件夹。**把该文件夹整体拷贝到目标 Windows 电脑，双击
`PhoneCtrlConsole.exe` 即可运行**，无需安装 Python / PyQt5 / Dart SDK。

> 排错：若控制台提示「无设备 / 后端启动失败」，多为 PowerShell dot-source 时 `$PSScriptRoot`
> 错位（已修复）；详见 [`docs/投屏控制方案设计.md`](docs/投屏控制方案设计.md) 第 11 章。

功能：

- **多设备同时投屏**：点「全部投屏」，各设备按竖屏比例自动均匀分配（1 台居中、2 台左右、3 台一行……）
- **单设备控制**：「启动(选中)」投屏下拉选中的设备；「停止(选中)」「停止全部」对应停止
- **按键/导航**：Home、返回、任务、电源
- **一键解锁**：填锁屏 PIN → 点「解锁」，自动完成唤醒 → 上滑 → 输入 PIN（可选「上滑解锁」）
- **自动滑屏**：方向 + 随机间隔（ms），「应用配置」即时生效
- **日志面板**：所有操作与 scrcpy 异常退出原因实时可见

> 实现要点：Qt 逻辑像素与 Win32 物理像素的 DPI 换算、`SetParent` + `WS_CHILD|WS_CLIPSIBLINGS`
> 嵌入、按比例手动几何分配、多实例错峰启动——详见设计文档第 9 章。

## Web 控制台

```powershell
# 一键启动（预检设备 + scrcpy 投屏 + HTTP 监控台 8858）
powershell -ExecutionPolicy Bypass -File scripts\phone-ctrl.ps1
# 浏览器打开 http://127.0.0.1:8858/
```

也可直接运行 Dart 服务：

```powershell
dart run tools/control/server.dart
# 浏览器打开 http://127.0.0.1:8858/
```

## 自动滑屏（Auto-Swipe）

轻量级、与具体 App 无关的「自动滑屏」任务，已嵌入 Web 控制台，适合做 Feed/小说/视频的保活滑动。

- **设备上线即控**：每检测到一台在线设备，控制台即出现该设备的卡片，可单独启停。
- **方向可选**：上 / 下 / 左 / 右（相对坐标，自动适配分辨率与横竖屏）。
- **随机间隔档位**（区间内随机，避免机械节奏）：
  - 快 · 1–3 秒、中 · 5–10 秒、慢 · 15–30 秒、极慢 · 40–60 秒（内置默认）。
  - 另支持「自定义」最小/最大秒数。
- **档位可配置**：控制台「滑屏档位配置」面板可编辑每个档位的标签与最小/最大秒数，保存后写入
  `.tools/phonectrl.swipe.json`，重启服务后依然生效；「恢复默认」可重置。
- **多机并发互不干扰**：任务按设备 `serial` 在 `Scheduler` 内独立调度，一台手机一个任务槽，
  多台手机同时滑屏互不影响（同一台设备同时只能跑一个任务：脚本任务 或 滑屏任务，互斥）。

### Web 控制台操作

1. 打开 `http://127.0.0.1:8858/`。
2. 在对应设备卡片里选方向（上/下/左/右）→ 选间隔档位（或自定义秒数）→ 点「▶ 开始滑屏」。
3. 卡片实时显示：滑屏中 / 方向 / 已滑次数 / 下次滑屏倒计时；点「■ 停止」结束。
4. （可选）在「滑屏档位配置」里改档位区间后「保存档位配置」。

### 接口（供脚本/二次开发）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET  | `/api/swipe/config` | 读取当前档位配置 |
| POST | `/api/swipe/config` | 保存档位配置（JSON 数组） |
| POST | `/api/swipe/start`  | 启动滑屏：`{serial?, direction, rangeKey|custom,minMs,maxMs}` |
| POST | `/api/device/<serial>/stop` | 停止该设备的任意任务（脚本或滑屏） |
| GET  | `/api/status` | 所有设备状态（含 `taskKind`/`swipeDir`/`swipeCount`/`nextSwipeAt`） |

## 项目结构

```
PhoneCtrl/
├── scripts/        # PowerShell：环境注入 / 启动 / 停止（对齐 MyGameSim/scripts）
├── run_server.ps1  # 启动 Dart 服务（桌面控制台自动拉起）
├── tools/control/  # Dart 零依赖服务：server.dart + lib/* + web/
├── win_console/    # PyQt5 Windows 桌面控制台（app.py）
├── tasks/          # JSON 任务剧本（每个 App 一套识别/坐标配置）
├── docs/           # 方案设计文档
└── pubspec.yaml
```

## 变更日志

| 版本 | 说明 |
|------|------|
| v0.3.0 | 修复 dot-source `$PSScriptRoot` 错位导致的「无设备/后端启动失败」；后端自动拉起预算提至 90s 并回显日志；新增独立窗口模式 + scrcpy 自动重连；支持打包为可分发 exe（`build_console_exe.ps1` / 启动控制台.bat / launch_console.ps1） |
| v0.2.0 | 新增 PyQt5 桌面控制台：scrcpy 原生窗口嵌入、多设备按比例均匀分配、按键/一键解锁/自动滑屏/日志；新增 `/api/device/<serial>/unlock` 接口 |
| v0.1.0 | 初版：PowerShell + Dart 零依赖服务 + ADB 封装 + Web 监控台 + 自动滑屏 |

> 合规提示：本工具仅用于**你拥有合法使用权**的 App 的自动化测试/自用场景；
> 请勿用于刷量、作弊或违反平台规则的用途。
