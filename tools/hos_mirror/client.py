"""自建鸿蒙投屏接收端（开发调试用，不随产品分发）。

链路：本模块 -> 启动 Sidecar.java（调用华为 hosScrcpy API）-> 设备 uittest 推 H.264
      -> sidecar 本机 TCP 转发裸 H.264 -> 本模块 PyAV 解码 -> BGR 帧回调/显示。

依赖：PyAV(av)、numpy、Java(JBR)、hosScrcpy-1.0.18-beta.jar、hdc.exe。
hosScrcpy.jar 版权归华为，仅供个人学习研究，勿随商业产品分发。

用法：
  python tools/hos_mirror/client.py <serial> --preview 5     # 自检：显示5秒
  from client import HosMirrorClient  # 库模式：on_frame(bgr) 回调
"""
from __future__ import annotations

import argparse
import os
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
HERE = Path(__file__).resolve().parent
SIDECAR_JAVA = HERE / "Sidecar.java"
JAR = ROOT / ".tools/.downloads/star-cast/resources/hos/hosScrcpy-1.0.18-beta.jar"
JAVA = Path(r"D:\HuaWei\DevEco-Studio\jbr\bin\java.exe")
HDC = Path(r"D:\HuaWei\Sdk\26.0.0\toolchains\hdc.exe")
FIRST_FRAME_TIMEOUT = 25   # TCP 连上后等待首个 H.264 帧的秒数；超时判定通道未就绪并重建


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class HosMirrorClient:
    def __init__(self, serial: str, on_frame=None, on_status=None,
                 java=None, hdc=None, jar=None, fps=30, scale=1):
        self.serial = serial
        self.on_frame = on_frame          # callback(bgr_ndarray, width, height)
        self.on_status = on_status        # callback(str)
        self.java = str(java or JAVA)
        self.hdc = str(hdc or HDC)
        self.jar = str(jar or JAR)
        self.fps = fps
        self.scale = scale
        self.proc = None
        self.sock = None
        self.thread = None
        self.running = False
        self._log = None
        self._got_frame = False

    def _cleanup_stale_uitest(self):
        """清理设备端残留 uitest daemon。

        uitest start-daemon 是 singleness 模式：上次投屏残留的 daemon（尤其带
        --extension-name libscreen_casting.z.so 的）会占死通道，导致新会话
        createChannel UNAVAILABLE / start scrcpy fail，永不推 H.264 -> 黑屏。
        启动 sidecar 前 kill 掉，让华为 API 重新拉起干净的采集通道。
        """
        try:
            out = subprocess.run(
                [self.hdc, "-t", self.serial, "shell", "ps -ef"],
                capture_output=True, text=True, timeout=6,
                creationflags=subprocess.CREATE_NO_WINDOW,
            ).stdout or ""
        except Exception:
            return
        pids = []
        for line in out.splitlines():
            if "uitest start-daemon" not in line or "grep" in line or "sh -c" in line:
                continue
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                pids.append(parts[1])
        if not pids:
            return
        try:
            subprocess.run(
                [self.hdc, "-t", self.serial, "shell", "kill -9 " + " ".join(pids)],
                capture_output=True, text=True, timeout=6,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            # 等旧 daemon 完全退出再启动新会话，否则华为 API 可能又复用到正在死的通道
            time.sleep(1.5)
            self._status(f"已清理设备残留投屏服务 ({len(pids)})")
        except Exception:
            pass

    def _status(self, msg):
        if self.on_status:
            try:
                self.on_status(msg)
            except Exception:
                pass

    def start(self):
        if self.running:
            return True
        for name, p in (("java", self.java), ("hdc", self.hdc), ("jar", self.jar)):
            if not os.path.exists(p):
                self._status(f"[ERR] 缺少 {name}: {p}")
                return False
        self._got_frame = False
        self._cleanup_stale_uitest()
        port = _free_port()
        self.port = port
        cmd = [self.java, "-cp", self.jar, str(SIDECAR_JAVA),
               self.serial, self.hdc, str(port), str(self.fps), str(self.scale)]
        logpath = ROOT / ".tools" / f"hos_sidecar_{self.serial}.log"
        logpath.parent.mkdir(parents=True, exist_ok=True)
        self._log = open(logpath, "w", encoding="utf-8", errors="replace")
        self._status(f"启动鸿蒙 sidecar (port {port})")
        try:
            self.proc = subprocess.Popen(
                cmd, stdout=self._log, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except Exception as e:
            self._status(f"[ERR] 启动 sidecar 失败: {e}")
            return False
        self.running = True
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()
        return True

    def _connect(self, timeout=2):
        """连 sidecar TCP；sidecar 就绪前重试。返回 socket 或 None。"""
        deadline = time.time() + timeout
        while time.time() < deadline and self.running:
            try:
                return socket.create_connection(("127.0.0.1", self.port), timeout=2)
            except OSError:
                if self.proc and self.proc.poll() is not None:
                    return None   # sidecar 进程已退出，别再等
                time.sleep(0.3)
        return None

    def _restart_sidecar(self):
        """TCP 断开后 sidecar 内部 gRPC 已废，必须整进程重启才能恢复。返回是否成功。"""
        # 停旧进程
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=3)
            except Exception:
                try: self.proc.kill()
                except Exception: pass
        self.proc = None
        try:
            if self._log and not self._log.closed:
                self._log.close()
        except Exception:
            pass
        # 新端口 + 新进程（先清残留 daemon，否则新会话仍复用死通道）
        self._cleanup_stale_uitest()
        self.port = _free_port()
        logpath = ROOT / ".tools" / f"hos_sidecar_{self.serial}.log"
        self._log = open(logpath, "w", encoding="utf-8", errors="replace")
        cmd = [self.java, "-cp", self.jar, str(SIDECAR_JAVA),
               self.serial, self.hdc, str(self.port), str(self.fps), str(self.scale)]
        try:
            self.proc = subprocess.Popen(
                cmd, stdout=self._log, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            return True
        except Exception:
            return False

    def _loop(self):
        s = None
        retries = 0
        try:
            import av  # PyAV
            while self.running:
                codec = av.CodecContext.create("h264", "r")
                s = self._connect(20 if retries == 0 else 3)
                if s is None:
                    if retries == 0:
                        self._status("[ERR] 连不上 sidecar TCP")
                    break
                s.settimeout(10)
                self.sock = s
                self._got_frame = False
                if retries == 0:
                    self._status("已连接，等待设备推送画面…")
                else:
                    self._status(f"重连成功（第{retries}次），等待画面…")
                    # 不清零：累计断开次数达 3 次后停止自动重连，避免无限循环
                threading.Thread(target=self._watch_first_frame, args=(s,), daemon=True).start()
                disconnected = False
                while self.running:
                    try:
                        hdr = self._recv_exact(s, 4)
                        if not hdr:
                            disconnected = True; break
                        n = struct.unpack(">I", hdr)[0]
                        if n <= 0 or n > 30_000_000:
                            self._status(f"[WARN] 异常帧长 {n}")
                            disconnected = True; break
                        data = self._recv_exact(s, n)
                        if not data:
                            disconnected = True; break
                        # 每个 TCP 消息 = 一个完整 AU，整块喂 PyAV
                        self._decode_chunk(codec, data)
                    except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, OSError):
                        disconnected = True; break
                try: s.close()
                except Exception: pass
                s = None
                self.sock = None
                if not self.running:
                    break
                if not disconnected:
                    break
                # 断线：重启整个 sidecar（gRPC 已废），带退避，最多3次
                retries += 1
                if retries > 3:
                    self._status("[ERR] 视频流多次断开，已停止自动重连")
                    break
                self._status(f"视频流断开，重启投屏服务重连 ({retries}/3)…")
                time.sleep(min(2 * retries, 5))
                if not self._restart_sidecar():
                    self._status("[ERR] 重启投屏服务失败")
                    break
        except Exception as e:
            if self.running:
                self._status(f"[ERR] 视频流异常: {type(e).__name__}: {e}")
        finally:
            try:
                if s: s.close()
            except Exception:
                pass
            self.sock = None

    def _watch_first_frame(self, s):
        """TCP 连上 ≠ 有画面。若超时仍无 H.264 帧，关 socket 触发重建，避免永久假解码。"""
        t0 = time.time()
        while self.running and not self._got_frame:
            try:
                if s.fileno() < 0:
                    return
            except Exception:
                return
            if time.time() - t0 > FIRST_FRAME_TIMEOUT:
                self._status("视频通道迟迟无画面，重建投屏服务…")
                # Windows 下 close() 不唤醒阻塞的 recv，先 shutdown 让读线程立即抛错走重建
                try: s.shutdown(socket.SHUT_RDWR)
                except Exception: pass
                try: s.close()
                except Exception: pass
                return
            time.sleep(0.5)

    def _recv_exact(self, s, n):
        out = b""
        while len(out) < n and self.running:
            try:
                c = s.recv(n - len(out))
            except socket.timeout:
                continue
            if not c:
                return b""
            out += c
        return out

    def _decode_chunk(self, codec, chunk):
        try:
            packets = codec.parse(chunk)
        except Exception:
            return
        for pkt in packets:
            try:
                frames = codec.decode(pkt)
            except Exception:
                continue
            for frame in frames:
                if not self.running:
                    return
                try:
                    bgr = frame.to_ndarray(format="bgr24")
                except Exception:
                    continue
                if not self._got_frame:
                    self._got_frame = True
                    self._status("视频通道就绪，开始显示")
                if self.on_frame:
                    try:
                        self.on_frame(bgr)
                    except Exception:
                        pass

    def stop(self):
        self.running = False
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=4)
            except Exception:
                try: self.proc.kill()
                except Exception: pass
        if self._log:
            try: self._log.close()
            except Exception: pass
        self.proc = None
        self.thread = None


def _split_annexb(buf: bytes):
    """把一个字节串按 Annex-B 起始码切成若干完整 NAL/AU；末尾残留返回给调用方。
    约定每个 TCP 消息是一个完整 AU（首字节即 00 00 00 01），通常直接单块解析。
    """
    marker = b"\x00\x00\x00\x01"
    out = []
    starts = []
    i = 0
    while True:
        j = buf.find(marker, i)
        if j < 0:
            break
        starts.append(j)
        i = j + 4
    if not starts:
        return [], buf
    for k in range(len(starts) - 1):
        out.append(buf[starts[k]:starts[k+1]])
    return out, buf[starts[-1]:]


def preview(serial: str, seconds: float):
    import cv2
    holder = {"frame": None}
    def on_frame(bgr):
        holder["frame"] = bgr
    def on_status(m):
        print(m, flush=True)
    c = HosMirrorClient(serial, on_frame=on_frame, on_status=on_status)
    if not c.start():
        return
    t0 = time.time()
    n = 0
    try:
        while time.time() - t0 < seconds:
            f = holder["frame"]
            if f is not None:
                cv2.imshow("HOS Mirror (dev)", f)
                n += 1
                holder["frame"] = None
            if cv2.waitKey(1) & 0xFF == 27:
                break
    finally:
        c.stop()
        cv2.destroyAllWindows()
    print(f"preview done, frames shown={n}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="鸿蒙自建投屏接收端（开发调试用）")
    ap.add_argument("serial", help="hdc 设备序列号")
    ap.add_argument("--preview", type=float, default=0, help="显示N秒自检（cv2窗口）")
    args = ap.parse_args()
    if args.preview > 0:
        preview(args.serial, args.preview)
    else:
        print("使用 --preview N 进行自检，或在代码中 import HosMirrorClient")
