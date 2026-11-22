#!/usr/bin/env python3
"""抖音极速版「连续看视频30天赚30元」—— 看视频攒金币，红包待领取后免费领取。

真机流程（EBG-AN00 实测，2026-11-21）：
  首页底部「赚钱」→ 赚钱任务页，顶部卡片「连续看视频30天赚30元」
    ├─ 未攒够：红包显示「N/目标 解锁中」+「去看视频」按钮
    └─ 攒够后：红包内出现「领取」小按钮，大按钮变「立即领取」
  「去看视频」→ 推荐信息流视频，看视频攒金币（回页面下拉刷新更新进度）
  攒够 → 点红包内的「领取」小按钮（★不是右上角「立即领取」——那是引导去逛街任务）
  → 弹「恭喜获得看视频奖励 +N金币」→ 点「开心收下」（★绝不点「看广告再赚」）

关键实测结论：
  - 抖音任务进度回页面即部分刷新，但金币余额/红包状态需下拉刷新才更新
  - 「立即领取」大按钮是引导到「逛街得金币」浏览90秒任务，不是领红包，切勿点
  - 真正领取入口是红包卡片内的小「领取」按钮，模板 templates/dy_packet_claim_btn.png
    实测待领态 score 0.94~1.0，未攒够/已领 0.485，阈值 0.8 可靠区分

安全：全程不涉及支付；不点「看广告再赚」；每步校验前台；识别失败零点击退出。
坐标基于 EBG-AN00 1080x2340。
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
PKG = "com.ss.android.ugc.aweme.lite"
STATE = ROOT / ".tools" / "dy_video_coins_state.json"
SHOTS = ROOT / ".tools" / "dy_video_shots"
TPL_PACKET_CLAIM = ROOT / "templates" / "dy_packet_claim_btn.png"

# 赚钱任务页顶部卡片
BTN_WATCH = (902, 762)       # 「去看视频」大按钮
PACKET_CLAIM = (167, 1005)   # 红包内「领取」小按钮（★真正的领取入口）
# 「开心收下」按钮由 find_happy_accept() 双模板动态定位（新旧机型布局不同，不能硬编码）
REWARD_DIALOG_TPL = ROOT / "templates" / "dy_reward_dialog.png"

MATCH_TH = 0.80


def log(msg):
    print(f"[dy-video] {msg}", flush=True)


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


def ensure_douyin(serial):
    """确保前台是抖音极速版且停在首页。"""
    fg = foreground(serial)
    if not fg.startswith(PKG):
        log(f"前台非抖音极速版（{fg}），自动启动...")
        adb(serial, "shell", "monkey", "-p", PKG, "1", capture=False)
        for _ in range(10):
            time.sleep(1)
            if foreground(serial).startswith(PKG):
                break
        if not foreground(serial).startswith(PKG):
            log("[ERR] 无法启动抖音极速版，零点击退出")
            return False
        time.sleep(2)
    return True


def tpl_score(tpl, img_path):
    try:
        import cv2
        r = cv2.matchTemplate(cv2.imread(str(img_path)), cv2.imread(str(tpl)), cv2.TM_CCOEFF_NORMED)
        return cv2.minMaxLoc(r)[1]
    except Exception as e:
        log(f"[WARN] 模板匹配失败: {e}")
        return 0.0


def is_reward_dialog(img):
    """「恭喜获得看视频奖励」弹窗。"""
    if not REWARD_DIALOG_TPL.exists():
        return False
    p = save_shot(img, "_chk_dialog.png")
    return tpl_score(REWARD_DIALOG_TPL, p) >= 0.75


def is_earn_page(img):
    """赚钱任务页：顶部粉色金币/现金 banner（y≈0.11~0.25）。"""
    w, h = img.width, img.height
    band = img.crop((int(0.05*w), int(0.11*h), int(0.95*w), int(0.25*h)))
    px = list(band.getdata())
    red = sum(1 for r,g,b in px if r > 180 and g < 140 and b < 160 and r > g+60)
    return red/len(px) > 0.3


def find_dialog_x(img):
    """模板匹配弹窗右上角 ×，返回中心坐标；找不到返回 None。"""
    tpl = ROOT / "templates" / "dy_dialog_x.png"
    if not tpl.exists():
        return None
    p = save_shot(img, "_find_x.png")
    try:
        import cv2
        t = cv2.imread(str(tpl))
        r = cv2.matchTemplate(cv2.imread(str(p)), t, cv2.TM_CCOEFF_NORMED)
        _, mx, _, loc = cv2.minMaxLoc(r)
        if mx < 0.75:
            return None
        th, tw = t.shape[:2]
        return (loc[0] + tw//2, loc[1] + th//2)
    except Exception:
        return None


def close_reward_if_any(serial, img):
    """若当前是奖励弹窗则关闭它，返回最新截图。"""
    if is_reward_dialog(img):
        log("关闭遗留的奖励弹窗")
        pos = find_dialog_x(img)
        if pos:
            tap_xy(serial, pos[0], pos[1], "弹窗×")
        else:
            # 兜底：× 在弹窗右上角区域，用相对坐标
            tap_xy(serial, int(img.width * 0.80), int(img.height * 0.35), "弹窗×(兜底)")
        time.sleep(1.5)
        return screenshot(serial)
    return img


def goto_earn_top(serial):
    """确保在赚钱任务页顶部。返回是否成功。"""
    if not ensure_douyin(serial):
        return False
    img = screenshot(serial)
    img = close_reward_if_any(serial, img)
    H = img.height  # 基准 2340，按实际屏高缩放
    sy = lambda v: int(v * H / 2340)
    cx = img.width // 2
    # 不在赚钱页：点底部「赚钱」tab
    if not is_earn_page(img):
        tap_xy(serial, cx, sy(2250), "赚钱tab")
        time.sleep(2)
        img = screenshot(serial)
    # 下拉刷新 + 回到顶部
    for _ in range(2):
        swipe(serial, cx, sy(700), cx, sy(1800), 400)
        time.sleep(0.8)
    for _ in range(8):
        swipe(serial, cx, sy(500), cx, sy(2000), 300)
        time.sleep(0.25)
    img = screenshot(serial)
    img = close_reward_if_any(serial, img)
    return is_earn_page(img)


def packet_claimable(serial):
    """检测红包内是否出现「领取」小按钮（待领取）。返回 bool。"""
    img = screenshot(serial)
    p = save_shot(img, "card_state.png")
    if not TPL_PACKET_CLAIM.exists():
        log("[ERR] 红包领取模板缺失")
        return None
    sc = tpl_score(TPL_PACKET_CLAIM, p)
    log(f"红包领取按钮检测 score={sc:.3f} (阈值 {MATCH_TH})")
    return sc >= MATCH_TH


def watch_videos(serial, seconds):
    """推荐流刷视频指定秒数，每 ~15 秒上滑切一个。"""
    log(f"刷视频 {seconds}s 攒金币...")
    img = screenshot(serial)
    H = img.height
    sy = lambda v: int(v * H / 2340)
    cx = img.width // 2
    end = time.time() + seconds
    idx = 0
    while time.time() < end:
        time.sleep(15)
        if time.time() >= end:
            break
        idx += 1
        swipe(serial, cx, sy(1800), cx, sy(500), 300)
        log(f"上滑切第 {idx} 个视频")
    return idx


def find_happy_accept(img):
    """双模板匹配「开心收下」按钮（新机红色实心 / 旧机白底红边），返回中心坐标或 None。

    不同机型/状态下弹窗布局不同（新机 1080x2400 只有一个红按钮，
    旧机 1080x2340 有「看广告再赚」+「开心收下」两个），硬编码坐标会点空导致卡住。
    """
    p = save_shot(img, "_find_accept.png")
    best = (-1, None, None)
    for name, path in (("new", ROOT / "templates" / "dy_happy_accept.png"),
                       ("old", ROOT / "templates" / "dy_happy_accept_old.png")):
        if not path.exists():
            continue
        try:
            import cv2
            tpl = cv2.imread(str(path))
            screen = cv2.imread(str(p))
            r = cv2.matchTemplate(screen, tpl, cv2.TM_CCOEFF_NORMED)
            _, mx, _, loc = cv2.minMaxLoc(r)
            log(f"开心收下模板 {name} score={mx:.3f}")
            if mx > best[0]:
                th, tw = tpl.shape[:2]
                best = (mx, (loc[0] + tw//2, loc[1] + th//2), name)
        except Exception as e:
            log(f"[WARN] 开心收下匹配失败: {e}")
    if best[0] < 0.75 or best[1] is None:
        return None
    log(f"定位开心收下: {best[1]} (模板 {best[2]})")
    return best[1]


def claim_reward(serial):
    """点红包「领取」→ 奖励弹窗 →「开心收下」。返回是否领取成功。"""
    tap_xy(serial, PACKET_CLAIM[0], PACKET_CLAIM[1], "红包领取")
    time.sleep(2)
    im = screenshot(serial)
    save_shot(im, "reward_dialog.png")
    if not is_reward_dialog(im):
        log("[WARN] 未出现奖励弹窗，检查 reward_dialog.png")
        return False
    log("奖励弹窗出现 -> 开心收下（不看广告）")
    pos = find_happy_accept(im)
    if pos is None:
        # 兜底：用相对弹窗中心的比例坐标
        log("[WARN] 未定位到开心收下按钮，尝试比例坐标兜底")
        pos = (int(im.width * 0.5), int(im.height * 0.62))
    tap_xy(serial, pos[0], pos[1], "开心收下")
    time.sleep(2)
    return True


def main():
    ap = argparse.ArgumentParser(description="抖音连续看视频赚金币（免费，红包待领取后开心收下）")
    ap.add_argument("--serial", default="2NSDU20423005421")
    ap.add_argument("--rounds", type=int, default=10, help="最多刷几轮视频")
    ap.add_argument("--watch-seconds", type=int, default=90, help="每轮刷视频秒数")
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

    if not ensure_douyin(serial=args.serial):
        sys.exit(3)

    claimed = 0
    for i in range(max(1, args.rounds)):
        if not goto_earn_top(args.serial):
            log("[ERR] 无法进入赚钱任务页顶部，零点击退出")
            sys.exit(4)

        can_claim = packet_claimable(args.serial)
        if can_claim is None:
            sys.exit(5)
        if can_claim:
            log("检测到红包待领取，执行领取")
            if claim_reward(args.serial):
                claimed += 1
                log(f"已领取第 {claimed} 个红包")
                continue
            log("[WARN] 领取失败，停止")
            break

        # 未攒够：点「去看视频」进入推荐流
        tap_xy(args.serial, BTN_WATCH[0], BTN_WATCH[1], "去看视频")
        time.sleep(2)
        watch_videos(args.serial, args.watch_seconds)
        d = random.uniform(args.delay_min, args.delay_max)
        log(f"随机等待 {d:.1f}s 后回赚钱页刷新")
        time.sleep(d)

    # 收尾再检查一次
    if goto_earn_top(args.serial):
        if packet_claimable(args.serial):
            if claim_reward(args.serial):
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
