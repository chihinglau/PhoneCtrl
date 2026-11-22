#!/usr/bin/env python3
"""快手「连续看视频7天 0.76元必到账」—— 看视频攒金币，红包待领取后一键领取。

真机流程（EBG-AN00 实测，2026-11-21）：
  任务中心顶部粉色卡片「连续看视频7天 0.76元必到账」
    ├─ 未攒够：「已攒N/目标金币，继续看视频可开启」+「去看视频」按钮
    └─ 攒够后：「N金币待领取，继续看视频赚更多」+「一键领取」按钮
  点「去看视频」→ 发现页信息流视频，左上角红包进度环随观看时长增长
  → 看满若干视频 → 回任务中心下拉刷新 → 进度更新 → 有待领取红包
  → 点「一键领取」→ 弹「任务完成奖励 +N金币」→ 点「继续赚钱」关闭

关键实测结论：
  - 视频页左上角红包进度环是实时进度，切视频会重置为当前视频观看进度；
  - 任务中心的「已攒N/目标」是缓存，**必须下拉刷新**才更新；
  - 攒够后第一个红包变「待领取」，第二个变「解锁中」，依次推进；
  - 卡片状态用整行模板匹配区分（watch vs claim，区分度 1.0 vs 0.695）。

安全：不涉及任何支付；每步校验前台；找不到任务中心/卡片时零点击退出。
坐标基于 EBG-AN00 1080x2340 归一化。
"""
import argparse
import io
import random
import subprocess
import sys
import time
import warnings
from datetime import date
from pathlib import Path

from PIL import Image

warnings.filterwarnings("ignore", message=".*getdata.*", category=DeprecationWarning)

ROOT = Path(__file__).resolve().parent.parent
ADB = ROOT / ".tools" / "android-sdk" / "platform-tools" / "adb.exe"
PKG = "com.kuaishou.nebula"
HOME_ACTIVITY = "com.kuaishou.nebula/com.yxcorp.gifshow.HomeActivity"
STATE = ROOT / ".tools" / "ks_video_coins_state.json"
SHOTS = ROOT / ".tools" / "ks_video_shots"
TPL_WATCH = ROOT / "templates" / "ks_video_row_watch.png"
TPL_CLAIM = ROOT / "templates" / "ks_video_row_claim.png"

# 卡片按钮（去看视频 / 一键领取 位置相同）
CARD_BTN = (880, 1032)
# 领取成功弹窗「继续赚钱」按钮
CLAIM_DONE_BTN = (540, 1560)
CLAIM_DONE_CLOSE = (980, 645)

MATCH_TH = 0.80  # 数字变化会使整行模板略降；配合 claim>watch 相对比较防误判


def log(msg):
    print(f"[ks-video] {msg}", flush=True)


def adb(serial, *args, capture=True):
    return subprocess.run([str(ADB), "-s", serial, *args], capture_output=capture)


def screenshot(serial):
    r = adb(serial, "exec-out", "screencap", "-p")
    if r.returncode != 0 or len(r.stdout) < 1000:
        raise RuntimeError(f"screencap failed (rc={r.returncode}, bytes={len(r.stdout)})")
    return Image.open(io.BytesIO(r.stdout)).convert("RGB")


def tap_xy(serial, x, y, what):
    adb(serial, "shell", "input", "tap", str(int(x)), str(int(y)), capture=False)
    log(f"tap {what} -> ({int(x)},{int(y)})")


def swipe(serial, x1, y1, x2, y2, ms=350):
    adb(serial, "shell", "input", "swipe", str(int(x1)), str(int(y1)),
        str(int(x2)), str(int(y2)), str(int(ms)), capture=False)


def save_shot(img, name):
    SHOTS.mkdir(parents=True, exist_ok=True)
    img.save(SHOTS / name)
    return SHOTS / name


def json_load(p):
    import json
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def json_save(p, data):
    import json
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def foreground(serial):
    r = adb(serial, "shell", "dumpsys", "activity", "activities")
    for line in r.stdout.decode("utf-8", "replace").splitlines():
        if "mResumedActivity" in line or "topResumedActivity" in line:
            for tok in line.split():
                if "/" in tok and not tok.startswith("{"):
                    return tok
    return ""


def ensure_kuaishou(serial):
    fg = foreground(serial)
    if not fg.startswith(PKG):
        log(f"前台非快手（{fg}），自动启动...")
        adb(serial, "shell", "monkey", "-p", PKG, "1", capture=False)
        for _ in range(8):
            time.sleep(1)
            if foreground(serial).startswith(PKG):
                break
        if not foreground(serial).startswith(PKG):
            log("[ERR] 无法启动快手，零点击退出")
            return False
    return True


def is_earn_page(img):
    """任务中心：中上部粉色金币/现金 banner（y≈0.12~0.24）。"""
    w, h = img.width, img.height
    band = img.crop((int(0.05*w), int(0.12*h), int(0.95*w), int(0.24*h)))
    px = list(band.getdata())
    red = sum(1 for r,g,b in px if r > 180 and g < 140 and b < 160 and r > g+60)
    return red/len(px) > 0.3


def is_claim_dialog(img):
    """「任务完成奖励 +N金币」弹窗：下半部橙色金币堆 + 粉色按钮。"""
    w, h = img.width, img.height
    box = img.crop((int(0.28*w), int(0.32*h), int(0.72*w), int(0.48*h)))
    px = list(box.getdata())
    orange = sum(1 for r,g,b in px if r > 200 and 110 < g < 200 and b < 120)
    btn = img.crop((int(0.30*w), int(0.63*h), int(0.70*w), int(0.72*h)))
    bp = list(btn.getdata())
    pink = sum(1 for r,g,b in bp if r > 200 and 40 < g < 150 and 70 < b < 180)
    return orange/len(px) > 0.03 and pink/len(bp) > 0.4


def tpl_score(tpl, img):
    try:
        import cv2
        r = cv2.matchTemplate(cv2.imread(str(img)), cv2.imread(str(tpl)), cv2.TM_CCOEFF_NORMED)
        return cv2.minMaxLoc(r)[1]
    except Exception as e:
        log(f"[WARN] 模板匹配失败: {e}")
        return 0.0


def goto_earn_top(serial):
    """确保在任务中心并上滑到顶部。返回是否成功。"""
    if not foreground(serial).startswith(PKG):
        if not ensure_kuaishou(serial):
            return False
    img = screenshot(serial)
    # 先关掉可能遗留的领取奖励弹窗（× 在右上角）
    if is_claim_dialog(img):
        log("关闭遗留的领取奖励弹窗")
        tap_xy(serial, CLAIM_DONE_CLOSE[0], CLAIM_DONE_CLOSE[1], "弹窗×")
        time.sleep(1.5)
        img = screenshot(serial)
    # 不在任务中心：回首页再去赚钱
    if not is_earn_page(img):
        adb(serial, "shell", "am", "start", "-n", HOME_ACTIVITY, capture=False)
        time.sleep(2)
        if not foreground(serial).startswith(PKG):
            log("[ERR] 无法停在快手首页")
            return False
        img = screenshot(serial)
        if is_claim_dialog(img):
            tap_xy(serial, CLAIM_DONE_CLOSE[0], CLAIM_DONE_CLOSE[1], "弹窗×")
            time.sleep(1.5)
            img = screenshot(serial)
        if not is_earn_page(img):
            tap_xy(serial, 760, 2280, "去赚钱")
            time.sleep(2)
    # 上滑回顶部
    for _ in range(10):
        swipe(serial, 540, 500, 540, 2000, 300)
        time.sleep(0.25)
    img = screenshot(serial)
    if is_claim_dialog(img):
        tap_xy(serial, CLAIM_DONE_CLOSE[0], CLAIM_DONE_CLOSE[1], "弹窗×")
        time.sleep(1.5)
        img = screenshot(serial)
    return is_earn_page(img)


def card_state(serial):
    """返回 'claim'（有待领取红包）/ 'watch'（未攒够）/ None。"""
    img = screenshot(serial)
    save_shot(img, "card_state.png")
    if not TPL_CLAIM.exists() or not TPL_WATCH.exists():
        log("[ERR] 卡片模板缺失")
        return None
    sc = tpl_score(TPL_CLAIM, str(SHOTS / "card_state.png"))
    sw = tpl_score(TPL_WATCH, str(SHOTS / "card_state.png"))
    log(f"卡片状态: claim={sc:.3f} watch={sw:.3f} (阈值 {MATCH_TH})")
    if sc >= MATCH_TH and sc > sw:
        return "claim"
    if sw >= MATCH_TH:
        return "watch"
    return None


def watch_videos(serial, seconds):
    """发现页刷视频指定秒数，每 ~18 秒上滑切一个。"""
    log(f"刷视频 {seconds}s 攒金币...")
    end = time.time() + seconds
    idx = 0
    while time.time() < end:
        time.sleep(18)
        if time.time() >= end:
            break
        idx += 1
        swipe(serial, 540, 1800, 540, 500, 300)
        log(f"上滑切第 {idx} 个视频")
    return idx


def claim_one(serial):
    """点「一键领取」并处理奖励弹窗。返回是否领取成功。"""
    tap_xy(serial, CARD_BTN[0], CARD_BTN[1], "一键领取")
    time.sleep(2)
    im = screenshot(serial)
    save_shot(im, "claim_dialog.png")
    if is_claim_dialog(im):
        log("领取成功：任务完成奖励")
        # 点「继续赚钱」关闭
        tap_xy(serial, CLAIM_DONE_BTN[0], CLAIM_DONE_BTN[1], "继续赚钱")
        time.sleep(1.5)
        return True
    log("[WARN] 未检测到领取奖励弹窗，检查 claim_dialog.png")
    return False


def main():
    ap = argparse.ArgumentParser(description="快手连续看视频攒金币（免费，红包待领取后一键领取）")
    ap.add_argument("--serial", default="2NSDU20423005421")
    ap.add_argument("--rounds", type=int, default=10, help="最多刷几轮视频（每轮后回任务中心刷新检查）")
    ap.add_argument("--watch-seconds", type=int, default=60, help="每轮刷视频秒数")
    ap.add_argument("--delay-min", type=float, default=3.0, help="轮间随机延时下限")
    ap.add_argument("--delay-max", type=float, default=8.0, help="轮间随机延时上限")
    ap.add_argument("--force", action="store_true", help="忽略当日幂等")
    ap.add_argument("--dry-run", action="store_true", help="测试模式：不写幂等记录")
    args = ap.parse_args()
    if args.delay_min > args.delay_max:
        args.delay_min, args.delay_max = args.delay_max, args.delay_min

    if not ADB.exists():
        log(f"[ERR] adb not found: {ADB}")
        sys.exit(1)

    state = json_load(STATE)
    today = date.today().isoformat()
    key = f"{args.serial}|{today}"
    if not args.force and state.get(key, {}).get("ok"):
        log(f"[SKIP] {today} 已完成（幂等），--force 强制重跑")
        return

    if not ensure_kuaishou(args.serial):
        sys.exit(3)

    claimed = 0
    for i in range(max(1, args.rounds)):
        if not goto_earn_top(args.serial):
            log("[ERR] 无法进入任务中心顶部，零点击退出")
            sys.exit(4)

        st = card_state(args.serial)
        if st == "claim":
            log("检测到红包待领取，执行一键领取")
            if claim_one(args.serial):
                claimed += 1
                log(f"已领取第 {claimed} 个红包")
                # 领取后按钮可能变回「去看视频」或下一个红包，继续下一轮检查
                continue
            log("[WARN] 领取失败，停止")
            break
        elif st == "watch":
            log("红包未攒够，进入看视频")
        else:
            log("[ERR] 无法识别卡片状态，零点击退出（不猜点）")
            sys.exit(5)

        # 点「去看视频」进入发现页
        tap_xy(args.serial, CARD_BTN[0], CARD_BTN[1], "去看视频")
        time.sleep(2)
        watch_videos(args.serial, args.watch_seconds)

        d = random.uniform(args.delay_min, args.delay_max)
        log(f"随机等待 {d:.1f}s 后回任务中心刷新")
        time.sleep(d)

    # 最终再检查一次是否有待领取
    if goto_earn_top(args.serial):
        if card_state(args.serial) == "claim":
            if claim_one(args.serial):
                claimed += 1
                log(f"收尾领取第 {claimed} 个红包")
    final = screenshot(args.serial)
    save_shot(final, "final.png")

    ok = claimed > 0
    log(f"[OK] 本轮共领取 {claimed} 个红包" if ok else "[WARN] 本轮未领取到红包，检查截图")
    if args.dry_run:
        log("[DONE] 测试模式：跳过写入幂等记录")
        return
    state[key] = {"ok": ok, "date": today, "serial": args.serial,
                  "ts": time.time(), "claimed": claimed}
    json_save(STATE, state)
    log("[DONE] 已写当日记录" if ok else "[DONE] 写入未成功记录（下次可重试）")


if __name__ == "__main__":
    main()
