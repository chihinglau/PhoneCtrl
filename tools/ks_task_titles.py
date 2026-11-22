"""快手打卡任务行识别：用整行图像指纹模板定位每个任务，回读计数/完成状态。

模板 templates/ks_row_<task>.png（图标+标题+副标题，400x84 @1080x2340）。
匹配时按屏幕宽度缩放模板，TM_CCOEFF_NORMED；阈值 0.80。

对外：
  identify_row(img, buttons) -> [(task, (x,y))]   识别每个黑按钮属于哪个任务
  read_task_count(img, task) -> (n,total)|None    回读 (N/5)/(N/20) 计数
  is_button_done(img, task)  -> bool              按钮是否变灰「已完成」
  read_target_app(img)       -> str|None          下载任务当天目标APP（保守）
  read_total_progress(img)   -> (n,6)|None        顶部「今日打卡任务 N/6」
"""
from __future__ import annotations

import re
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
TPL_DIR = ROOT / "templates"
ROW_TASKS = ["video", "signin", "ad", "drama", "app", "hot"]
_THRESH = 0.80


def _match_row(img, task):
    p = TPL_DIR / f"ks_row_{task}.png"
    if not p.exists():
        return None
    tpl = cv2.imread(str(p))
    screen = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
    scale = img.width / 1080.0
    if abs(scale - 1.0) > 0.02:
        tpl = cv2.resize(tpl, None, fx=scale, fy=scale)
    th, tw = tpl.shape[:2]
    if th > screen.shape[0] or tw > screen.shape[1]:
        return None
    res = cv2.matchTemplate(screen, tpl, cv2.TM_CCOEFF_NORMED)
    _, mx, _, loc = cv2.minMaxLoc(res)
    if mx < _THRESH:
        return None
    return {"score": mx, "x": loc[0], "y": loc[1], "w": tw, "h": th}


def identify_row(img, buttons):
    """buttons: [(y,x)] 黑按钮中心。返回 [(task,(x,y))]。"""
    result = []
    for task in ROW_TASKS:
        m = _match_row(img, task)
        if not m:
            continue
        row_cy = m["y"] + m["h"] / 2
        # 找同一行的黑按钮（y 接近）
        best = None
        for by, bx in buttons:
            if abs(by - row_cy) < 70:
                if best is None or abs(by - row_cy) < abs(best[0] - row_cy):
                    best = (by, bx)
        if best:
            result.append((task, (best[1], best[0])))
    return result


def _ocr_small_numbers(img, box):
    """回读小数字 (N/total)：用 ks_digits 的多字体模板在 box 内匹配数字。"""
    from ks_digits import _templates
    crop = img.crop(box)
    g = np.array(crop.convert("L"))
    inv = 255 - g
    lo, hi = np.percentile(inv, 25), np.percentile(inv, 95)
    if hi > lo:
        inv = np.clip((inv-lo)*255/(hi-lo), 0, 255).astype(np.uint8)
    bw = (inv > 110).astype(np.uint8)
    n, lab, stats, cent = cv2.connectedComponentsWithStats(bw, 8)
    H = bw.shape[0]
    chars = []
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        # 过滤括号/斜杠等窄字符（数字宽高比约 0.45~1.0）
        if h < H*0.4 or h > H*1.4 or area < 8 or w < h*0.42:
            continue
        ch = (lab[y:y+h, x:x+w] > 0).astype(np.uint8)
        bd, bs = None, -1
        for d, tpl in _templates():
            th, tw = tpl.shape
            r = h/th
            nw = max(1, int(tw*r))
            if nw > w*1.8 or nw < w*0.4:
                continue
            t = cv2.resize(tpl, (nw, h), interpolation=cv2.INTER_AREA)
            if nw > w:
                t = t[:, :w]
            else:
                pad = w-nw
                t = np.pad(t, ((0,0),(pad//2, pad-pad//2)))
            inter = (t & ch).sum(); union = (t | ch).sum()
            s = inter/union if union else 0
            if s > bs: bs, bd = s, d
        if bd and bs >= 0.45:
            chars.append((x, bd))
    chars.sort()
    return "".join(c[1] for c in chars)


def read_task_count(img, task):
    """回读计数。task='any' 时读所有已知计数行。"""
    m = _match_row(img, task if task in ROW_TASKS else "hot")
    if not m:
        return None
    # 计数在标题右侧括号内；标题越长起点越靠右
    x_start = {"ad": 430, "hot": 480}.get(task, 430)
    x0 = int(x_start*img.width/1080)
    x1 = int((x_start+180)*img.width/1080)
    y0 = m["y"] + int(8*img.height/2340)
    y1 = m["y"] + m["h"]
    txt = _ocr_small_numbers(img, (x0, y0, x1, y1))
    return _parse_count(txt)


def _parse_count(txt):
    """把 OCR 数字串解析成 (当前, 总数)。斜杠被滤掉，如 '020'->(0,20)、'320'->(3,20)、'05'->(0,5)。"""
    if not txt or not txt.isdigit():
        return None
    for total in (20, 10, 6, 5, 30, 7):
        s = str(total)
        if len(txt) > len(s) and txt.endswith(s):
            cur = txt[:-len(s)]
            if cur.isdigit():
                return (int(cur), total)
    return None


def is_button_done(img, task):
    """按钮是否变灰（已完成）。检测该行右侧按钮区是否无黑色按钮。"""
    m = _match_row(img, task)
    if not m:
        return False
    cy = m["y"] + m["h"]//2
    x0 = int(780*img.width/1080); x1 = int(1040*img.width/1080)
    reg = np.array(img)[cy-30:cy+30, x0:x1, :]
    black = ((reg[:,:,0]<55)&(reg[:,:,1]<55)&(reg[:,:,2]<55)).sum()
    return black < 40


def read_target_app(img):
    """识别下载任务当天目标 APP。保守：只认标题里「下载并体验」后的中文串无法OCR，
    故返回 None 触发跳过；待评审落地页识别后再启用。"""
    return None


def read_total_progress(img):
    """回读顶部「今日打卡任务 N/6」。"""
    # 「今日打卡任务 N/6」行约 y 375~405；N/6 是红色数字，在 x 约 380~460
    crop_box = (int(370*img.width/1080), int(374*img.height/2340),
                int(470*img.width/1080), int(406*img.height/2340))
    txt = _ocr_small_numbers(img, crop_box)
    nums = re.findall(r"\d+", txt)
    if nums:
        return (int(nums[0]), 6)
    return None
