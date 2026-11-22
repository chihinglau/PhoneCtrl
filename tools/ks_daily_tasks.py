#!/usr/bin/env python3
"""快手「365天打卡 · 今日6子任务」自动化（分步实现，纯免费，绝不支付金币/走快速通）。

子任务（--task）：
  signin  每日签到          —— 点「去签到」，回读 N/6 增加 + 按钮变「已完成」
  hot     观看热门内容(0/5)  —— 计数型，回读打卡页 (N/5)
  video   观看精彩视频(80分)  —— 时长型，回读挂件「倒计时 MM:SS」
  drama   观看精彩短剧(30分)  —— 时长型，回读挂件「倒计时 MM:SS」
  app     下载并体验随机APP   —— 保守识别，识别失败即跳过，绝不误装

硬约束（用户确认）：
  1. 时长型必须回读 App 内真实进度（倒计时 OCR），不用本地计时估算。
  2. 「下载并体验」目标每天随机，运行时识别；失败则跳过并记录，绝不误装。

安全：绝不点「快速通 / 消耗金币」；二选一弹窗一律选免费项；
      每步校验前台；无法确认就零点击退出，不猜点。

复用 ks_quick_checkin.py 的 adb/截图/导航/页面检测。
坐标基于 EBG-AN00 1080x2340。
"""
import argparse
import random
import re
import sys
import time
from datetime import date
from pathlib import Path

import numpy as np
import cv2

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ks_quick_checkin as k
import ks_ocr as O
from ks_digits import read_mmss

PKG = k.PKG
SHOTS = k.SHOTS
STATE = k.ROOT / ".tools" / "ks_daily_tasks_state.json"

# 任务行顺序（展开「今日打卡任务」后，自上而下；已完成的会从列表移除）
TASK_ORDER = ["video", "signin", "ad", "drama", "app", "hot"]
TASK_CN = {"video":"观看精彩视频","signin":"每日签到","ad":"观看广告",
           "drama":"观看精彩短剧","app":"下载并体验","hot":"观看热门内容"}

# 弹窗关闭坐标（1080x2340）
X_BONUS = (975, 480)     # 「翻倍任务开启」弹窗 ×
X_OPTION = (1010, 1015)  # 「今日任务」二选一弹窗 ×（实测通用）


def log(msg):
    print(f"[ks-daily:{CURRENT['task']}] {msg}", flush=True)


CURRENT = {"task": "-"}

# ---------------------------------------------------------------- 基础设施
def on_checkin(im):
    """OCR 判定打卡页（颜色判定在任务展开后会失效）。"""
    return O.is_checkin_page(im)


def shot(im, name):
    return k.save_shot(im, f"{CURRENT['task']}_{name}.png")


def tap_xy(x, y, what):
    k.tap_xy(SERIAL, x, y, what)


def save_state(task, data):
    st = k.json_load(STATE)
    key = f"{SERIAL}|{date.today().isoformat()}|{task}"
    st[key] = {"date": date.today().isoformat(), "serial": SERIAL,
               "ts": time.time(), **data}
    k.json_save(STATE, st)


def load_state(task):
    st = k.json_load(STATE)
    return st.get(f"{SERIAL}|{date.today().isoformat()}|{task}", {})


def close_dialogs(im, max_round=4):
    """关闭干扰弹窗：翻倍弹窗 / 二选一弹窗。返回 (是否已关净, 最新图)。"""
    for _ in range(max_round):
        # 翻倍弹窗：OCR 标题文字确认，× 也用 OCR 定位（颜色判定易误报致循环）
        if O.find_text(im, ['翻倍任务开启', '看内容领取金币', '最高1600'], 0.6):
            log("关闭翻倍弹窗(OCR)")
            xpos = O.find_text(im, ['X', '×'], 0.5)
            tap_xy(*(xpos[:2] if xpos else X_BONUS), "翻倍弹窗×")
            time.sleep(0.8); im = k.screenshot(SERIAL); continue
        # 二选一弹窗：标题「今日广告任务/今日任务/可任选一方式完成任务」
        if O.find_text(im, ['今日广告任务', '可任选一方式完成任务', '今日任务'], 0.6):
            log("关闭二选一弹窗(OCR)")
            xpos = O.find_text(im, ['X', '×'], 0.5)
            tap_xy(*(xpos[:2] if xpos else X_OPTION), "弹窗×")
            time.sleep(0.8); im = k.screenshot(SERIAL); continue
        break
    return im


def goto_earn_safe():
    """稳健进入任务中心：强制回首页 -> 点底部「去赚钱」-> 校验。不用 is_bonus_dialog 判定。"""
    for attempt in range(4):
        k.adb(SERIAL, "shell", "am", "start", "-n", k.HOME_ACTIVITY, capture=False)
        time.sleep(1.5)
        im = k.screenshot(SERIAL)
        if O.is_earn_page(im):
            return im
        # 底部「去赚钱」是艺术字，OCR 常识别成乱码（ȥ׬Ǯ），坐标实测 (757,2274)。
        # 先用 OCR 找（若识别正常），否则用可靠回退坐标 (760,2274)。
        earn = O.find_text(im, ['去赚钱', '赚钱'], 0.6)
        if earn and earn[1] > int(2100/2340*im.height):
            tap_xy(earn[0], earn[1], "OCR去赚钱")
        else:
            tap_xy(int(760/1080*im.width), int(2274/2340*im.height), "去赚钱(回退)")
        time.sleep(2)
        im = k.screenshot(SERIAL)
        # 进任务中心后可能有翻倍弹窗：用 OCR 标题文字确认（颜色判定易误报）
        if O.find_text(im, ['翻倍任务开启', '看内容领取金币', '最高1600'], 0.6):
            log("检测到翻倍弹窗，OCR 定位 × 关闭")
            xpos = O.find_text(im, ['X', '×'], 0.5)
            if xpos:
                tap_xy(xpos[0], xpos[1], "翻倍弹窗×")
            else:
                tap_xy(*X_BONUS, "翻倍弹窗×(回退)")
            time.sleep(0.8)
            im = k.screenshot(SERIAL)
        if O.is_earn_page(im):
            return im
    return None


def open_checkin():
    """进入 365 天打卡页并展开全部任务。返回是否成功。"""
    if not k.ensure_kuaishou(SERIAL):
        log("[ERR] 无法启动快手"); return False
    im = k.screenshot(SERIAL)
    if on_checkin(im) or k.is_daily_claim_dialog(im):
        pass
    else:
        if goto_earn_safe() is None:
            log("[ERR] 无法进任务中心"); return False
        # 滚动找打卡卡片（滚动时若误判 bonus，直接忽略，靠模板匹配定位）
        btn = None
        for i in range(16):
            im = k.screenshot(SERIAL)
            m = k.find_claim_button(im)
            if m:
                btn = m
                break
            k.adb(SERIAL, "shell", "input", "swipe", "540", "1900", "540", "900", "420", capture=False)
            time.sleep(0.7)
        if btn is None:
            log("[ERR] 未找到打卡卡片（今日可能已完成）"); return False
        tap_xy(*btn, "去领取")
        for _ in range(12):
            time.sleep(1)
            im = k.screenshot(SERIAL)
            if k.is_daily_claim_dialog(im) or on_checkin(im):
                break
            close_dialogs(im)
    im = k.screenshot(SERIAL)
    if k.is_daily_claim_dialog(im):
        log("检测到打卡成功弹窗（今日6项已满）")
    # 展开更多任务：OCR 定位（硬编码 y=1503 在任务行数变化时会点空）
    im = k.screenshot(SERIAL)
    exp = O.find_text(im, ['展开更多任务', '展开更多', '收起更多'], 0.6)
    if exp and '收起' not in exp[2]:
        tap_xy(exp[0], exp[1], "OCR展开更多任务")
        time.sleep(1.0)
    else:
        log("未发现「展开更多任务」按钮（已展开或无此按钮）")
    return True


def find_buttons(im):
    """检测右侧所有黑色按钮 y 中心（未完成任务）。返回 [(y, x)] 按上到下。"""
    a = np.array(im)
    sub = a[:, 780:1040, :]
    black = (sub[:,:,0] < 55) & (sub[:,:,1] < 55) & (sub[:,:,2] < 55)
    counts = black.sum(axis=1)
    rows = [y for y, c in enumerate(counts) if c > 60]
    segs = []
    for y in rows:
        if segs and y - segs[-1][-1] <= 2:
            segs[-1].append(y)
        else:
            segs.append([y])
    return [((s[0]+s[-1])//2, 910) for s in segs if s[-1]-s[0] > 40]


def locate_task_button(im, task, scroll_rounds=6):
    """滚动查找目标任务按钮，返回 (x,y) 或 None。

    策略：任务按 TASK_ORDER 排列，已完成的从列表移除（按钮变灰不计入）。
    通过每行标题文字模板识别任务身份；找不到模板时回退按失败计数顺序。
    """
    from ks_task_titles import identify_row  # 行标题识别
    for r in range(scroll_rounds + 1):
        im = k.screenshot(SERIAL)
        im = close_dialogs(im)
        btns = find_buttons(im)
        identified = identify_row(im, btns)  # [(task, (x,y))]
        for t, pos in identified:
            if t == task:
                log(f"定位到「{TASK_CN[task]}」按钮 {pos}")
                return pos
        if r < scroll_rounds:
            # 向下滚动
            k.adb(SERIAL, "shell", "input", "swipe", "540", "1900", "540", "900", "420", capture=False)
            time.sleep(0.7)
    log(f"未定位到「{TASK_CN[task]}」按钮（可能今日已完成或不在此页）")
    return None


def read_progress_count(im, task):
    """回读计数型任务进度 (N/total)。返回 (n,total) 或 None。"""
    from ks_task_titles import read_task_count
    return read_task_count(im, task)


def find_red_check(img):
    """找二选一弹窗里的红色勾选圆（免费项选中标记）。返回 (cx,cy) 或 None。
    真机：红勾 RGB 约 (240,60,100)，圆形 w/h 40~60，area>800。"""
    a = np.array(img)
    red = ((a[:,:,0].astype(int)-a[:,:,1].astype(int) > 50) &
           (a[:,:,0].astype(int)-a[:,:,2].astype(int) > 25) &
           (a[:,:,0] > 170))
    mask = red.astype(np.uint8)
    n, lab, stats, cent = cv2.connectedComponentsWithStats(mask, 8)
    cands = []
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if area > 700 and 32 <= w <= 75 and 32 <= h <= 75:
            cands.append((int(cent[i][0]), int(cent[i][1]), y, area))
    cands.sort(key=lambda c: -c[3])
    return cands[0] if cands else None


def choose_free_option():
    """二选一弹窗：默认免费项已选中（红勾在上行）。校验红勾位置后点底部动作按钮。
    绝不点「消耗金币」付费项。"""
    import cv2  # noqa
    im = k.screenshot(SERIAL)
    if not k.is_ad_option_dialog(im):
        log("[WARN] 未出现二选一弹窗"); return None
    shot(im, "option_dialog")
    chk = find_red_check(im)
    if chk is None:
        log("[ERR] 未找到红勾，无法确认免费项，零点击退出")
        shot(im, "option_unsafe"); return None
    cx, cy, top, area = chk
    log(f"红勾中心 ({cx},{cy}) area={area}")
    # 免费项在上、付费项在下；红勾须在弹窗选项区上半（y < 1600 @2340）
    if cy > int(1620/2340*im.height):
        log("[ERR] 红勾在付费项行，拒绝点击（绝不支付）")
        shot(im, "option_paid_selected"); return None
    # 底部「去完成 / 去看视频 / 去下载」绿色按钮
    tap_xy(540, int(2085/2340*im.height), "免费-去完成")
    time.sleep(2)
    log("已确认免费项并进入内容")
    return "free"


# ---------------------------------------------------------------- 各子任务
def task_signin():
    """每日签到：点去签到 -> 回读 N/6 增加。"""
    pos = locate_task_button(None, "signin")
    if pos is None:
        log("「每日签到」按钮不存在（可能已完成）")
        return {"ok": True, "note": "按钮不存在/已完成"}
    before = read_progress_count(k.screenshot(SERIAL), "any")
    tap_xy(*pos, "去签到")
    time.sleep(1.5)
    im = k.screenshot(SERIAL); shot(im, "after_sign")
    after = read_progress_count(im, "any")
    log(f"签到后计数 {before} -> {after}")
    ok = (after is not None and before is not None and after[0] > before[0])
    if not ok:
        # 回读按钮是否变灰「已完成」
        from ks_task_titles import is_button_done
        ok = is_button_done(im, "signin")
    return {"ok": ok, "before": before, "after": after}


def _swipe_content():
    """上滑切换到下一个内容（时长型是任务级计时，切内容不重置）。"""
    k.adb(SERIAL, "shell", "input", "swipe", "540", "1800", "540", "600", "350", capture=False)


def _back_to_checkin(max_back=8):
    """从内容流连续返回直到打卡页。返回最新截图（可能不是打卡页）。"""
    im = None
    for _ in range(max_back):
        k.back(SERIAL); time.sleep(1.1)
        im = k.screenshot(SERIAL)
        if on_checkin(im):
            return im
        # 二选一/翻倍等弹窗先关掉（close_dialogs 内部用 OCR 判定，颜色易误报）
        im = close_dialogs(im)
    return im


def _ensure_checkin():
    """确保停在打卡页：先 back 尝试，失败则强制回首页重新导航进打卡页。"""
    im = _back_to_checkin()
    if im and on_checkin(im):
        return im
    log("back 未回到打卡页，强制重新导航")
    if open_checkin():
        return k.screenshot(SERIAL)
    return im


def _is_task_completed(task, im):
    """回打卡页确认任务是否真正完成：该行按钮消失或变灰「已完成」。"""
    from ks_task_titles import _match_row, is_button_done
    m = _match_row(im, task)
    if m is None:
        # 行模板找不到可能是已完成移除了，也可能是页面不对；用完成态兜底
        log(f"回读：未找到「{TASK_CN[task]}」行，检查完成态")
        return is_button_done(im, task) or False
    return is_button_done(im, task)


def watch_time_based(task, watch_seconds):
    """时长型（视频/短剧）：一次运行持续观看直到 App 倒计时归零并在打卡页确认完成。

    - 循环上滑内容 + 周期性回读倒计时（真实进度，非本地计时）。
    - 倒计时归零 → 回打卡页确认该行完成 → 才算 ok=True。
    - 页面被弹窗/落地页打断 → 自动返回并重进内容页继续。
    - watch_seconds 为本次运行的超时保护上限（默认 video/drama 很长，测试可用 --watch 短跑）。
    - 超时未归零 → 返回 in_progress，靠 App 持久倒计时，下次 --force 运行继续累积。
    """
    # 首次进入
    pos = locate_task_button(None, task)
    if pos is None:
        log(f"「{TASK_CN[task]}」按钮不存在（可能已完成）")
        im = k.screenshot(SERIAL)
        return {"ok": _is_task_completed(task, im), "note": "按钮不存在"}
    tap_xy(*pos, f"{task}去观看")
    time.sleep(1.5)
    if choose_free_option() is None:
        return {"ok": False, "note": "未能安全选免费项"}

    im = k.screenshot(SERIAL); shot(im, "enter")
    cur, sc = read_mmss(im)
    if cur is None:
        time.sleep(2); im = k.screenshot(SERIAL); cur, sc = read_mmss(im)
    log(f"进入时倒计时 {cur} (score {sc:.2f})，开始持续观看直到归零")

    deadline = time.time() + watch_seconds
    last = cur
    lasted_progress = 0      # 连续读到倒计时的轮数
    no_countdown = 0         # 连续读不到倒计时的轮数（页面可能丢失）
    while time.time() < deadline:
        time.sleep(random.uniform(6, 10))
        _swipe_content()
        time.sleep(1.5)
        im = k.screenshot(SERIAL)
        cur, sc = read_mmss(im)
        if cur:
            no_countdown = 0
            if last and cur != last:
                lasted_progress += 1
                if lasted_progress <= 3 or lasted_progress % 10 == 0:
                    log(f"倒计时 {last} -> {cur} (score {sc:.2f})  已推进{lasted_progress}次")
            last = cur
            if cur == "00:00":
                log("倒计时归零，返回打卡页确认完成")
                back = _ensure_checkin()
                done = _is_task_completed(task, back) if back and on_checkin(back) else False
                if done:
                    return {"ok": True, "countdown": cur, "note": "倒计时归零且打卡页确认完成"}
                # 归零但列表还没刷新，再等两轮回读
                log("归零后打卡页未确认完成，继续观察")
                time.sleep(6)
        else:
            no_countdown += 1
            log(f"倒计时读取失败 (score {sc:.2f})，连续{no_countdown}次")
            if no_countdown >= 3:
                # 可能跳出内容页（作者页/落地页/弹窗/回打卡页），确保回打卡页确认或重进
                back = _ensure_checkin()
                if back and on_checkin(back):
                    if _is_task_completed(task, back):
                        return {"ok": True, "countdown": last, "note": "打卡页确认任务已完成"}
                    pos = locate_task_button(None, task)
                    if pos:
                        log("任务未完成，重新进入内容页继续观看")
                        tap_xy(*pos, f"{task}重进")
                        time.sleep(1.5)
                        if k.is_ad_option_dialog(k.screenshot(SERIAL)):
                            choose_free_option()
                        time.sleep(2)
                no_countdown = 0

    # 超时：靠 App 持久倒计时累积，不做本地计时估算
    back = _ensure_checkin()
    done = _is_task_completed(task, back) if back and on_checkin(back) else False
    return {"ok": done, "in_progress": (lasted_progress > 0 or done), "countdown": last,
            "note": ("本次观看达超时上限，倒计时持久，下次 --force 继续"
                     if not done else "超时回读时已确认完成")}


def task_video():
    return watch_time_based("video", WATCH_VIDEO_SEC)


def task_drama():
    return watch_time_based("drama", WATCH_DRAMA_SEC)


def task_hot():
    """观看热门内容(0/5)：一次运行循环进入观看到计数满(5/5)或完成态。"""
    max_rounds = HOT_MAX_ROUNDS   # 单条约看 22 秒，40 轮足够 5 次 + 重试冗余；短测可调小
    last_after = None
    for rnd in range(1, max_rounds + 1):
        pos = locate_task_button(None, "hot")
        if pos is None:
            log("「观看热门内容」按钮不存在（可能已完成）")
            im = k.screenshot(SERIAL)
            from ks_task_titles import is_button_done
            if is_button_done(im, "hot"):
                return {"ok": True, "after": last_after, "done": True, "note": "按钮已完成"}
            break
        tap_xy(*pos, "hot去观看")
        time.sleep(1.5)
        if k.is_ad_option_dialog(k.screenshot(SERIAL)):
            if choose_free_option() is None:
                return {"ok": False, "note": "未安全选免费项", "after": last_after}
        # 观看本条内容：周期上滑（热门内容按观看条数/时长计数）
        log(f"第 {rnd} 轮：观看热门内容 22 秒")
        for _ in range(3):
            time.sleep(7)
            _swipe_content()
            time.sleep(1)
        # 回打卡页回读 (N/5)
        back = _back_to_checkin()
        if back is None or not on_checkin(back):
            log("[WARN] 未回到打卡页，强制重新导航")
            if not open_checkin():
                return {"ok": False, "note": "无法回到打卡页", "after": last_after}
            back = k.screenshot(SERIAL)
        after = read_progress_count(back, "hot")
        if after:
            last_after = after
        from ks_task_titles import is_button_done
        done = is_button_done(back, "hot")
        log(f"回读热门内容 {after} 完成态={done}")
        if done or (after and after[0] >= after[1] and after[1] > 0):
            return {"ok": True, "after": after, "done": done, "note": "计数满额/按钮已完成"}
    return {"ok": False, "after": last_after, "note": f"达{max_rounds}轮上限仍未完成"}


def _ocr_tap(im, kws, what, score=0.6, dy_max=None):
    """用 OCR 找文字并点击其中心。返回是否点到。"""
    import ks_ocr as O
    hit = O.find_text(im, kws, score)
    if not hit:
        log(f"[WARN] OCR 未找到「{what}」({kws})")
        return False
    x, y = hit[0], hit[1]
    tap_xy(x, y, f"OCR-{what}")
    return True


def task_ad():
    """观看广告(0/20)：一次运行循环看满 20 条。入口/弹窗/领奖/计数全用 OCR 定位。

    页面完成数变化会让广告行上下移动，硬编码坐标会点空——故按钮、弹窗动作、关闭 ×
    一律通过 RapidOCR 识别屏幕文字定位；绝不点「消耗金币/跳过今日任务/快速通」。
    """
    import ks_ocr as O
    max_ads = AD_MAX
    start_count = None
    stall = 0
    # 轮次上限 = 目标条数 + 少量失败重试冗余（--watch 短测时 AD_MAX 已收敛）
    for rnd in range(1, max_ads + 3):
        back = _ensure_checkin()
        if back is None or not on_checkin(back):
            log("[ERR] 无法回到打卡页，广告任务中止")
            break
        # OCR 回读计数 (N/20)，满额即完成
        cnt = O.read_count_near(back, '观看广告', 0.5)
        if cnt:
            if start_count is None:
                start_count = cnt
            log(f"第{rnd}轮前计数 {cnt}")
            if cnt[0] >= cnt[1] and cnt[1] > 0:
                return {"ok": True, "count": cnt, "note": "计数满额"}
        # OCR 定位「观看广告」行的「去观看」按钮
        btn = O.find_row_button(back, '观看广告', '去观看', 0.5, 0.5)
        if btn is None:
            log("[WARN] OCR 未定位到广告行按钮（可能已完成/被移除）")
            stall += 1
            if stall >= 3:
                break
            time.sleep(2)
            continue
        tap_xy(*btn, "广告去观看")
        time.sleep(1.8)
        im = k.screenshot(SERIAL)
        if not k.is_ad_option_dialog(im):
            log("[WARN] 未出现广告二选一弹窗")
            stall += 1
            if stall >= 3:
                break
            continue
        shot(im, "ad_option")
        # 免费项校验：找红勾，须在免费项（「观看…广告完成任务」上半），不在「跳过/消耗金币」
        chk = find_red_check(im)
        go = O.find_text(im, ['去看广告', '去看视频', '去观看广告'], 0.6)
        if chk is None:
            log("[ERR] 未找到红勾，拒绝进入（防误点付费项）")
            shot(im, "ad_option_unsafe"); break
        cx, cy, top, area = chk
        paid = O.find_text(im, ['跳过今日任务', '消耗', '金币'], 0.6)
        if paid and cy > paid[1] - 40:
            log("[ERR] 红勾位于付费项行，拒绝点击（绝不支付）")
            shot(im, "ad_paid_selected"); break
        if go is None:
            log("[WARN] OCR 未找到「去看广告」按钮，回退固定底部坐标")
            k.tap_xy(SERIAL, 540, int(2085/2340*im.height), "去看广告(回退)")
        else:
            tap_xy(go[0], go[1], "OCR去看广告")
        time.sleep(2)
        # 广告观看：等「已成功领取奖励」→ 点顶部 × 关闭 → 回打卡页回读
        got_reward = False
        ad_deadline = time.time() + 75
        while time.time() < ad_deadline:
            time.sleep(3)
            aim = k.screenshot(SERIAL)
            # 只认「已成功领取奖励」/「继续观看」；排除广告中「Ns后可领取奖励」倒计时
            reward = None
            for box, txt, score in O.ocr_texts(aim):
                t = str(txt).replace(" ", "")
                if score < 0.6:
                    continue
                if "后可领取" in t:      # "17s后可领取奖励" = 还没结束
                    continue
                if "已成功领取奖励" in t or "继续观看" in t or "开心收下" in t:
                    reward = txt
                    break
            if reward:
                log(f"广告领奖: {reward}")
                got_reward = True
                time.sleep(1.0)
                # 领奖横幅的 × 是图标、OCR 常认成乱码，坐标波动(317~494,228)。
                # 更稳的是先尝试 back 离开广告落地页，由 _ensure_checkin 接管回打卡页。
                k.back(SERIAL); time.sleep(1.0)
                aim = k.screenshot(SERIAL)
                if 'AwardVideoPlayActivity' in k.foreground(SERIAL):
                    # 仍在广告页，点顶部横幅 ×（OCR 优先，回退按横幅右端）
                    xpos = O.find_text(aim, ['X', '×'], 0.55)
                    if xpos and xpos[1] < 400:
                        tap_xy(xpos[0], xpos[1], "奖励×")
                    else:
                        k.tap_xy(SERIAL, 469, 229, "奖励×(回退)")
                    time.sleep(1.0)
                break
            # 广告页被落地页/浏览器劫持时，强制回打卡页
            fg = k.foreground(SERIAL)
            if 'AwardVideoPlayActivity' not in fg and 'nebula' not in fg:
                log(f"广告页跳出到 {fg}，中止本条")
                break
        if not got_reward:
            # 未能确认领奖，尝试返回打卡页继续（不误判完成）
            log("[WARN] 本条未确认领奖，尝试回打卡页")
        stall = 0
    # 结束回读：最多重试 3 次确保回到打卡页并读到计数
    final = None
    for _ in range(3):
        back = _ensure_checkin()
        if back and on_checkin(back):
            final = O.read_count_near(back, '观看广告', 0.5)
        if final:
            break
        time.sleep(2)
    log(f"广告任务结束，最终计数 {final}")
    return {"ok": final is not None and final[0] >= final[1] and final[1] > 0,
            "count": final, "note": "OCR 回读最终计数"}


def task_app():
    """下载并体验随机 APP（目标每天变）：保守策略——无法可靠识别就跳过，绝不误装。

    现状：本机无中文 OCR、uiautomator dump 因动画失败，无法可靠读出当天目标 APP 名。
    因此本任务只做：定位任务卡 -> 截图留档当天目标行 -> 记录 skipped。
    绝不点「去下载」进入安装链路（避免误装其它 APP / 消耗金币）。
    待评审：接入中文 OCR 或应用商店落地页识别后，再启用真实的下载+体验流程。
    """
    pos = locate_task_button(None, "app")
    if pos is None:
        log("「下载并体验」按钮不存在（可能已完成）")
        return {"ok": False, "note": "按钮不存在/已完成"}
    im = k.screenshot(SERIAL)
    shot(im, "app_target_row")
    from ks_task_titles import read_target_app
    app = read_target_app(im)
    log(f"当天目标 APP 识别结果: {app!r}（无中文OCR，预期为 None）")
    log("[SKIP] 无法可靠识别目标 APP，保守跳过，绝不误装/绝不点去下载")
    return {"ok": False, "skipped": True, "target": app,
            "note": "无中文OCR，保守跳过；下载/安装需评审后启用（目标行已截图留档）"}


TASKS = {"signin": task_signin, "video": task_video, "drama": task_drama,
         "hot": task_hot, "ad": task_ad, "app": task_app}

# 单次运行超时保护上限（秒）：足够一次做完整个时长型任务，留冗余。
# video 80分钟 + 重进/结算冗余 = 5400s(90分)；drama 30分钟 = 2400s(40分)。
WATCH_VIDEO_SEC = 5400
WATCH_DRAMA_SEC = 2400
HOT_MAX_ROUNDS = 40
AD_MAX = 20


def main():
    global SERIAL
    ap = argparse.ArgumentParser(description="快手365天打卡子任务（纯免费）")
    ap.add_argument("--task", required=True, choices=list(TASKS.keys()))
    ap.add_argument("--serial", default="2NSDU20423005421")
    ap.add_argument("--watch", type=int, default=None,
                    help="时长型本次运行超时上限秒（默认 video=5400/drama=2400；测试可传小数短跑）")
    ap.add_argument("--force", action="store_true", help="忽略当日幂等")
    ap.add_argument("--dry-run", action="store_true", help="不写幂等记录")
    args = ap.parse_args()
    SERIAL = args.serial
    CURRENT["task"] = args.task
    global WATCH_VIDEO_SEC, WATCH_DRAMA_SEC, HOT_MAX_ROUNDS, AD_MAX
    if args.watch:
        WATCH_VIDEO_SEC = WATCH_DRAMA_SEC = args.watch
        # 短测：热门内容也收敛轮数（约每轮 22s，最多 watch/22 轮，至少1轮）
        HOT_MAX_ROUNDS = max(1, args.watch // 22)
        # 广告每条约 30~40s；短测时按 watch 估算条数（至少1条）
        AD_MAX = max(1, args.watch // 35)

    if not k.ADB.exists():
        log(f"[ERR] adb not found: {k.ADB}"); sys.exit(1)

    st = load_state(args.task)
    if not args.force and st.get("ok"):
        log(f"[SKIP] 今日「{TASK_CN[args.task]}」已完成（幂等），--force 强制重跑"); return

    if not open_checkin():
        sys.exit(3)
    result = TASKS[args.task]()
    log(f"结果: {result}")
    if not args.dry_run:
        save_state(args.task, result)
        log("[DONE] 已写幂等记录" if result.get("ok") else "[DONE] 写入未完成记录（可重试）")


if __name__ == "__main__":
    main()
