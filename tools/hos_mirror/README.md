# 鸿蒙自建实时投屏（开发调试用）

复用华为 hosScrcpy API 做视频源，自己写 PC 端接收 + Qt 显示，绕开 scrcpy（纯 adb，鸿蒙不支持）和 2fps 快照。

## 法律声明（务必先读）

- `hosScrcpy-1.0.18-beta.jar` 及其中 `.so` **版权归华为**，来自开源项目 `star-cast` / `hnscrcpy`，两者均声明**仅供个人学习研究、禁止商用、商用需向华为授权**。
- 本实现**仅用于本机开发调试**，jar 放在被 gitignore 的 `.tools/.downloads/`，**不随产品分发、不进打包产物**。
- 若产品要分发给客户，请勿启用本功能，改用外置 hnscrcpy（用户自行安装）或保持快照回退。

## 链路

```
Qt 控制台 (_start_hos_native)
  -> HosMirrorClient (client.py)
     -> java -cp hosScrcpy.jar Sidecar.java <sn> <hdc> <port>   [Sidecar.java 调华为 API]
        -> 设备 uitest 加载 libscreen_casting.z.so -> H.264 硬件编码
        -> hosScrcpy gRPC -> sidecar 本机 TCP 转发裸 H.264 帧
     -> PyAV 解码 Annex-B H.264 -> BGR ndarray
  -> Qt 信号桥 -> QLabel 显示（HosFrameBridge / _on_hos_frame）
```

sidecar 内部自动处理：`.so` 推送、uitest daemon 版本适配（真机 `uitest 7.0.0.1` 自动选 `libscrcpy_server_unix_6.6`）、`hdc fport`、gRPC 握手、唤醒屏幕。

## 依赖（均已在本机就绪）

- Java：DevEco JBR `D:\HuaWei\DevEco-Studio\jbr\bin\java.exe`（用 source-file mode 直接跑 `.java`，无需 javac）
- hdc：`D:\HuaWei\Sdk\26.0.0\toolchains\hdc.exe`
- jar：`.tools/.downloads/star-cast/resources/hos/hosScrcpy-1.0.18-beta.jar`（18MB，git clone star-cast 得到）
- Python：`av`(PyAV)、numpy、PyQt5

## 使用（已集成进 PC 端 GUI）

双击项目根 `启动控制台.bat`（走源码 `launch_console.ps1` -> `win_console/app.py`）打开 GUI：
1. 设备列表里鸿蒙设备带 `[鸿蒙]` 标签（后端 `/api/devices` 返回 `transport=hdc`）。
2. 选中它，点「启动(选中)」→ `start_mirror` 检测 transport=hdc → `_start_mirror_hdc` → 优先 `_start_hos_native`。
3. 画面直接嵌在左侧投屏区（QLabel），标签显示「鸿蒙(H.264 实时)」。
4. 日志区可见 `[鸿蒙投屏] ...` 与 `▶ 启动鸿蒙实时投屏(自建H.264)`。

降级顺序：自建组件缺失/失败 → 外置 hnscrcpy（独立窗口）→ 后端 2fps 快照。

**打包注意**：`build_console_exe.ps1` 打包前会临时移走 `.tools/.downloads/star-cast`（华为版权 jar），
产物不含该组件，部署机自动降级到快照/外置工具，符合"不随产品分发"。本机保留组件仍可用自建投屏。

## GUI 集成关键实现

- `HosFrameBridge(QObject)`：`pyqtSignal(object)` 跨线程把解码线程的 BGR 帧送到 GUI 主线程。
- `_on_hos_frame`：BGR ndarray → 连续内存 `.tobytes()` → `QImage(Format_BGR888)` → QLabel pixmap。
  （注意 PyQt5 不接受 numpy `memoryview`，必须转 bytes。）
- **cell 必须显式 `show()`**：投屏格子用 `setGeometry` 手动定位、不进 layout，父窗口 `show()` 不会级联显示它；
  漏掉会导致「日志在跑但左侧全黑」。在 `_apply_geometry()` 里对每个 `cell` 调 `show()`。
- 首帧到达会打日志 `GUI 已收到首帧 WxH 并显示`，显示异常也会打日志，便于区分「没推流」和「推流但显示失败」。
- **鼠标输入**：`MirrorInputLabel` 继承 QLabel，单击归一化坐标后由 `_on_hos_tap` 直接调
  `hdc -t <sn> shell uitest uiInput click <px> <py>`；按住拖拽（>18px）走 `uiInput swipe x1 y1 x2 y2 ms`。
  坐标用「点击位置 / label 当前尺寸 × 帧分辨率」，因为 `setScaledContents(True)` 会把帧拉伸填满 label。
  日志会显示「点击 (px,py) OK / 滑动 … OK」。异步线程执行，不阻塞 GUI。
- 后端 `POST /api/device/<serial>/tap` 也修成路由感知（`adb.tap` → 鸿蒙走 `Hdc.tap`），Web 控制台点画面同样可用；
  但需重新编译后端 exe 才生效（GUI 直连 hdc 不受此限）。
- **uitest 参数坑（实测）**：`uiInput swipe` 第 5 参是 `velocity`(200~40000)，**不是时长 ms**——传 466 这种小值页面不滚动，3000 才正常；
  `keyEvent` 只认 `Back`/`Home`/`Power` 字符串，Android keycode(3/4/187/26) 静默无效。GUI 已按此修正（velocity 由拖拽时长反推），
  后端 `Hdc.keyEvent`/`Hdc.swipe` 同步兼容（keycode 映射字符串 + ms 反推 velocity）。
- **任务键**：鸿蒙 `uitest keyEvent` 无 APP_SWITCH/Recent 等价，GUI 点「任务」会提示改用底部上滑手势，不发假成功的命令。
- 断连恢复：TCP 断开 → client 自动重启 sidecar（gRPC 通道关闭后无法原进程恢复），最多 3 次退避重连。
- 启动/重连前清理残留 daemon：`HosMirrorClient._cleanup_stale_uitest()` 用 `hdc -t <sn> shell ps -ef` 找出残留的
  `uitest start-daemon` 并 kill。这是"连上却黑屏"的头号根因：uitest 是 singleness 模式，上次投屏残留的
  daemon（尤其带 `--extension-name libscreen_casting.z.so`）占死通道，新会话报 `createChannel UNAVAILABLE` /
  `start scrcpy fail`，永不推流。sidecar 日志里若全是 scrcpy 重试、没有 `onData`，就是这个。
- 首帧看门狗：TCP 连上 ≠ 有画面（socket backlog 握手成功但侧无推流）。连上后 `FIRST_FRAME_TIMEOUT=25s` 内
  收不到首个 H.264 帧就主动关 socket 触发重建；状态如实显示「等待设备推送画面…」→「视频通道就绪，开始显示」，
  不再在零帧时空报「已连接视频流，开始解码」。

自检（独立）：
```
python tools/hos_mirror/client.py <serial> --preview 5
```

## 已知特性

- 推流按屏幕变化触发：静态画面几乎不发帧，属正常（不是卡死）。交互时可到 20~30fps。
- 首次启动需 5~15 秒：sidecar 会尝试多个 `.so` 变体找与设备 uitest 匹配的版本。
- 锁屏后黑屏（HarmonyOS 安全窗口限制），解锁后自动恢复。
- 无音频通道（hosScrcpy 本身不提供）。
- 日志：`.tools/hos_sidecar_<serial>.log`（sidecar 详细过程）。

## 真机验证（Pura70 HBN-AL00 / API 26 / uitest 7.0.0.1 / 2026-11-22）

- sidecar 自动识别 uitest 7.0.0.1 并选对 `.so`，`startCaptureScreen` 成功（`onReady`）。
- 裸流：8 秒收到 94 个 H.264 AU，全部 Annex-B 起始码，首帧 SPS+PPS+IDR。
- PyAV 解码出真实图像帧：BGR `(2844, 1260, 3)`。
- Qt 集成（真实事件循环 `app.exec_()` 验证）：跨线程帧信号成功投递到 GUI 槽，BGR→QImage 转换成功（0 错误），启动/停止生命周期正常。

稳定性与断连处理（2026-11-22 优化）：
- 正常停止投屏不再误报 `[ERR] ConnectionResetError`（那是 terminate Java 进程后 TCP 自然断开）。
- 重复点「启动」同一设备会被忽略（避免反复重建 sidecar、反复试 .so）。
- TCP 断开后自动**重启整个 sidecar**重连（华为 gRPC 通道一旦 shutdown 无法在原进程恢复），带退避最多 3 次，日志显示「重启投屏服务重连 (N/3)」/「重连成功」。
- 移除了 IDR 定时请求：它在 gRPC 关闭后每 2 秒刷 `Channel shutdown` 警告且无实际帮助；静态画面帧少是华为按变化推流的特性，QLabel 保留上一帧不会黑屏。
- sidecar 首次启动偶发多次 `.so` 重试（5~15 秒）属正常。
