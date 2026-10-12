#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""抖音极速版金币增长监控 + 自动上滑。

用法（先让 adb 进 PATH，或传 --adb）：
    . D:\ai\prj\trae\HuaWei\MyGameSim\scripts\use-env.ps1 | Out-Null
    python tools/coin_watch.py

核心逻辑：每隔 interval 秒对金币挂件区域截屏，比较当前帧与上一帧的灰度像素差。
差分 > threshold 视为金币在增长；连续 <= threshold 达到 --stable 秒且之前出现过增长，
则执行上滑切换到下一个视频。
"""

import argparse
import io
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional, Tuple

from PIL import Image

# Windows 下被管道/重定向时仍输出可读中文
if sys.version_info >= (3, 7) and sys.stdout is not None and hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(
        sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True
    )
if sys.version_info >= (3, 7) and sys.stderr is not None and hasattr(sys.stderr, "buffer"):
    sys.stderr = io.TextIOWrapper(
        sys.stderr.buffer, encoding="utf-8", errors="replace", line_buffering=True
    )

# 默认设备：EBG-AN00（技能中记录的序列号）
DEFAULT_SERIAL = "2NSDU20423005421"

# 默认金币挂件 ROI（相对坐标 x,y,w,h），基于 EBG-AN00 1080x2340 竖屏抖音极速版截图校准
DEFAULT_ROI = (0.02, 0.10, 0.18, 0.12)


def find_adb(prefer: Optional[str] = None) -> str:
    """解析 adb 可执行文件路径。"""
    if prefer:
        return prefer
    # 1) ANDROID_HOME
    home = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
    if home:
        p = os.path.join(home, "platform-tools", "adb.exe")
        if os.path.exists(p):
            return p
    # 2) PATH
    p = shutil.which("adb")
    if p:
        return p
    # 3) MyGameSim 共享 toolchain（常用）
    p = r"D:\ai\prj\trae\HuaWei\MyGameSim\.tools\android-sdk\platform-tools\adb.exe"
    if os.path.exists(p):
        return p
    raise FileNotFoundError(
        "找不到 adb.exe。请先用 '. \\scripts\\use-env.ps1' 注入环境，"
        "或传 --adb 指定完整路径。"
    )


def run_adb(adb: str, serial: str, args: list[str]) -> str:
    cmd = [adb, "-s", serial, *args]
    try:
        return subprocess.check_output(cmd, stderr=subprocess.STDOUT, timeout=30).decode("utf-8", errors="ignore")
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"adb failed: {' '.join(cmd)}\n{e.output.decode('utf-8', errors='ignore')}") from e


def screenshot(adb: str, serial: str) -> Image.Image:
    """截屏并返回 PIL Image。使用 exec-out 二进制捕获（Python 下安全）。"""
    cmd = [adb, "-s", serial, "exec-out", "screencap", "-p"]
    data = subprocess.check_output(cmd, timeout=15)
    if not data or data[:4] != b"\x89PNG":
        raise RuntimeError("截屏数据异常，adb 连接可能断开")
    return Image.open(io.BytesIO(data))


def get_resolution(adb: str, serial: str) -> Tuple[int, int]:
    out = run_adb(adb, serial, ["shell", "wm", "size"])
    m = re.search(r"(\d+)x(\d+)", out)
    if not m:
        raise RuntimeError(f"无法解析分辨率: {out}")
    return int(m.group(1)), int(m.group(2))


def roi_to_pixels(roi_rel: Tuple[float, float, float, float], w: int, h: int) -> Tuple[int, int, int, int]:
    x = int(roi_rel[0] * w)
    y = int(roi_rel[1] * h)
    rw = int(roi_rel[2] * w)
    rh = int(roi_rel[3] * h)
    return x, y, rw, rh


def frame_diff(a: Image.Image, b: Image.Image) -> float:
    """返回两帧 ROI 的灰度平均绝对差分（0-255）。"""
    if a.size != b.size:
        b = b.resize(a.size)
    a_gray = a.convert("L")
    b_gray = b.convert("L")
    # 转 numpy-free 计算：用 PIL ImageChops 的差值统计
    from PIL import ImageChops
    diff = ImageChops.difference(a_gray, b_gray)
    hist = diff.histogram()
    total = sum(i * c for i, c in enumerate(hist))
    count = sum(hist)
    return total / count if count else 0.0


def swipe_up(adb: str, serial: str, w: int, h: int, duration_ms: int = 300) -> None:
    """在视频区域执行一次上滑（切下一个视频）。"""
    x1 = int(0.5 * w)
    y1 = int(0.78 * h)
    x2 = int(0.5 * w)
    y2 = int(0.22 * h)
    run_adb(adb, serial, ["shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2), str(duration_ms)])


def main() -> None:
    parser = argparse.ArgumentParser(description="抖音极速版金币增长监控 + 自动上滑")
    parser.add_argument("--adb", help="adb.exe 完整路径（不填则自动解析）")
    parser.add_argument("--serial", default=DEFAULT_SERIAL, help=f"设备序列号（默认 {DEFAULT_SERIAL}）")
    parser.add_argument("--roi", nargs=4, type=float, default=list(DEFAULT_ROI),
                        metavar=("X", "Y", "W", "H"),
                        help=f"金币区域相对坐标 x y w h（默认 {' '.join(map(str, DEFAULT_ROI))}）")
    parser.add_argument("--threshold", type=float, default=5.0,
                        help="像素差分阈值，超过视为金币在变化（默认 5.0，按 live diff 调）")
    parser.add_argument("--stable", type=float, default=5.0,
                        help="金币停止增长多少秒后上滑（默认 5.0）")
    parser.add_argument("--cooldown", type=float, default=3.0,
                        help="每次滑动后冷却秒数，避免转场动画误判（默认 3.0）")
    parser.add_argument("--interval", type=float, default=0.5,
                        help="截屏采样间隔秒数（默认 0.5）")
    parser.add_argument("--rounds", type=int, default=0,
                        help="最大上滑次数，0=无限（默认 0）")
    parser.add_argument("--max-watch", type=float, default=0,
                        help="单视频最长观看秒数，超过强制上滑（0=禁用，默认 0）")
    parser.add_argument("--calibrate", action="store_true",
                        help="只截一帧并保存 ROI 裁剪图，用于校准坐标")
    parser.add_argument("--debug", action="store_true",
                        help="把每次 ROI 裁剪保存到临时目录")
    args = parser.parse_args()

    adb = find_adb(args.adb)
    serial = args.serial
    roi_rel = tuple(args.roi)

    print(f"[adb] {adb}")
    print(f"[device] {serial}")

    # 连接检查
    run_adb(adb, serial, ["wait-for-device"])
    w, h = get_resolution(adb, serial)
    print(f"[resolution] {w}x{h}")
    x, y, rw, rh = roi_to_pixels(roi_rel, w, h)
    print(f"[roi] rel={roi_rel} abs=({x},{y},{rw},{rh})")

    if args.calibrate:
        img = screenshot(adb, serial)
        crop = img.crop((x, y, x + rw, y + rh))
        out = os.path.join(os.environ.get("TEMP", "."), "coin_watch_calibrate.png")
        crop.save(out)
        print(f"[calibrate] ROI saved: {out}")
        return

    debug_dir: Optional[Path] = None
    if args.debug:
        debug_dir = Path(os.environ.get("TEMP", ".")) / "coin_watch_debug"
        debug_dir.mkdir(exist_ok=True)
        print(f"[debug] saving crops to {debug_dir}")

    print(f"[monitor] threshold={args.threshold} stable={args.stable}s interval={args.interval}s")
    print("[hint] 按 Ctrl+C 停止\n")

    prev_crop: Optional[Image.Image] = None
    saw_growth = False
    stable_start: Optional[float] = None
    cooldown_until = time.time() + args.cooldown
    watch_start: Optional[float] = None
    swipes = 0
    frame_idx = 0

    try:
        while True:
            now = time.time()
            try:
                img = screenshot(adb, serial)
            except Exception as e:
                print(f"[!] 截屏失败: {e}")
                time.sleep(args.interval)
                continue

            crop = img.crop((x, y, x + rw, y + rh))
            if debug_dir:
                crop.save(debug_dir / f"{frame_idx:05d}.png")
            frame_idx += 1

            if prev_crop is None:
                prev_crop = crop
                cooldown_until = now + args.cooldown
                watch_start = None
                continue

            diff = frame_diff(prev_crop, crop)
            prev_crop = crop

            # 冷却期：更新 prev，但不触发滑动；冷却结束时重置状态
            if now < cooldown_until:
                if now >= cooldown_until - args.cooldown + 0.1:
                    print(f"[cooldown] diff={diff:6.2f}")
                continue
            else:
                if stable_start is None:
                    # 冷却刚结束：重置状态
                    saw_growth = False
                    stable_start = now
                    watch_start = now

            if diff > args.threshold:
                saw_growth = True
                stable_start = None
                print(f"[growth] diff={diff:6.2f} 金币在增长")
            else:
                if stable_start is None:
                    stable_start = now
                stable_for = now - stable_start
                status = f"stable={stable_for:.1f}s"
                if saw_growth:
                    status += " (saw growth)"
                print(f"[stable] diff={diff:6.2f} {status}")
                if saw_growth and stable_for >= args.stable:
                    swipes += 1
                    print(f"\n[swipe #{swipes}] 金币停止增长 {args.stable}s，执行上滑\n")
                    swipe_up(adb, serial, w, h)
                    if args.rounds > 0 and swipes >= args.rounds:
                        print("[done] 达到设定轮数")
                        break
                    # 重置状态机
                    prev_crop = None
                    saw_growth = False
                    stable_start = None
                    cooldown_until = time.time() + args.cooldown
                    watch_start = None
                    continue

            # 单视频最大观看时间兜底
            if args.max_watch > 0 and watch_start is not None and (now - watch_start) >= args.max_watch:
                swipes += 1
                print(f"\n[swipe #{swipes}] 观看达到 {args.max_watch}s 上限，强制上滑\n")
                swipe_up(adb, serial, w, h)
                if args.rounds > 0 and swipes >= args.rounds:
                    print("[done] 达到设定轮数")
                    break
                prev_crop = None
                saw_growth = False
                stable_start = None
                cooldown_until = time.time() + args.cooldown
                watch_start = None
                continue

            time.sleep(args.interval)
    except KeyboardInterrupt:
        print(f"\n[stop] 共滑动 {swipes} 次")


if __name__ == "__main__":
    main()
