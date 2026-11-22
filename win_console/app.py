# PhoneCtrl — Windows 桌面控制台 (MVP)
#
# 用 PyQt5 把"scrcpy 流畅投屏"和"手机自动化控制台"集成进同一个窗口：
#   * 左侧：把 scrcpy 的原生渲染窗口通过 Win32 SetParent 嵌入进来
#           （scrcpy 自身就是 SDL 直接渲染，30~60fps 不卡；
#            并且它原生处理 鼠标点击=触摸、拖拽=滑动，零延迟操控）
#   * 右侧：设备列表 / Home·返回·任务·电源 按键 / 自动滑屏开关
#           —— 全部复用现有 Dart 后端 (http://127.0.0.1:8858) 的 REST 接口，
#              Dart 自动化引擎一行不改。
#
# 运行：
#   powershell -ExecutionPolicy Bypass -File run_server.ps1   # 先起后端(若未起)
#   python win_console/app.py
#
# 依赖：Python 3.10 + PyQt5（本机已装）；嵌入用 ctypes，无需 pywin32。

import os
import sys
import json
import time
import math
import ctypes
import threading
import subprocess
import urllib.request
import urllib.error

from PyQt5.QtCore import Qt, QTimer, pyqtSignal, QObject, QPoint, QRect
from PyQt5.QtGui import QImage, QPixmap
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QFrame, QLabel, QVBoxLayout, QHBoxLayout,
    QComboBox, QPushButton, QCheckBox, QSpinBox, QGroupBox, QTextEdit, QMessageBox,
    QGridLayout, QLineEdit, QToolButton, QSizePolicy,
)

BASE = "http://127.0.0.1:8858"


class HosFrameBridge(QObject):
    """鸿蒙自建投屏：解码线程的 BGR 帧 -> Qt 主线程信号桥。"""
    frame_ready = pyqtSignal(object)   # numpy BGR ndarray
    status = pyqtSignal(str)


class MirrorInputLabel(QLabel):
    """投屏画面：单击 -> 设备 tap；拖拽 -> 设备 swipe。坐标归一化到 0..1。"""
    tapped = pyqtSignal(float, float)
    swiped = pyqtSignal(float, float, float, float, int)

    def __init__(self, text=""):
        super().__init__(text)
        self._press = None
        self._press_t = 0.0

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self._press = ev.pos()
            self._press_t = time.time()
        super().mousePressEvent(ev)

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.LeftButton and self._press is not None:
            dx = ev.pos().x() - self._press.x()
            dy = ev.pos().y() - self._press.y()
            w, h = max(1, self.width()), max(1, self.height())
            nx1, ny1 = self._press.x() / w, self._press.y() / h
            nx2, ny2 = ev.pos().x() / w, ev.pos().y() / h
            if (dx * dx + dy * dy) ** 0.5 > 18:
                ms = max(120, min(800, int((time.time() - self._press_t) * 1000)))
                self.swiped.emit(nx1, ny1, nx2, ny2, ms)
            else:
                self.tapped.emit(nx2, ny2)
            self._press = None
        super().mouseReleaseEvent(ev)


def _hdc_path():
    p = os.environ.get("HDC_EXE")
    if p and os.path.isfile(p):
        return p
    return r"D:\HuaWei\Sdk\26.0.0\toolchains\hdc.exe"


def _app_base():
    """打包后指向 exe 所在目录；开发态指向本文件所在目录。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _resolve_project():
    """定位项目根（含 .tools / run_server.ps1 / pubspec.yaml 的目录）。

    兼容三种运行形态：
      * 开发态：win_console/app.py -> 父目录即项目根
      * --onedir：exe 与 .tools、run_server.ps1 同目录
      * --onefile：资源被解压到 sys._MEIPASS
    """
    base = _app_base()
    meipass = getattr(sys, "_MEIPASS", None)
    for cand in (meipass, base, os.path.join(base, "_internal")):
        if cand and (os.path.isfile(os.path.join(cand, "run_server.ps1"))
                     or os.path.isdir(os.path.join(cand, ".tools"))):
            return cand
    d = base
    while True:
        if (os.path.isfile(os.path.join(d, "run_server.ps1"))
                or os.path.isfile(os.path.join(d, "pubspec.yaml"))):
            return d
        p = os.path.dirname(d)
        if p == d:
            break
        d = p
    return base


PROJECT = _resolve_project()

# Windows 下以「无窗口」方式启动子进程，避免后端/辅助控制台弹出黑框
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

# ---------------------------------------------------------------------------
# scrcpy / adb 路径解析（与 Dart 端 screen.dart 的候选逻辑一致）
# ---------------------------------------------------------------------------
def android_home():
    env = os.environ.get("ANDROID_HOME")
    if env and os.path.isdir(env):
        return env
    candidates = [
        r"D:\ai\prj\trae\HuaWei\MyGameSim\.tools\android-sdk",
        os.path.join(PROJECT, ".tools", "android-sdk"),
    ]
    for c in candidates:
        if os.path.isdir(c):
            return c
    return None


def scrcpy_exe():
    home = android_home()
    candidates = []
    if home:
        candidates.append(os.path.join(home, "..", "scrcpy", "scrcpy.exe"))
    candidates.append(os.path.join(PROJECT, ".tools", "scrcpy", "scrcpy.exe"))
    candidates.append("scrcpy.exe")
    for c in candidates:
        if os.path.exists(c):
            return os.path.abspath(c)
    return None


def hnscrcpy_exe():
    """外置鸿蒙投屏工具 hnscrcpy（GUI，独立窗口）。不打包进产品，仅本机调试用。

    候选顺序：环境变量 HNSCRCPY_EXE -> 项目 .tools/hnscrcpy -> 常见安装目录。
    hnscrcpy 内置 hosScrcpy.jar + JRE + hdc，走 hdc fport 推 H.264（30~60fps）。
    版权：hosScrcpy.jar/hdc 归华为，仅供个人学习研究，勿随产品分发。
    """
    candidates = []
    env = os.environ.get("HNSCRCPY_EXE")
    if env:
        candidates.append(env)
    candidates += [
        os.path.join(PROJECT, ".tools", "hnscrcpy", "hnscrcpy.exe"),
        os.path.join(PROJECT, ".tools", "hnscrcpy", "app", "hnscrcpy.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "hnscrcpy", "hnscrcpy.exe"),
        r"C:\Program Files\hnscrcpy\hnscrcpy.exe",
        r"C:\Program Files (x86)\hnscrcpy\hnscrcpy.exe",
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return os.path.abspath(c)
    return None


# ---------------------------------------------------------------------------
# Win32 嵌入（ctypes，无额外依赖）
# ---------------------------------------------------------------------------
GWL_STYLE = -16
WS_CHILD = 0x40000000
WS_POPUP = 0x80000000
WS_CLIPSIBLINGS = 0x04000000
WS_CLIPCHILDREN = 0x02000000
SWP_FRAMECHANGED = 0x20
SWP_NOZORDER = 0x4
SWP_SHOWWINDOW = 0x40

_user32 = ctypes.windll.user32
_user32.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
_user32.FindWindowW.restype = ctypes.c_void_p
_user32.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
_user32.GetWindowLongW.restype = ctypes.c_long
_user32.SetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_long]
_user32.SetWindowLongW.restype = ctypes.c_long
_user32.SetParent.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
_user32.SetParent.restype = ctypes.c_void_p
_user32.SetWindowPos.argtypes = [
    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, ctypes.c_int, ctypes.c_uint,
]
_user32.SetWindowPos.restype = ctypes.c_int


def find_window(title):
    return _user32.FindWindowW(None, title)


def embed_window(hwnd, parent_wid, x, y, w, h):
    """把 hwnd 变成 parent_wid 的子窗口，摆到 (x,y) 尺寸 (w,h)（物理像素）。"""
    # 子窗口设为 WS_CHILD + WS_CLIPSIBLINGS，多设备时各占一格互不覆盖
    style = _user32.GetWindowLongW(hwnd, GWL_STYLE)
    style_u = (style & 0xFFFFFFFF)
    style_u = (style_u & ~WS_POPUP) | WS_CHILD | WS_CLIPSIBLINGS
    _user32.SetWindowLongW(hwnd, GWL_STYLE, ctypes.c_long(style_u).value)
    _user32.SetParent(hwnd, ctypes.c_void_p(parent_wid))
    move_embedded(hwnd, x, y, w, h)


def move_embedded(hwnd, x, y, w, h):
    if w <= 0 or h <= 0:
        return
    _user32.SetWindowPos(hwnd, None, x, y, w, h,
                         SWP_FRAMECHANGED | SWP_NOZORDER | SWP_SHOWWINDOW)


# ---------------------------------------------------------------------------
# 简易 HTTP 客户端（urllib，无额外依赖）
# ---------------------------------------------------------------------------
def http_json(method, path, data=None, timeout=8):
    url = BASE + path
    if method == "GET":
        req = urllib.request.Request(url)
    else:
        req = urllib.request.Request(
            url,
            data=json.dumps(data).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method=method,
        )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def backend_ok():
    try:
        urllib.request.urlopen(BASE + "/api/devices", timeout=2)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 可折叠分组框：点击标题栏箭头展开/收起，收起时只占一行高度
# ---------------------------------------------------------------------------
class CollapsibleBox(QWidget):
    def __init__(self, title="", parent=None, expanded=False):
        super().__init__(parent)
        self.toggle = QToolButton()
        self.toggle.setStyleSheet(
            "QToolButton{border:0;background:transparent;font-weight:bold;"
            "font-size:12px;color:#374151;padding:4px 2px;text-align:left;}"
            "QToolButton:hover{color:#2563eb;}"
        )
        self.toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.toggle.setArrowType(Qt.DownArrow if expanded else Qt.RightArrow)
        self.toggle.setText(f"  {title}")
        self.toggle.setCheckable(True)
        self.toggle.setChecked(expanded)
        self.toggle.clicked.connect(self._on_toggle)

        self.content = QWidget()
        self.content.setVisible(expanded)
        self.content.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self.toggle)
        lay.addWidget(self.content)

    def _on_toggle(self, checked):
        self.content.setVisible(checked)
        self.toggle.setArrowType(Qt.DownArrow if checked else Qt.RightArrow)
        # 通知父布局重新计算尺寸
        self.updateGeometry()
        if self.parentWidget():
            self.parentWidget().updateGeometry()

    def setExpanded(self, expanded):
        self.toggle.setChecked(expanded)
        self.content.setVisible(expanded)
        self.toggle.setArrowType(Qt.DownArrow if expanded else Qt.RightArrow)

    def contentLayout(self):
        """返回内容区垂直布局，用法与 QGroupBox 的 QVBoxLayout 一致。"""
        layout = QVBoxLayout(self.content)
        layout.setContentsMargins(8, 4, 8, 8)
        layout.setSpacing(6)
        return layout

    def contentGridLayout(self):
        """返回内容区网格布局（用于按键等多列排布）。"""
        layout = QGridLayout(self.content)
        layout.setContentsMargins(8, 4, 8, 8)
        layout.setSpacing(6)
        return layout


# ---------------------------------------------------------------------------
# 主窗口
# ---------------------------------------------------------------------------
class MainWindow(QMainWindow):
    log_signal = pyqtSignal(str)
    devices_signal = pyqtSignal(list)
    autoswipe_signal = pyqtSignal(dict)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("PhoneCtrl — 桌面控制台")
        self.resize(1100, 680)

        self.current_serial = None
        self._backend_tries = 0
        # 多设备投屏状态：serial -> {proc, title, hwnd, host, tries, embed}
        self.mirrors = {}
        self._restart_count = {}
        self._backend_proc = None    # 后端子进程（原生 exe / powershell）
        self._backend_logf = None    # 后端日志文件句柄
        self._closing = False        # 退出中标志，避免定时器重连拉起新进程
        self.ks_proc = None          # 快手打卡子进程（运行中可停止）
        self.ks_running = False
        self.kv_proc = None          # 快手看视频攒金币子进程
        self.kv_running = False
        self.dy_proc = None          # 抖音看视频攒金币子进程
        self.dy_running = False
        self.kd_proc = None          # 快手打卡子任务子进程
        self.kd_running = False

        self.log_signal.connect(self._append_log)
        self.devices_signal.connect(self._populate_devices)
        self.autoswipe_signal.connect(self._populate_autoswipe)

        self._build_ui()

        # 后端可用性检查（带自动启动）
        QTimer.singleShot(200, self.ensure_backend)

        # 设备列表自动刷新
        self.dev_timer = QTimer(self)
        self.dev_timer.timeout.connect(self.refresh_devices)
        self.dev_timer.start(5000)

        # 主程序退出时杀掉拉起的后端 + scrcpy 进程树并释放资源（关闭按钮 / 异常退出兜底）
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._cleanup)

        # 已嵌入窗口存活检测 + 自适应尺寸
        self.watch_timer = QTimer(self)
        self.watch_timer.timeout.connect(self._watch_embed)
        self.watch_timer.start(1000)

    # ---- UI ----
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        hbox = QHBoxLayout(central)

        # 左侧：多设备投屏区（手动几何布局，按手机竖屏比例均匀分配）
        self.video_area = QWidget()
        self.video_area.setStyleSheet("background:#181b22;")
        self.video_area.setAttribute(Qt.WA_NativeWindow, True)
        self.video_area.setMinimumSize(420, 520)
        self.placeholder = QLabel("点击右侧『启动投屏』或『全部投屏』\n将设备画面嵌入此处\n（scrcpy 原生渲染，不卡）")
        self.placeholder.setAlignment(Qt.AlignCenter)
        self.placeholder.setStyleSheet("color:#6b7280;font-size:13px;")
        self.placeholder.setParent(self.video_area)
        hbox.addWidget(self.video_area, stretch=4)

        # 右侧：控制台
        right = QVBoxLayout()
        right.setSpacing(8)

        # 设备
        dev_row = QHBoxLayout()
        dev_row.addWidget(QLabel("设备"))
        self.dev_combo = QComboBox()
        self.dev_combo.setMinimumWidth(180)
        dev_row.addWidget(self.dev_combo, stretch=1)
        self.refresh_btn = QPushButton("刷新")
        self.refresh_btn.clicked.connect(self.refresh_devices)
        dev_row.addWidget(self.refresh_btn)
        right.addLayout(dev_row)

        # 投屏
        mir_row = QHBoxLayout()
        self.start_mirror_btn = QPushButton("启动(选中)")
        # 注意：clicked 会把 checked 传给槽函数，必须用 lambda 屏蔽，否则 serial=False
        self.start_mirror_btn.clicked.connect(lambda: self.start_mirror())
        self.start_all_btn = QPushButton("全部投屏")
        self.start_all_btn.clicked.connect(lambda: self.start_mirror_all())
        mir_row.addWidget(self.start_mirror_btn)
        mir_row.addWidget(self.start_all_btn)
        right.addLayout(mir_row)
        mir_row2 = QHBoxLayout()
        self.stop_mirror_btn = QPushButton("停止(选中)")
        self.stop_mirror_btn.clicked.connect(lambda: self.stop_mirror())
        self.stop_all_btn = QPushButton("停止全部")
        self.stop_all_btn.clicked.connect(lambda: self.stop_all())
        mir_row2.addWidget(self.stop_mirror_btn)
        mir_row2.addWidget(self.stop_all_btn)
        right.addLayout(mir_row2)

        # 投屏模式
        mir_row3 = QHBoxLayout()
        self.mirror_indep = QCheckBox("独立窗口(不嵌入)")
        self.mirror_indep.setToolTip("勾选后 scrcpy 以独立窗口运行，规避嵌入窗口可能导致的断连")
        mir_row3.addWidget(self.mirror_indep)
        right.addLayout(mir_row3)

        # 按键 —— 可折叠
        key_box = CollapsibleBox("按键 / 导航")
        kg = key_box.contentGridLayout()
        self.btn_home = QPushButton("Home")
        self.btn_back = QPushButton("返回")
        self.btn_task = QPushButton("任务")
        self.btn_power = QPushButton("电源")
        # (按钮, Android keycode, 鸿蒙 uitest keyEvent 字符串；None=鸿蒙无等价键)
        for b, code, hoskey in ((self.btn_home, 3, "Home"), (self.btn_back, 4, "Back"),
                                (self.btn_task, 187, None), (self.btn_power, 26, "Power")):
            b.clicked.connect(lambda _checked, c=code, hk=hoskey: self.send_key(c, hk))
        kg.addWidget(self.btn_home, 0, 0)
        kg.addWidget(self.btn_back, 0, 1)
        kg.addWidget(self.btn_task, 1, 0)
        kg.addWidget(self.btn_power, 1, 1)
        right.addWidget(key_box)

        # 解锁 —— 可折叠
        un_box = CollapsibleBox("一键解锁")
        uv = un_box.contentLayout()
        self.un_pwd = QLineEdit()
        self.un_pwd.setPlaceholderText("锁屏密码(PIN 数字)")
        self.un_pwd.setEchoMode(QLineEdit.Password)
        self.un_pwd.returnPressed.connect(self.unlock)
        un_row = QHBoxLayout()
        self.un_swipe = QCheckBox("上滑解锁")
        self.un_swipe.setChecked(True)
        self.un_btn = QPushButton("解锁")
        self.un_btn.clicked.connect(self.unlock)
        un_row.addWidget(self.un_swipe)
        un_row.addWidget(self.un_btn, stretch=1)
        uv.addWidget(self.un_pwd)
        uv.addLayout(un_row)
        right.addWidget(un_box)

        # 自动滑屏 —— 可折叠
        sw_box = CollapsibleBox("自动滑屏")
        sv = sw_box.contentLayout()
        self.as_enabled = QCheckBox("启用自动滑屏")
        self.as_enabled.stateChanged.connect(self._on_as_toggled)
        h1 = QHBoxLayout()
        h1.addWidget(QLabel("方向"))
        self.as_dir = QComboBox()
        self.as_dir.addItems(["up", "down", "left", "right"])
        h1.addWidget(self.as_dir, stretch=1)
        h2 = QHBoxLayout()
        h2.addWidget(QLabel("间隔(ms)"))
        self.as_min = QSpinBox(); self.as_min.setRange(200, 60000); self.as_min.setValue(1000)
        self.as_max = QSpinBox(); self.as_max.setRange(200, 60000); self.as_max.setValue(3000)
        h2.addWidget(self.as_min)
        h2.addWidget(QLabel("~"))
        h2.addWidget(self.as_max)
        self.as_apply = QPushButton("应用配置")
        self.as_apply.clicked.connect(self.apply_autoswipe)
        sv.addLayout(h1)
        sv.addLayout(h2)
        sv.addWidget(self.as_enabled)
        sv.addWidget(self.as_apply)
        right.addWidget(sw_box)

        # 快手极速版打卡（纯免费看广告，不支付金币）—— 可折叠
        self.ks_box = CollapsibleBox("快手·连续打卡白拿新手机")
        kv = self.ks_box.contentLayout()
        ks_tip = QLabel("自动滚动+模板匹配定位「连续打卡白拿新手机」卡片，\n找不到就停止，绝不误点其它任务\n只走免费看广告，绝不消耗金币\n测试1个：看1条广告且不记幂等；开始打卡：最多20条+随机延时")
        ks_tip.setStyleSheet("color:#6b7280;font-size:11px;")
        ks_tip.setWordWrap(True)
        kv.addWidget(ks_tip)
        kr = QHBoxLayout()
        self.ks_start = QPushButton("开始打卡")
        self.ks_start.clicked.connect(lambda: self.start_ks_checkin(False))
        self.ks_dry = QPushButton("测试1个")
        self.ks_dry.clicked.connect(lambda: self.start_ks_checkin(True))
        self.ks_stop = QPushButton("停止")
        self.ks_stop.clicked.connect(self.stop_ks_checkin)
        self.ks_stop.setEnabled(False)
        kr.addWidget(self.ks_start, stretch=2)
        kr.addWidget(self.ks_dry, stretch=1)
        kr.addWidget(self.ks_stop, stretch=1)
        kv.addLayout(kr)
        right.addWidget(self.ks_box)

        # 快手极速版连续看视频攒金币（纯免费，不涉及支付）—— 可折叠
        self.kv_box = CollapsibleBox("快手·连续看视频攒金币")
        vv = self.kv_box.contentLayout()
        kv_tip = QLabel("任务中心「连续看视频7天」卡片：看视频攒金币，\n红包待领取后自动「一键领取」\n刷视频用随机时长，回任务中心下拉刷新检测\n测试1轮：不写幂等；开始攒金币：最多10轮")
        kv_tip.setStyleSheet("color:#6b7280;font-size:11px;")
        kv_tip.setWordWrap(True)
        vv.addWidget(kv_tip)
        vr = QHBoxLayout()
        self.kv_start = QPushButton("开始攒金币")
        self.kv_start.clicked.connect(lambda: self.start_ks_video(False))
        self.kv_dry = QPushButton("测试1轮")
        self.kv_dry.clicked.connect(lambda: self.start_ks_video(True))
        self.kv_stop = QPushButton("停止")
        self.kv_stop.clicked.connect(self.stop_ks_video)
        self.kv_stop.setEnabled(False)
        vr.addWidget(self.kv_start, stretch=2)
        vr.addWidget(self.kv_dry, stretch=1)
        vr.addWidget(self.kv_stop, stretch=1)
        vv.addLayout(vr)
        right.addWidget(self.kv_box)

        # 抖音极速版连续看视频赚金币（纯免费，不涉及支付）—— 可折叠
        self.dy_box = CollapsibleBox("抖音·连续看视频赚金币")
        dyv = self.dy_box.contentLayout()
        dy_tip = QLabel("赚钱任务页「连续看视频30天赚30元」卡片：\n看视频攒金币，红包满后点红包内「领取」\n弹窗只点「开心收下」，绝不点「看广告再赚」\n测试1轮：不写幂等；开始攒金币：最多10轮")
        dy_tip.setStyleSheet("color:#6b7280;font-size:11px;")
        dy_tip.setWordWrap(True)
        dyv.addWidget(dy_tip)
        dr = QHBoxLayout()
        self.dy_start = QPushButton("开始攒金币")
        self.dy_start.clicked.connect(lambda: self.start_dy_video(False))
        self.dy_dry = QPushButton("测试1轮")
        self.dy_dry.clicked.connect(lambda: self.start_dy_video(True))
        self.dy_stop = QPushButton("停止")
        self.dy_stop.clicked.connect(self.stop_dy_video)
        self.dy_stop.setEnabled(False)
        dr.addWidget(self.dy_start, stretch=2)
        dr.addWidget(self.dy_dry, stretch=1)
        dr.addWidget(self.dy_stop, stretch=1)
        dyv.addLayout(dr)
        right.addWidget(self.dy_box)

        # 快手 365 天打卡 · 6 子任务（纯免费，时长型回读 App 倒计时）—— 可折叠
        self.kd_box = CollapsibleBox("快手·365天打卡 今日6子任务")
        kdv = self.kd_box.contentLayout()
        kd_tip = QLabel("入口/按钮/领奖全用 OCR 识别屏幕文字定位，布局变化也不点空。\n"
                        "时长型回读倒计时 / 计数型回读 (N/20)(N/5)，一次运行做到完成。\n"
                        "只走免费项，绝不点消耗金币/快速通。")
        kd_tip.setStyleSheet("color:#6b7280;font-size:11px;")
        kd_tip.setWordWrap(True)
        kdv.addWidget(kd_tip)
        kd_sel = QHBoxLayout()
        kd_sel.addWidget(QLabel("子任务:"))
        self.kd_task = QComboBox()
        self.kd_task.addItems(["观看广告(0/20)", "每日签到", "观看精彩视频(80分)",
                               "观看精彩短剧(30分)", "观看热门内容(0/5)", "下载并体验(跳过)"])
        self.kd_task.setItemData(0, "ad")
        self.kd_task.setItemData(1, "signin")
        self.kd_task.setItemData(2, "video")
        self.kd_task.setItemData(3, "drama")
        self.kd_task.setItemData(4, "hot")
        self.kd_task.setItemData(5, "app")
        kd_sel.addWidget(self.kd_task, stretch=1)
        kdv.addLayout(kd_sel)
        kr = QHBoxLayout()
        self.kd_start = QPushButton("开始")
        self.kd_start.clicked.connect(lambda: self.start_kd_task(False))
        self.kd_dry = QPushButton("测试1次")
        self.kd_dry.clicked.connect(lambda: self.start_kd_task(True))
        self.kd_stop = QPushButton("停止")
        self.kd_stop.clicked.connect(self.stop_kd_task)
        self.kd_stop.setEnabled(False)
        kr.addWidget(self.kd_start, stretch=2)
        kr.addWidget(self.kd_dry, stretch=1)
        kr.addWidget(self.kd_stop, stretch=1)
        kdv.addLayout(kr)
        right.addWidget(self.kd_box)

        # 日志 —— 可折叠（紧跟任务块，折叠其它分组时自动上移）
        # 注意：此处不放 addStretch，否则日志会被顶到面板底部固定不动
        log_box = CollapsibleBox("日志")
        lv = log_box.contentLayout()
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(160)
        self.log.setStyleSheet("font-family:Consolas,Menlo,monospace;font-size:11px;")
        lv.addWidget(self.log)
        right.addWidget(log_box)

        # 剩余空白落在日志下方，日志本体跟随上方分组折叠而移动
        right.addStretch(1)

        hbox.addLayout(right, stretch=1)

        self.start_mirror_btn.setEnabled(False)
        self.stop_mirror_btn.setEnabled(False)

    # ---- 后端 ----
    def ensure_backend(self):
        if self._closing:
            return
        if backend_ok():
            self.log_signal.emit("✅ 后端已连接 (8858)")
            self.refresh_devices()
            self.load_autoswipe()
            self.start_mirror_btn.setEnabled(True)
            return
        if self._backend_tries == 0:
            self.log_signal.emit("⏳ 未检测到后端，尝试启动后端 ...")
            try:
                env = self._scrcpy_env()
                server_exe = os.path.join(PROJECT, ".tools", "phonectrl_server.exe")
                flags = CREATE_NO_WINDOW | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                if os.path.isfile(server_exe):
                    # 已编译的原生后端（console 子系统）：用 CREATE_NO_WINDOW 隐藏黑框
                    log_path = os.path.join(PROJECT, ".tools", "backend.log")
                    self._backend_logf = open(log_path, "w", encoding="utf-8", errors="replace")
                    self._backend_proc = subprocess.Popen(
                        [server_exe], env=env, stdout=self._backend_logf,
                        stderr=subprocess.STDOUT, creationflags=flags,
                    )
                else:
                    # 回退：run_server.ps1（隐藏 powershell / dart 窗口）
                    self._backend_proc = subprocess.Popen(
                        ["powershell", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
                         "-File", os.path.join(PROJECT, "run_server.ps1")],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        creationflags=flags,
                    )
            except Exception as e:
                self.log_signal.emit(f"⚠️ 启动后端失败: {e}")
        self._backend_tries += 1
        # 冷启动 dart run 可能要 30~60s（首次 pub get + 编译），给足 90s 预算
        if self._backend_tries < 45 and not self._closing:
            QTimer.singleShot(2000, self.ensure_backend)
        else:
            tail = self._backend_log_tail()
            self.log_signal.emit("❌ 后端启动失败，请手动运行 run_server.ps1"
                                 + (f"\n—— 后端日志 ——\n{tail}" if tail else ""))

    @staticmethod
    def _backend_log_tail(n=20):
        path = os.path.join(PROJECT, ".tools", "backend.log")
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                lines = [l.rstrip() for l in f if l.strip()]
            return "\n".join(lines[-n:])
        except Exception:
            return ""

    # ---- 设备 ----
    def refresh_devices(self):
        threading.Thread(target=self._refresh_devices, daemon=True).start()

    def _refresh_devices(self):
        try:
            devs = http_json("GET", "/api/devices")
        except Exception as e:
            self.devices_signal.emit([])
            return
        self.devices_signal.emit(devs if isinstance(devs, list) else [])

    def _populate_devices(self, devs):
        prev = self.dev_combo.currentData()
        self.dev_combo.clear()
        self._dev_transport = {}
        for d in devs:
            serial = d.get("serial")
            transport = d.get("transport", "adb")
            self._dev_transport[serial] = transport
            tag = "鸿蒙" if transport == "hdc" else ""
            label = f"{d.get('model','?')}  ({serial})" + (f"  [{tag}]" if tag else "")
            self.dev_combo.addItem(label, serial)
        if self.dev_combo.count() == 0:
            self.dev_combo.addItem("（无设备）", None)
            self.start_mirror_btn.setEnabled(False)
        else:
            self.start_mirror_btn.setEnabled(True)
            # 尽量保持原选择
            idx = self.dev_combo.findData(prev)
            if idx >= 0:
                self.dev_combo.setCurrentIndex(idx)
            else:
                self.dev_combo.setCurrentIndex(0)
        self.current_serial = self.dev_combo.currentData()

    def _current_serial(self):
        return self.dev_combo.currentData()

    # ---- 投屏嵌入（多设备） ----
    def _scrcpy_env(self):
        env = dict(os.environ)
        home = android_home()
        if home:
            env["ANDROID_HOME"] = home
            ptools = os.path.join(home, "platform-tools")
            if ptools not in env.get("PATH", ""):
                env["PATH"] = ptools + ";" + env.get("PATH", "")
        return env

    def start_mirror(self, serial=None):
        if self._closing:
            return
        if serial is None:
            serial = self._current_serial()
        if not serial:
            QMessageBox.warning(self, "提示", "请先选择一台在线设备")
            return
        if serial in self.mirrors:
            e = self.mirrors[serial]
            # 鸿蒙自建投屏运行中：重复点「启动」直接忽略，避免反复重启 sidecar
            # （每次重启要重新试 .so 5~15s，还会触发 ConnectionResetError）
            if e.get("tool") == "native" and e.get("hos_client") and e["hos_client"].running:
                self.log_signal.emit(f"ℹ️ {serial} 鸿蒙投屏已在运行，忽略重复启动")
                return
            self.stop_mirror(serial, silent=True)
        transport = self._dev_transport.get(serial, "adb")
        if transport == "hdc":
            # 鸿蒙设备：优先自建 H.264 投屏，降级 hnscrcpy / 快照
            self._start_mirror_hdc(serial)
            return
        exe = scrcpy_exe()
        if not exe:
            QMessageBox.critical(self, "错误", "找不到 scrcpy.exe")
            return
        title = f"PhoneCtrl-{serial}"
        embed = not self.mirror_indep.isChecked()
        if embed:
            # 每个设备一个格子：上方设备名 + 下方 scrcpy 宿主（纯 Qt 布局占位，非 native）
            cell = QWidget(self.video_area)
            cv = QVBoxLayout(cell)
            cv.setContentsMargins(0, 0, 0, 0)
            cv.setSpacing(2)
            tag = QLabel(serial)
            tag.setStyleSheet("color:#9aa4b2;font-size:11px;")
            cv.addWidget(tag)
            host = QFrame()
            host.setFrameShape(QFrame.StyledPanel)
            host.setStyleSheet("background:#10131a;")
            cv.addWidget(host, stretch=1)
            entry = {"proc": None, "title": title, "hwnd": None, "host": host,
                     "cell": cell, "tries": 0, "embed": True}
            self.mirrors[serial] = entry
            self._relayout()
        else:
            entry = {"proc": None, "title": title, "hwnd": None, "host": None,
                     "cell": None, "tries": 0, "embed": False}
            self.mirrors[serial] = entry
        env = self._scrcpy_env()
        os.makedirs(os.path.join(PROJECT, ".tools"), exist_ok=True)
        err_path = os.path.join(PROJECT, ".tools", f"scrcpy_{serial}.log")
        entry["errfile"] = open(err_path, "w", encoding="utf-8", errors="replace")
        try:
            if embed:
                self._apply_geometry()
                cv.activate()  # 立即落地格子内部布局
                _, _, init_w, init_h = self._embed_rect(entry)
                init_w, init_h = max(init_w, 320), max(init_h, 480)  # 兜底
                args = [exe, "-s", serial, "--window-title", title,
                        "--window-borderless", "--window-width", str(init_w),
                        "--window-height", str(init_h), "--max-fps", "60", "--no-audio"]
                tagtext = "嵌入中..."
            else:
                args = [exe, "-s", serial, "--window-title", title,
                        "--max-fps", "60", "--no-audio"]
                tagtext = "独立窗口"
            entry["proc"] = subprocess.Popen(
                args, env=env, stdout=entry["errfile"], stderr=entry["errfile"],
                creationflags=CREATE_NO_WINDOW,
            )
        except Exception as e:
            entry["errfile"].close()
            self.log_signal.emit(f"⚠️ 启动 scrcpy 失败: {e}")
            return
        self.log_signal.emit(f"▶ 启动 scrcpy: {serial} ({tagtext})")
        if embed:
            QTimer.singleShot(300, lambda: self._try_embed(serial))

    def start_mirror_all(self):
        serials = [self.dev_combo.itemData(i) for i in range(self.dev_combo.count())]
        serials = [s for s in serials if s]
        if not serials:
            QMessageBox.information(self, "提示", "当前没有在线设备")
            return
        # 错开启动，避免多台 scrcpy 同时抢占 adb/端口导致后者退出
        for i, s in enumerate(serials):
            QTimer.singleShot(i * 1500, lambda s=s: self.start_mirror(s))

    def _start_hos_native(self, serial):
        """自建鸿蒙投屏：hosScrcpy sidecar -> H.264 -> PyAV 解码 -> 本窗口 QLabel。
        仅开发调试用；hosScrcpy.jar 版权归华为，不随产品分发。"""
        client_py = os.path.join(PROJECT, "tools", "hos_mirror", "client.py")
        jar = os.path.join(PROJECT, ".tools", ".downloads", "star-cast", "resources",
                           "hos", "hosScrcpy-1.0.18-beta.jar")
        if not (os.path.isfile(client_py) and os.path.isfile(jar)):
            return False
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location("hos_mirror_client", client_py)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        except Exception as e:
            self.log_signal.emit(f"ℹ️ 自建鸿蒙投屏组件不可用({e})，尝试其它方式")
            return False

        cell = QWidget(self.video_area)
        cv = QVBoxLayout(cell)
        cv.setContentsMargins(0, 0, 0, 0)
        cv.setSpacing(2)
        tag = QLabel(f"{serial}  ·  鸿蒙(H.264 实时)")
        tag.setStyleSheet("color:#9aa4b2;font-size:11px;")
        cv.addWidget(tag)
        label = MirrorInputLabel("连接中…")
        label.setAlignment(Qt.AlignCenter)
        label.setStyleSheet("background:#10131a;color:#6b7280;")
        label.setScaledContents(True)
        label.setCursor(Qt.PointingHandCursor)
        label.tapped.connect(lambda nx, ny, s=serial: self._on_hos_tap(s, nx, ny))
        label.swiped.connect(lambda a, b, c, d, ms, s=serial: self._on_hos_swipe(s, a, b, c, d, ms))
        cv.addWidget(label, stretch=1)
        entry = {
            "proc": None, "title": f"PhoneCtrl-HOS-{serial}", "hwnd": None,
            "host": None, "cell": cell, "tries": 0, "embed": False,
            "transport": "hdc", "tool": "native", "label": label,
        }
        bridge = HosFrameBridge()
        entry["bridge"] = bridge
        bridge.status.connect(lambda m, s=serial: self.log_signal.emit(f"[鸿蒙投屏] {m}"))
        bridge.frame_ready.connect(lambda bgr, s=serial: self._on_hos_frame(s, bgr))
        self.mirrors[serial] = entry
        self._relayout()
        client = mod.HosMirrorClient(
            serial,
            on_frame=lambda bgr, b=bridge: b.frame_ready.emit(bgr),
            on_status=lambda m, b=bridge: b.status.emit(m),
        )
        entry["hos_client"] = client
        if not client.start():
            self.mirrors.pop(serial, None)
            cell.hide(); cell.deleteLater(); self._relayout()
            return False
        self.log_signal.emit(f"▶ 启动鸿蒙实时投屏(自建H.264): {serial}")
        return True

    def _on_hos_frame(self, serial, bgr):
        """主线程：把解码出的 BGR 帧画到对应镜像格子的 QLabel。"""
        e = self.mirrors.get(serial)
        if not e or self._closing or e.get("tool") != "native":
            return
        try:
            h, w, ch = bgr.shape
            if ch != 3:
                return
            e["frame_w"], e["frame_h"] = w, h
            # PyQt5 不接受 numpy memoryview，须用 bytes；.copy() 保证连续内存
            bgr = __import__("numpy").ascontiguousarray(bgr)
            qimg = QImage(bgr.tobytes(), w, h, 3 * w, QImage.Format_BGR888)
            if qimg.isNull():
                self.log_signal.emit(f"[鸿蒙投屏] QImage 构造失败 {w}x{h}")
                return
            e["label"].setPixmap(QPixmap.fromImage(qimg))
            if not e.get("_frame_logged"):
                e["_frame_logged"] = True
                self.log_signal.emit(f"[鸿蒙投屏] GUI 已收到首帧 {w}x{h} 并显示")
        except Exception as ex:
            self.log_signal.emit(f"[鸿蒙投屏] 显示帧异常: {type(ex).__name__}: {ex}")

    def _hos_input(self, serial, args, desc):
        """异步执行 hdc shell 输入命令，不阻塞 GUI。"""
        e = self.mirrors.get(serial)
        client = e.get("hos_client") if e else None
        hdc = getattr(client, "hdc", None) or _hdc_path()
        if not hdc:
            self.log_signal.emit(f"[鸿蒙投屏] 无 hdc 路径，{desc}未执行")
            return

        def work():
            try:
                r = subprocess.run(
                    [hdc, "-t", serial, "shell"] + args,
                    capture_output=True, text=True, timeout=10,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                detail = (r.stdout or "").strip() or (r.stderr or "").strip()
                ok = r.returncode == 0
                self.log_signal.emit(
                    f"[鸿蒙投屏] {desc} {'OK' if ok else '失败'}"
                    + (f"：{detail[:160]}" if detail else "")
                )
            except Exception as ex:
                self.log_signal.emit(f"[鸿蒙投屏] {desc}异常: {type(ex).__name__}: {ex}")

        threading.Thread(target=work, daemon=True).start()

    def _on_hos_tap(self, serial, nx, ny):
        e = self.mirrors.get(serial)
        if not e or self._closing or e.get("tool") != "native":
            return
        w, h = e.get("frame_w", 1260), e.get("frame_h", 2844)
        px, py = int(round(nx * w)), int(round(ny * h))
        self._hos_input(serial, ["uitest", "uiInput", "click", str(px), str(py)],
                        f"点击 ({px},{py})")

    def _on_hos_swipe(self, serial, nx1, ny1, nx2, ny2, ms):
        e = self.mirrors.get(serial)
        if not e or self._closing or e.get("tool") != "native":
            return
        w, h = e.get("frame_w", 1260), e.get("frame_h", 2844)
        x1, y1 = int(round(nx1 * w)), int(round(ny1 * h))
        x2, y2 = int(round(nx2 * w)), int(round(ny2 * h))
        # uitest swipe 第5参是 velocity(200~40000)，不是时长；太慢的 velocity 不会滚动
        velocity = max(200, min(40000, int(10000 / max(120, ms))))
        self._hos_input(serial,
                        ["uitest", "uiInput", "swipe", str(x1), str(y1), str(x2), str(y2), str(velocity)],
                        f"滑动 ({x1},{y1})->({x2},{y2}) v={velocity}")

    def _start_mirror_hdc(self, serial):
        """鸿蒙设备镜像：优先自建 Python 接收端（hosScrcpy sidecar + H.264，嵌到本窗口）；
        不可用则回退外置 hnscrcpy（独立窗口），再回退 /frame 快照（约 2fps）。"""
        if self._start_hos_native(serial):
            return
        exe = hnscrcpy_exe()
        if exe:
            # hnscrcpy 是 JavaFX GUI，独立窗口运行（不能像 scrcpy 用 --window-title 嵌入）
            title = f"PhoneCtrl-HOS-{serial}"
            entry = {
                "proc": None, "title": title, "hwnd": None, "host": None,
                "cell": None, "tries": 0, "embed": False, "transport": "hdc",
                "tool": "hnscrcpy",
            }
            self.mirrors[serial] = entry
            os.makedirs(os.path.join(PROJECT, ".tools"), exist_ok=True)
            log_path = os.path.join(PROJECT, ".tools", f"hnscrcpy_{serial}.log")
            errfile = open(log_path, "w", encoding="utf-8", errors="replace")
            entry["errfile"] = errfile
            try:
                # --serial 指定设备；--no-control 可只看不控（此处默认带控制）
                args = [exe, "--serial", serial]
                entry["proc"] = subprocess.Popen(
                    args, stdout=errfile, stderr=errfile, creationflags=CREATE_NO_WINDOW,
                )
                self.log_signal.emit(f"▶ 启动 hnscrcpy 鸿蒙投屏: {serial}（独立窗口，H.264）")
            except Exception as e:
                errfile.close()
                self.mirrors.pop(serial, None)
                self.log_signal.emit(f"⚠️ 启动 hnscrcpy 失败: {e}，回退快照预览")
                self._start_hdc_snapshot(serial)
            return
        # 未安装 hnscrcpy → 快照回退
        self.log_signal.emit("ℹ️ 未找到 hnscrcpy（可设 HNSCRCPY_EXE 或装到 .tools/hnscrcpy），回退 2fps 快照")
        self._start_hdc_snapshot(serial)

    def _start_hdc_snapshot(self, serial):
        """鸿蒙快照回退：后端 /frame 快照轮询（约 2fps）。"""
        title = f"PhoneCtrl-{serial}"
        cell = QWidget(self.video_area)
        cv = QVBoxLayout(cell)
        cv.setContentsMargins(0, 0, 0, 0)
        cv.setSpacing(2)
        tag = QLabel(f"{serial}  ·  鸿蒙(hdc 快照)")
        tag.setStyleSheet("color:#9aa4b2;font-size:11px;")
        cv.addWidget(tag)
        label = QLabel("鸿蒙投屏（快照轮询，约 2fps）")
        label.setAlignment(Qt.AlignCenter)
        label.setStyleSheet("background:#10131a;color:#6b7280;")
        label.setScaledContents(True)
        cv.addWidget(label, stretch=1)
        entry = {
            "proc": None, "title": title, "hwnd": None, "host": None,
            "cell": cell, "tries": 0, "embed": False, "transport": "hdc",
            "tool": "snapshot", "label": label,
        }
        self.mirrors[serial] = entry
        self._relayout()
        self.log_signal.emit(f"▶ 启动鸿蒙快照预览: {serial}")
        timer = QTimer(self)
        timer.timeout.connect(lambda: self._poll_hdc_frame(serial))
        timer.start(500)
        entry["poll"] = timer

    def _poll_hdc_frame(self, serial):
        e = self.mirrors.get(serial)
        if not e or self._closing or e.get("transport") != "hdc":
            return
        try:
            data = urllib.request.urlopen(BASE + f"/frame/{serial}", timeout=2).read()
            if data:
                img = QImage.fromData(data)
                if not img.isNull():
                    e["label"].setPixmap(QPixmap.fromImage(img))
        except Exception:
            pass

    def _try_embed(self, serial):
        entry = self.mirrors.get(serial)
        if not entry or not entry.get("embed", True):
            return
        hwnd = find_window(entry["title"])
        if hwnd:
            entry["hwnd"] = hwnd
            x, y, w, h = self._embed_rect(entry)
            embed_window(hwnd, int(self.video_area.winId()), x, y, w, h)
            self._restart_count.pop(serial, None)  # 稳定嵌入，清零重连计数
            self.log_signal.emit(f"✅ scrcpy 已嵌入: {serial} (rect {x},{y} {w}x{h})")
            return
        entry["tries"] += 1
        if entry["tries"] < 30:
            QTimer.singleShot(300, lambda: self._try_embed(serial))
        else:
            self.log_signal.emit(f"⚠️ 未能嵌入 {serial}，超时（可勾选『独立窗口』模式）")

    def _embed_rect(self, entry):
        """计算某设备 scrcpy 窗口应处的矩形（相对 video_area 的物理像素）。"""
        host = entry["host"]
        tl = host.mapTo(self.video_area, QPoint(0, 0))
        ratio = self.video_area.devicePixelRatioF() or 1.0
        return (int(tl.x() * ratio), int(tl.y() * ratio),
                int(host.width() * ratio), int(host.height() * ratio))

    def _relayout(self):
        """设备增减后刷新可见性并重新分配区域。"""
        n = len(self.mirrors)
        self.placeholder.setVisible(n == 0)
        self._apply_geometry()
        # Qt 布局落地有延迟，稍后再补一次
        QTimer.singleShot(60, self._apply_geometry)

    def _apply_geometry(self):
        """按手机竖屏比例把可用区均匀分给每台设备，格子在其槽位内居中。"""
        area = self.video_area
        aw, ah = area.width(), area.height()
        n = len(self.mirrors)
        if n == 0:
            self.placeholder.setGeometry(0, 0, max(aw, 1), max(ah, 1))
            return
        aspect = 0.5  # 目标格子宽高比（手机竖屏近似 9:18）
        # 枚举列数，取"每格面积最大"的方案（并列时取列数更小者）
        best_cols, best_rows, best_cw = 1, 1, 0.0
        for cols in range(1, n + 1):
            rows = math.ceil(n / cols)
            cw = min(aw / cols, (ah / rows) * aspect)
            if cw > best_cw:
                best_cw, best_cols, best_rows = cw, cols, rows
        ch = best_cw / aspect
        slot_w, slot_h = aw / best_cols, ah / best_rows
        for i, e in enumerate(self.mirrors.values()):
            if e.get("cell") is None:
                continue
            sx, sy = (i % best_cols) * slot_w, (i // best_cols) * slot_h
            x = sx + (slot_w - best_cw) / 2
            y = sy + (slot_h - ch) / 2
            e["cell"].setGeometry(QRect(int(x), int(y), int(best_cw), int(ch)))
            # 手动 setGeometry 的子控件不在 layout 管理下，父窗口 show() 不会级联显示
            e["cell"].show()
        self._resize_all()

    def _resize_all(self):
        parent = int(self.video_area.winId())
        for e in self.mirrors.values():
            if e["hwnd"]:
                x, y, w, h = self._embed_rect(e)
                move_embedded(e["hwnd"], x, y, w, h)

    def _remove_mirror(self, serial):
        e = self.mirrors.pop(serial, None)
        if not e:
            return
        poll = e.get("poll")
        if poll is not None:
            try:
                poll.stop()
            except Exception:
                pass
        hc = e.get("hos_client")
        if hc is not None:
            try:
                hc.stop()
            except Exception:
                pass
        if e["proc"] is not None:
            self._kill_tree(e["proc"])
        f = e.get("errfile")
        if f is not None:
            try:
                f.close()
            except Exception:
                pass
        if e["cell"] is not None:
            e["cell"].hide()
            e["cell"].deleteLater()
        self._relayout()

    @staticmethod
    def _scrcpy_err_tail(serial, n=4):
        """读取该设备 scrcpy 日志的最后几行，用于定位异常退出原因。"""
        path = os.path.join(PROJECT, ".tools", f"scrcpy_{serial}.log")
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                lines = [l.strip() for l in f if l.strip()]
            return " | ".join(lines[-n:])
        except Exception:
            return ""

    def stop_mirror(self, serial=None, silent=False):
        if serial is None:
            serial = self._current_serial()
        if serial is None:
            return
        e = self.mirrors.get(serial)
        if e is not None:
            e["user_stop"] = True
        self._remove_mirror(serial)
        if not silent:
            self.log_signal.emit(f"■ 已停止投屏: {serial}")

    def stop_all(self):
        for serial in list(self.mirrors.keys()):
            self.mirrors[serial]["user_stop"] = True
            self._remove_mirror(serial)
        self.log_signal.emit("■ 已停止全部投屏")

    # ---- 进程树清理（无窗口启动 + 退出释放）----
    @staticmethod
    def _kill_tree(proc):
        """杀掉进程及其子进程树（scrcpy/adb、powershell/dart 等）。"""
        if proc is None:
            return
        try:
            if proc.poll() is None and sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
        except Exception:
            pass
        try:
            proc.kill()
        except Exception:
            pass

    def _kill_backend(self):
        self._kill_tree(self._backend_proc)
        self._backend_proc = None
        if self._backend_logf is not None:
            try:
                self._backend_logf.close()
            except Exception:
                pass
            self._backend_logf = None

    def _cleanup(self):
        """主程序退出：停止全部投屏、杀掉后端进程树、关闭日志、停定时器。"""
        if self._closing:
            return
        self._closing = True
        try:
            self.dev_timer.stop()
        except Exception:
            pass
        self.stop_all()
        self._kill_backend()

    def closeEvent(self, event):
        self._cleanup()
        super().closeEvent(event)

    def _watch_embed(self):
        for serial, e in list(self.mirrors.items()):
            # 自适应：按最新格子矩形重新摆位（自愈能力）
            if e["hwnd"]:
                x, y, w, h = self._embed_rect(e)
                move_embedded(e["hwnd"], x, y, w, h)
            # 检测 scrcpy 是否已退出
            if e["proc"] is not None and e["proc"].poll() is not None:
                tail = self._scrcpy_err_tail(serial)
                # 非用户主动停止 → 自动重连（最多 3 次），应对瞬时断连
                if (not e.get("user_stop") and self._restart_count.get(serial, 0) < 3
                        and not self._closing):
                    self._restart_count[serial] = self._restart_count.get(serial, 0) + 1
                    self.log_signal.emit(
                        f"ℹ️ scrcpy 退出，自动重连 {serial} "
                        f"({self._restart_count[serial]}/3)" + (f" ｜ {tail}" if tail else ""))
                    self._remove_mirror(serial)
                    QTimer.singleShot(2500, lambda s=serial: self.start_mirror(s))
                    continue
                self._restart_count.pop(serial, None)
                self.log_signal.emit(f"ℹ️ scrcpy 已退出: {serial}" + (f" ｜ {tail}" if tail else ""))
                self._remove_mirror(serial)

    # ---- 按键 ----
    def send_key(self, code, hoskey=None):
        serial = self._current_serial()
        if not serial:
            return
        if self._dev_transport.get(serial) == "hdc":
            if hoskey is None:
                self.log_signal.emit("ℹ️ 鸿蒙无『任务键』等价 keyEvent，请用底部上滑手势（GUI 拖拽）")
                return
            # 鸿蒙：uitest keyEvent 认 Back/Home/Power 字符串，不认 Android 数字 keycode
            self._hos_input(serial, ["uitest", "uiInput", "keyEvent", hoskey],
                            f"按键 {hoskey}")
            return
        threading.Thread(target=self._send_key, args=(serial, code), daemon=True).start()

    def _send_key(self, serial, code):
        try:
            r = http_json("POST", f"/api/device/{serial}/key", {"code": code})
            self.log_signal.emit(f"🔘 key {code} -> {r}")
        except Exception as e:
            self.log_signal.emit(f"⚠️ key {code} 失败: {e}")

    # ---- 一键解锁 ----
    def unlock(self):
        serial = self._current_serial()
        if not serial:
            QMessageBox.warning(self, "提示", "请先选择一台在线设备")
            return
        pwd = self.un_pwd.text().strip()
        if not pwd:
            QMessageBox.warning(self, "提示", "请输入锁屏密码")
            return
        swipe = self.un_swipe.isChecked()
        threading.Thread(target=self._unlock, args=(serial, pwd, swipe), daemon=True).start()

    def _unlock(self, serial, pwd, swipe):
        try:
            r = http_json("POST", f"/api/device/{serial}/unlock",
                          {"password": pwd, "swipeUp": swipe})
            self.log_signal.emit(f"🔓 解锁 -> {r}")
        except Exception as e:
            self.log_signal.emit(f"⚠️ 解锁失败: {e}")

    # ---- 自动滑屏 ----
    def load_autoswipe(self):
        threading.Thread(target=self._load_autoswipe, daemon=True).start()

    def _load_autoswipe(self):
        try:
            cfg = http_json("GET", "/api/autoswipe")
            if isinstance(cfg, dict):
                self.autoswipe_signal.emit(cfg)
        except Exception:
            pass

    def _populate_autoswipe(self, cfg):
        self.as_enabled.blockSignals(True)
        self.as_enabled.setChecked(bool(cfg.get("enabled")))
        self.as_enabled.blockSignals(False)
        d = cfg.get("direction", "up")
        idx = self.as_dir.findText(d)
        if idx >= 0:
            self.as_dir.setCurrentIndex(idx)
        if cfg.get("custom"):
            self.as_min.setValue(int(cfg.get("minMs", 1000)))
            self.as_max.setValue(int(cfg.get("maxMs", 3000)))

    def _on_as_toggled(self, state):
        self.apply_autoswipe()

    def apply_autoswipe(self):
        serial = self._current_serial()
        if not serial:
            return
        cfg = {
            "enabled": self.as_enabled.isChecked(),
            "direction": self.as_dir.currentText(),
            "rangeKey": "custom",
            "custom": True,
            "minMs": self.as_min.value(),
            "maxMs": self.as_max.value(),
        }
        threading.Thread(target=self._apply_autoswipe, args=(cfg,), daemon=True).start()

    def _apply_autoswipe(self, cfg):
        try:
            r = http_json("POST", "/api/autoswipe", cfg)
            self.log_signal.emit(f"🔁 自动滑屏 -> {r}")
        except Exception as e:
            self.log_signal.emit(f"⚠️ 自动滑屏失败: {e}")

    # ---- 快手极速版打卡（纯免费，运行 tools/ks_quick_checkin.py） ----
    def start_ks_checkin(self, dry=False):
        if self.ks_running:
            self.log_signal.emit("ℹ️ 快手打卡已在运行中")
            return
        serial = self._current_serial()
        if not serial:
            QMessageBox.warning(self, "提示", "请先选择一台在线设备")
            return
        script = os.path.join(PROJECT, "tools", "ks_quick_checkin.py")
        if not os.path.isfile(script):
            QMessageBox.critical(self, "错误", f"找不到脚本:\n{script}")
            return
        cmd = [sys.executable, script, "--serial", serial]
        if dry:
            cmd += ["--rounds", "1", "--dry-run"]
        else:
            cmd += ["--rounds", "20"]
        self.ks_running = True
        self.ks_box.setExpanded(True)
        self.ks_start.setEnabled(False)
        self.ks_dry.setEnabled(False)
        self.ks_stop.setEnabled(True)
        self.log_signal.emit(f"▶ 启动快手打卡{'（测试1个）' if dry else ''}: {serial}")
        threading.Thread(target=self._run_ks_checkin, args=(cmd,), daemon=True).start()

    def _run_ks_checkin(self, cmd):
        proc = None
        try:
            # 强制子进程用 UTF-8 输出，避免 Windows 默认 GBK 导致客户端日志中文乱码
            env = dict(os.environ)
            env["PYTHONIOENCODING"] = "utf-8"
            proc = subprocess.Popen(
                cmd, cwd=PROJECT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                creationflags=CREATE_NO_WINDOW,
            )
            self.ks_proc = proc
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    self.log_signal.emit(f"  {line}")
            rc = proc.wait()
            self.log_signal.emit(f"■ 快手打卡结束 (exit={rc})")
        except Exception as e:
            self.log_signal.emit(f"⚠️ 快手打卡失败: {e}")
        finally:
            self.ks_proc = None
            self.ks_running = False
            # 用 Qt 信号安全地恢复按钮状态（跨线程不能直接改 QWidget）
            QTimer.singleShot(0, self._ks_buttons_idle)

    def _ks_buttons_idle(self):
        self.ks_start.setEnabled(True)
        self.ks_dry.setEnabled(True)
        self.ks_stop.setEnabled(False)

    def stop_ks_checkin(self):
        proc = self.ks_proc
        if proc is None or proc.poll() is not None:
            self.log_signal.emit("ℹ️ 当前没有运行中的快手打卡")
            return
        try:
            proc.terminate()
            self.log_signal.emit("■ 已请求停止快手打卡")
        except Exception as e:
            self.log_signal.emit(f"⚠️ 停止失败: {e}")

    # ---- 快手极速版连续看视频攒金币（tools/ks_video_coins.py） ----
    def start_ks_video(self, dry=False):
        if self.kv_running:
            self.log_signal.emit("ℹ️ 看视频攒金币已在运行中")
            return
        serial = self._current_serial()
        if not serial:
            QMessageBox.warning(self, "提示", "请先选择一台在线设备")
            return
        script = os.path.join(PROJECT, "tools", "ks_video_coins.py")
        if not os.path.isfile(script):
            QMessageBox.critical(self, "错误", f"找不到脚本:\n{script}")
            return
        cmd = [sys.executable, script, "--serial", serial]
        if dry:
            cmd += ["--rounds", "1", "--dry-run"]
        else:
            cmd += ["--rounds", "10"]
        self.kv_running = True
        self.kv_box.setExpanded(True)
        self.kv_start.setEnabled(False)
        self.kv_dry.setEnabled(False)
        self.kv_stop.setEnabled(True)
        self.log_signal.emit(f"▶ 启动看视频攒金币{'（测试1轮）' if dry else ''}: {serial}")
        threading.Thread(target=self._run_ks_video, args=(cmd,), daemon=True).start()

    def _run_ks_video(self, cmd):
        proc = None
        try:
            env = dict(os.environ)
            env["PYTHONIOENCODING"] = "utf-8"
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                env=env, text=True, encoding="utf-8", errors="replace",
                creationflags=CREATE_NO_WINDOW,
            )
            self.kv_proc = proc
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    self.log_signal.emit(f"  {line}")
            rc = proc.wait()
            self.log_signal.emit(f"■ 看视频攒金币结束 (exit={rc})")
        except Exception as e:
            self.log_signal.emit(f"⚠️ 看视频攒金币失败: {e}")
        finally:
            self.kv_proc = None
            self.kv_running = False
            QTimer.singleShot(0, self._kv_buttons_idle)

    def _kv_buttons_idle(self):
        self.kv_start.setEnabled(True)
        self.kv_dry.setEnabled(True)
        self.kv_stop.setEnabled(False)

    def stop_ks_video(self):
        proc = self.kv_proc
        if proc is None or proc.poll() is not None:
            self.log_signal.emit("ℹ️ 当前没有运行中的看视频攒金币")
            return
        try:
            proc.terminate()
            self.log_signal.emit("■ 已请求停止看视频攒金币")
        except Exception as e:
            self.log_signal.emit(f"⚠️ 停止失败: {e}")

    # ---- 抖音极速版连续看视频攒金币（tools/dy_video_coins.py） ----
    def start_dy_video(self, dry=False):
        if self.dy_running:
            self.log_signal.emit("ℹ️ 抖音看视频攒金币已在运行中")
            return
        serial = self._current_serial()
        if not serial:
            QMessageBox.warning(self, "提示", "请先选择一台在线设备")
            return
        script = os.path.join(PROJECT, "tools", "dy_video_coins.py")
        if not os.path.isfile(script):
            QMessageBox.critical(self, "错误", f"找不到脚本:\n{script}")
            return
        cmd = [sys.executable, script, "--serial", serial]
        if dry:
            cmd += ["--rounds", "1", "--dry-run"]
        else:
            cmd += ["--rounds", "10"]
        self.dy_running = True
        self.dy_box.setExpanded(True)
        self.dy_start.setEnabled(False)
        self.dy_dry.setEnabled(False)
        self.dy_stop.setEnabled(True)
        self.log_signal.emit(f"▶ 启动抖音看视频攒金币{'（测试1轮）' if dry else ''}: {serial}")
        threading.Thread(target=self._run_dy_video, args=(cmd,), daemon=True).start()

    def _run_dy_video(self, cmd):
        proc = None
        try:
            env = dict(os.environ)
            env["PYTHONIOENCODING"] = "utf-8"
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                env=env, text=True, encoding="utf-8", errors="replace",
                creationflags=CREATE_NO_WINDOW,
            )
            self.dy_proc = proc
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    self.log_signal.emit(f"  {line}")
            rc = proc.wait()
            self.log_signal.emit(f"■ 抖音看视频攒金币结束 (exit={rc})")
        except Exception as e:
            self.log_signal.emit(f"⚠️ 抖音看视频攒金币失败: {e}")
        finally:
            self.dy_proc = None
            self.dy_running = False
            QTimer.singleShot(0, self._dy_buttons_idle)

    def _dy_buttons_idle(self):
        self.dy_start.setEnabled(True)
        self.dy_dry.setEnabled(True)
        self.dy_stop.setEnabled(False)

    def stop_dy_video(self):
        proc = self.dy_proc
        if proc is None or proc.poll() is not None:
            self.log_signal.emit("ℹ️ 当前没有运行中的抖音看视频攒金币")
            return
        try:
            proc.terminate()
            self.log_signal.emit("■ 已请求停止抖音看视频攒金币")
        except Exception as e:
            self.log_signal.emit(f"⚠️ 停止失败: {e}")

    # ---- 快手 365 天打卡 6 子任务（tools/ks_daily_tasks.py，纯免费） ----
    def start_kd_task(self, dry=False):
        if self.kd_running:
            self.log_signal.emit("ℹ️ 打卡子任务已在运行中")
            return
        serial = self._current_serial()
        if not serial:
            QMessageBox.warning(self, "提示", "请先选择一台在线设备")
            return
        script = os.path.join(PROJECT, "tools", "ks_daily_tasks.py")
        if not os.path.isfile(script):
            QMessageBox.critical(self, "错误", f"找不到脚本:\n{script}")
            return
        task = self.kd_task.currentData() or "signin"
        cmd = [sys.executable, script, "--task", task, "--serial", serial]
        if dry:
            cmd += ["--dry-run", "--force", "--watch", "12"]
        self.kd_running = True
        self.kd_box.setExpanded(True)
        self.kd_start.setEnabled(False)
        self.kd_dry.setEnabled(False)
        self.kd_stop.setEnabled(True)
        self.log_signal.emit(f"▶ 启动打卡子任务 {task}{'（测试1次，12秒，不写幂等）' if dry else ''}: {serial}")
        threading.Thread(target=self._run_kd_task, args=(cmd,), daemon=True).start()

    def _run_kd_task(self, cmd):
        proc = None
        try:
            env = dict(os.environ)
            env["PYTHONIOENCODING"] = "utf-8"
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                env=env, text=True, encoding="utf-8", errors="replace",
                creationflags=CREATE_NO_WINDOW,
            )
            self.kd_proc = proc
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    self.log_signal.emit(f"  {line}")
            rc = proc.wait()
            self.log_signal.emit(f"■ 打卡子任务结束 (exit={rc})")
        except Exception as e:
            self.log_signal.emit(f"⚠️ 打卡子任务失败: {e}")
        finally:
            self.kd_proc = None
            self.kd_running = False
            QTimer.singleShot(0, self._kd_buttons_idle)

    def _kd_buttons_idle(self):
        self.kd_start.setEnabled(True)
        self.kd_dry.setEnabled(True)
        self.kd_stop.setEnabled(False)

    def stop_kd_task(self):
        proc = self.kd_proc
        if proc is None or proc.poll() is not None:
            self.log_signal.emit("ℹ️ 当前没有运行中的打卡子任务")
            return
        try:
            proc.terminate()
            self.log_signal.emit("■ 已请求停止打卡子任务")
        except Exception as e:
            self.log_signal.emit(f"⚠️ 停止失败: {e}")

    # ---- 日志 ----
    def _append_log(self, msg):
        ts = time.strftime("%H:%M:%S")
        self.log.append(f"[{ts}] {msg}")

    # ---- 尺寸 ----
    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._apply_geometry()


def main():
    if hasattr(Qt, "AA_EnableHighDpiScaling"):
        QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
