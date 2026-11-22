#!/usr/bin/env python3
"""快手「连续打卡白拿新款手机」每日打卡 —— 纯免费，绝不支付金币。

真实流程（EBG-AN00 实测，2026-11-21）：
  快手首页 → 底部「去赚钱」→ 任务中心（卡片位置不固定，需滚动+模板匹配定位）
  →「连续打卡白拿新款手机」行的「去领取」
  →「完成365天打卡任务 白拿好礼」WebView 页
  →「今日打卡任务 5/6」里唯一未完成的「观看广告 (N/20)」→「去观看」
  → 弹窗二选一：① 观看N个广告完成任务（免费）；② 消耗金币跳过（**绝不点**）
  → 只选免费项 →「去看广告」→ 看够时长 →「继续观看」→ 返回打卡页
  → 广告计满 20/20、6/6 完成 → 弹「打卡成功 看完广告领取惊喜奖励」→「立即领取」
  → 奖励广告 →「已成功领取奖励 ×」→ 关闭

关键：卡片在任务中心的位置每天不同（完成/优先级变化会移动），
      用 cv2 模板匹配（templates/ks_checkin_row.png）自动定位，找不到就停止，绝不乱点。

安全：不点击任何支付按钮；每步校验前台；找不到目标卡片/页面时零点击退出。
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
STATE = ROOT / ".tools" / "ks_checkin_state.json"
SHOTS = ROOT / ".tools" / "ks_shots"
TEMPLATE = ROOT / "templates" / "ks_checkin_row.png"

P = {
    "earn_tab":       (760 / 1080, 2280 / 2340),  # 底部「去赚钱」
    "watch_ad":       (910 / 1080,  970 / 2340),  # 打卡页「去观看」
    "free_option":    (270 / 540,   745 / 1170),  # 弹窗「观看N个广告完成任务」整行
    "go_ad":          (270 / 540,  1045 / 1170),  # 选中免费项后的底部动作按钮
    "option_close":   (502 / 540,   505 / 1170),  # 弹窗右上 ×
    "daily_claim":    (540 / 1080, 1500 / 2340),  # 「打卡成功」弹窗的「立即领取」
    "reward_close":   (238 / 540,   105 / 1170),  # 广告左上角 ×
    "keep_watching":  (540 / 1080, 1290 / 2340),  # 「再看N秒领取奖励」→「继续观看」
    "bonus_close":    (975 / 1080,  477 / 2340),  # 任务中心「翻倍任务开启」弹窗 ×
}

AD_WAIT = 45      # 单条广告最长等待秒数
AD_MIN = 15       # 最短观看秒数


def log(msg):
    print(f"[ks-checkin] {msg}", flush=True)


def adb(serial, *args, capture=True):
    return subprocess.run([str(ADB), "-s", serial, *args], capture_output=capture)


def screenshot(serial):
    r = adb(serial, "exec-out", "screencap", "-p")
    if r.returncode != 0 or len(r.stdout) < 1000:
        raise RuntimeError(f"screencap failed (rc={r.returncode}, bytes={len(r.stdout)})")
    return Image.open(io.BytesIO(r.stdout)).convert("RGB")


def pt(img, key):
    nx, ny = P[key]
    return int(nx * img.width), int(ny * img.height)


def tap(serial, img, key):
    x, y = pt(img, key)
    adb(serial, "shell", "input", "tap", str(x), str(y), capture=False)
    log(f"tap {key} -> ({x},{y})")


def tap_xy(serial, x, y, what):
    adb(serial, "shell", "input", "tap", str(x), str(y), capture=False)
    log(f"tap {what} -> ({x},{y})")


def back(serial):
    adb(serial, "shell", "input", "keyevent", "4", capture=False)


def save_shot(img, name):
    SHOTS.mkdir(parents=True, exist_ok=True)
    p = SHOTS / name
    img.save(p)
    return p


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
    """确保前台是快手且停在首页；不是则自动启动。返回是否成功。"""
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
    # 强制回首页（不用 BACK，避免退出 App 点到桌面）
    adb(serial, "shell", "am", "start", "-n", HOME_ACTIVITY, capture=False)
    time.sleep(2)
    ok = foreground(serial).startswith(PKG)
    if not ok:
        log("[ERR] 无法停在快手首页，零点击退出")
    return ok


def is_earn_page(img):
    w, h = img.width, img.height
    band = img.crop((int(0.05*w), int(0.02*h), int(0.95*w), int(0.12*h)))
    px = list(band.getdata())
    red = sum(1 for r,g,b in px if r > 180 and g < 140 and b < 160 and r > g+60)
    band2 = img.crop((int(0.05*w), int(0.18*h), int(0.95*w), int(0.30*h)))
    px2 = list(band2.getdata())
    pink = sum(1 for r,g,b in px2 if r > 190 and 60 < g < 170 and 80 < b < 190 and r > g)
    return (red/len(px) > 0.3) or (pink/len(px2) > 0.2)


def is_bonus_dialog(img):
    w, h = img.width, img.height
    box = img.crop((int(0.2*w), int(0.52*h), int(0.8*w), int(0.68*h)))
    px = list(box.getdata())
    red = sum(1 for r,g,b in px if r > 180 and g < 90 and b < 120)
    return red/len(px) > 0.08


def is_checkin_page(img):
    """365天打卡页：下半部青绿色日历格子。"""
    w, h = img.width, img.height
    band = img.crop((int(0.05*w), int(0.72*h), int(0.95*w), int(0.98*h)))
    px = list(band.getdata())
    teal = sum(1 for r,g,b in px if g > 150 and 60 < r < 190 and 90 < b < 210 and g > r+20)
    return teal/len(px) > 0.25


def is_ad_option_dialog(img):
    """「今日广告任务 / 可任选一方式完成任务」黄色横幅弹窗。"""
    w, h = img.width, img.height
    band = img.crop((int(0.05*w), int(0.42*h), int(0.95*w), int(0.52*h)))
    px = list(band.getdata())
    yellow = sum(1 for r,g,b in px if r > 205 and g > 165 and b < 130 and r > b+80)
    return yellow/len(px) > 0.05


def is_free_option_selected(img):
    """免费项（上方一排）右侧单选圈是否变绿勾。"""
    w, h = img.width, img.height
    box = img.crop((int(0.76*w), int(0.60*h), int(0.86*w), int(0.68*h)))
    px = list(box.getdata())
    green = sum(1 for r,g,b in px if g > 140 and g > r+35 and g > b+35)
    return green/len(px) > 0.02


def is_paid_option_selected(img):
    """付费项「消耗N金币」右侧单选圈是否仍为红勾。"""
    w, h = img.width, img.height
    box = img.crop((int(0.76*w), int(0.73*h), int(0.86*w), int(0.80*h)))
    px = list(box.getdata())
    red = sum(1 for r,g,b in px if r > 185 and g < 110 and b < 135 and r > g+80)
    return red/len(px) > 0.02


def is_daily_claim_dialog(img):
    """「打卡成功 看完广告领取惊喜奖励」弹窗。"""
    w, h = img.width, img.height
    yellow = img.crop((int(0.25*w), int(0.48*h), int(0.75*w), int(0.59*h)))
    yp = list(yellow.getdata())
    y_n = sum(1 for r,g,b in yp if r > 200 and g > 175 and b < 200 and r > b+30)
    dark = img.crop((int(0.25*w), int(0.60*h), int(0.75*w), int(0.70*h)))
    dp = list(dark.getdata())
    d_n = sum(1 for r,g,b in dp if r < 60 and g < 70 and b < 80)
    hook = img.crop((int(0.45*w), int(0.28*h), int(0.75*w), int(0.40*h)))
    hp = list(hook.getdata())
    g_n = sum(1 for r,g,b in hp if g > 150 and r < 180 and b < 180 and g > r+30)
    return (y_n/len(yp) > 0.15) and (d_n/len(dp) > 0.4) and (g_n/len(hp) > 0.02)


def is_ad_reward(img):
    """广告结束「已成功领取奖励」黄勾横幅。"""
    w, h = img.width, img.height
    box = img.crop((0, int(0.04*h), int(0.52*w), int(0.15*h)))
    px = list(box.getdata())
    light = sum(1 for r,g,b in px if r > 195 and g > 185 and 110 < b < 205 and r >= g >= b)
    orange = sum(1 for r,g,b in px if r > 190 and 110 < g < 190 and b < 120)
    return (light/len(px) > 0.08) or (orange/len(px) > 0.004)


def is_keep_watching(img):
    """「再看N秒领取奖励」粉色「继续观看」按钮。"""
    w, h = img.width, img.height
    box = img.crop((int(0.30*w), int(0.52*h), int(0.70*w), int(0.58*h)))
    px = list(box.getdata())
    pink = sum(1 for r,g,b in px if r > 200 and 40 < g < 140 and 80 < b < 180)
    return pink/len(px) > 0.5


def find_claim_button(img):
    """cv2 模板匹配定位「连续打卡白拿新款手机」行的「去领取」。"""
    if not TEMPLATE.exists():
        log(f"[WARN] 模板不存在: {TEMPLATE}")
        return None
    try:
        import cv2
        import numpy as np
        tmpl = cv2.imread(str(TEMPLATE))
        screen = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
        if tmpl is None:
            return None
        base = img.width / 540.0
        best_val, best_loc, best_scale = -1, None, base
        for adj in (1.0, 0.95, 1.05, 0.9, 1.1):
            s = base * adj
            t = cv2.resize(tmpl, None, fx=s, fy=s)
            if t.shape[0] > screen.shape[0] or t.shape[1] > screen.shape[1]:
                continue
            res = cv2.matchTemplate(screen, t, cv2.TM_CCOEFF_NORMED)
            _, mx, _, loc = cv2.minMaxLoc(res)
            if mx > best_val:
                best_val, best_loc, best_scale = mx, loc, s
        log(f"模板匹配 score={best_val:.3f}")
        if best_val < 0.70:
            return None
        th, tw = tmpl.shape[:2]
        tw, th = int(tw*best_scale), int(th*best_scale)
        return (best_loc[0] + int(tw*0.88), best_loc[1] + int(th*0.15))
    except Exception as e:
        log(f"[WARN] 模板匹配失败: {e}")
        return None


def goto_earn(serial):
    for attempt in range(4):
        if not foreground(serial).startswith(PKG):
            adb(serial, "shell", "monkey", "-p", PKG, "1", capture=False)
            time.sleep(2)
        adb(serial, "shell", "am", "start", "-n", HOME_ACTIVITY, capture=False)
        time.sleep(2)
        if not foreground(serial).startswith(PKG):
            return None
        img = screenshot(serial)
        if is_bonus_dialog(img):
            tap(serial, img, "bonus_close")
            time.sleep(1)
            img = screenshot(serial)
        if is_earn_page(img):
            return img
        tap(serial, img, "earn_tab")
        time.sleep(2)
        img = screenshot(serial)
        if is_bonus_dialog(img):
            tap(serial, img, "bonus_close")
            time.sleep(1)
            img = screenshot(serial)
        if is_earn_page(img):
            return img
    return None


def scroll_find_claim(serial, max_scroll=15):
    """滚动查找目标卡片，返回按钮坐标或 None（不猜点其它任务）。"""
    for i in range(max_scroll + 1):
        img = screenshot(serial)
        if is_bonus_dialog(img):
            tap(serial, img, "bonus_close")
            time.sleep(1)
            continue
        btn = find_claim_button(img)
        if btn:
            save_shot(img, "01_claim_found.png")
            log(f"找到目标卡片，去领取按钮 {btn}")
            return btn
        if i < max_scroll:
            w, h = img.width, img.height
            adb(serial, "shell", "input", "swipe",
                str(int(w*0.5)), str(int(h*0.85)),
                str(int(w*0.5)), str(int(h*0.25)), "350", capture=False)
            time.sleep(0.9)
    return None


def open_checkin_page(serial):
    """从任意界面进入 365 天打卡页。已在则复用。"""
    img0 = screenshot(serial)
    save_shot(img0, "00_start.png")
    if is_checkin_page(img0) or is_daily_claim_dialog(img0):
        log("已在打卡页，复用")
        return True
    if goto_earn(serial) is None:
        log("[ERR] 无法进入任务中心，零点击退出")
        return False
    btn = scroll_find_claim(serial)
    if btn is None:
        log("[ERR] 未找到「连续打卡白拿新款手机」卡片（今日可能已完成），零点击退出")
        return False
    x, y = btn
    tap_xy(serial, x, y, "去领取")
    for _ in range(12):
        time.sleep(1)
        im = screenshot(serial)
        if is_daily_claim_dialog(im) or is_checkin_page(im):
            break
        if is_bonus_dialog(im):
            tap(serial, im, "bonus_close")
            time.sleep(1)
    return True


def select_free_and_start(serial, tag=""):
    """在打卡页点「去观看」，只选免费项并启动广告。返回是否成功启动。

    安全核心：底部动作按钮只有在免费项已选中、付费项已取消时才点；
    任何一步无法确认就零点击返回，绝不误触「消耗金币/去支付」。
    """
    img = screenshot(serial)
    if not is_checkin_page(img):
        log("[ERR] 不在打卡页，跳过本轮")
        return False
    tap(serial, img, "watch_ad")
    time.sleep(1.5)

    img = screenshot(serial)
    if not is_ad_option_dialog(img):
        log("[WARN] 未出现广告方式弹窗（任务可能已完成），本轮结束")
        return False
    save_shot(img, f"{tag}03_ad_option.png")

    # 最多试 3 次：点免费项整行 → 校验绿勾/红勾状态
    ok = False
    for i in range(3):
        tap(serial, img, "free_option")
        time.sleep(0.6)
        img = screenshot(serial)
        free_ok = is_free_option_selected(img)
        paid_ok = not is_paid_option_selected(img)
        log(f"选项校验 #{i+1}: 免费勾={free_ok} 付费未选={paid_ok}")
        if free_ok and paid_ok:
            ok = True
            break
    if not ok:
        log("[ERR] 无法确认已选中免费项，零点击退出（绝不点支付）")
        save_shot(img, f"{tag}03_option_unsafe.png")
        return False

    tap(serial, img, "go_ad")
    time.sleep(2)
    log("已选免费项并启动广告")
    return True


def watch_one_ad(serial, tag=""):
    """完整看一条广告并从广告层回到打卡页。返回 'ad'/'final'/None。"""
    if not select_free_and_start(serial, tag):
        return None

    log("等待广告完成...")
    deadline = time.time() + AD_WAIT
    started = time.time()
    while time.time() < deadline:
        time.sleep(3)
        im = screenshot(serial)
        if is_keep_watching(im):
            log("继续观看")
            tap(serial, im, "keep_watching")
            time.sleep(2)
            continue
        if is_ad_reward(im) and (time.time()-started) >= AD_MIN:
            log("已成功领取奖励")
            save_shot(im, f"{tag}04_reward.png")
            break
    else:
        log("[WARN] 广告等待超时，检查截图")

    im = screenshot(serial)
    if is_ad_reward(im):
        tap(serial, im, "reward_close")
        time.sleep(1.5)

    # 回到打卡页；若任务全完成会出现「打卡成功」弹窗
    for _ in range(4):
        im = screenshot(serial)
        if is_checkin_page(im):
            return "ad"
        if is_daily_claim_dialog(im):
            save_shot(im, f"{tag}05_daily_claim.png")
            return "final"
        back(serial)
        time.sleep(1.2)
    im = screenshot(serial)
    if is_daily_claim_dialog(im):
        return "final"
    log("[WARN] 无法回到打卡页")
    return None


def finish_daily_claim(serial):
    """6/6 完成后：点「立即领取」看最后一条奖励广告并关闭。"""
    im = screenshot(serial)
    if not is_daily_claim_dialog(im):
        return False
    log("打卡成功弹窗 -> 立即领取（免费看广告）")
    tap(serial, im, "daily_claim")
    time.sleep(2)

    deadline = time.time() + AD_WAIT
    started = time.time()
    while time.time() < deadline:
        time.sleep(3)
        im = screenshot(serial)
        if is_keep_watching(im):
            tap(serial, im, "keep_watching")
            time.sleep(2)
            continue
        if is_ad_reward(im) and (time.time()-started) >= AD_MIN:
            save_shot(im, "06_final_reward.png")
            break
    im = screenshot(serial)
    if is_ad_reward(im):
        tap(serial, im, "reward_close")
        time.sleep(1.5)
    for _ in range(4):
        im = screenshot(serial)
        if is_checkin_page(im):
            return True
        back(serial)
        time.sleep(1.2)
    return is_checkin_page(screenshot(serial))


def main():
    ap = argparse.ArgumentParser(description="快手连续打卡白拿新手机（免费，模板定位卡片）")
    ap.add_argument("--serial", default="2NSDU20423005421")
    ap.add_argument("--rounds", type=int, default=1, help="最多看几条广告（测试1个=1，开始打卡=20）")
    ap.add_argument("--delay-min", type=float, default=8.0, help="广告之间随机延时下限秒")
    ap.add_argument("--delay-max", type=float, default=20.0, help="广告之间随机延时上限秒")
    ap.add_argument("--force", action="store_true", help="忽略当日幂等")
    ap.add_argument("--dry-run", action="store_true",
                    help="测试模式：完整跑一遍但不写当日幂等记录，便于反复验证")
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
    if not open_checkin_page(args.serial):
        sys.exit(4)

    done = 0
    confirmed = False
    for i in range(max(1, args.rounds)):
        tag = f"r{i+1:02d}_"
        im = screenshot(args.serial)
        if is_daily_claim_dialog(im):
            log("检测到「打卡成功」弹窗，进入最终领取")
            break
        result = watch_one_ad(args.serial, tag)
        if result is None:
            log(f"[WARN] 第 {i+1} 条广告未完成，停止后续轮次（不猜点）")
            break
        done += 1
        log(f"已完成第 {done} 条广告")
        if result == "final":
            log("6/6 完成，进入最终领取")
            break
        if i + 1 < args.rounds:
            d = random.uniform(args.delay_min, args.delay_max)
            log(f"随机等待 {d:.1f}s 后继续")
            time.sleep(d)

    # 最终「立即领取」奖励
    im = screenshot(args.serial)
    if is_daily_claim_dialog(im):
        confirmed = finish_daily_claim(args.serial)

    final = screenshot(args.serial)
    save_shot(final, "07_final.png")
    if is_checkin_page(final):
        confirmed = True
    log(f"[OK] 本轮完成广告 {done} 条" + ("，已确认打卡页" if confirmed else "，未确认打卡页，请查看 07_final.png"))

    if args.dry_run:
        log("[DONE] 测试模式：跳过写入当日幂等记录")
        return
    state[key] = {"ok": confirmed, "date": today, "serial": args.serial,
                  "ts": time.time(), "ads": done, "confirmed": confirmed}
    json_save(STATE, state)
    log("[DONE] 已写当日记录" if confirmed else "[DONE] 写入未确认记录（下次可重试）")


if __name__ == "__main__":
    main()
