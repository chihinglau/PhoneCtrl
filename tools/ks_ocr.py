"""快手自动化 OCR 公共模块（RapidOCR / onnxruntime，纯本地中文识别，无外部服务）。

用途：替代硬编码坐标——页面布局随完成情况变化（如打卡项完成数量改变行位置），
必须用 OCR 识别屏幕上的文字来定位按钮、判断弹窗、回读进度。

对外：
  get_engine()              单例 RapidOCR
  ocr_texts(img)            -> [(box, text, score)]  box=[[x,y]*4]
  find_text(img, kws, ...)  -> (cx, cy, text, score) 或 None；kws 可字符串/列表，命中任一即可
  find_all(img, kw, ...)    -> [(cx,cy,text,score)] 全部命中
  has_text(img, kws)        -> bool
  find_row_button(img, row_kw, btn_kw, ...) -> (bx,by) 与标题同行的按钮中心（按 y 就近）
  read_count_near(img, text, ...) -> (n,total)|None  读取文字附近括号计数 (N/20)
"""
from __future__ import annotations

import functools
import re
from pathlib import Path

import numpy as np

_ENGINE = None


@functools.lru_cache(maxsize=1)
def get_engine():
    global _ENGINE
    if _ENGINE is None:
        from rapidocr_onnxruntime import RapidOCR
        _ENGINE = RapidOCR()
    return _ENGINE


def ocr_texts(img):
    """返回 [(box, text, score)]，box 为 4 点轮廓。img 为 PIL.Image。"""
    res, _ = get_engine()(np.array(img))
    return res or []


def _norm(s):
    return re.sub(r"\s+", "", str(s)).strip()


def find_text(img, kws, score_min=0.5, contains=True):
    """找到命中任一关键词的文字，返回得分最高者的中心 (cx,cy,text,score)。
    contains=True 子串匹配；False 全等（去空格）。"""
    if isinstance(kws, str):
        kws = [kws]
    kws = [_norm(k) for k in kws]
    best = None
    for box, txt, score in ocr_texts(img):
        if score < score_min:
            continue
        t = _norm(txt)
        hit = any((k in t) if contains else (k == t) for k in kws)
        if not hit:
            continue
        cx = int(sum(p[0] for p in box) / 4)
        cy = int(sum(p[1] for p in box) / 4)
        if best is None or score > best[3]:
            best = (cx, cy, txt, float(score))
    return best


def find_all(img, kw, score_min=0.5):
    kw = _norm(kw)
    out = []
    for box, txt, score in ocr_texts(img):
        if score < score_min or kw not in _norm(txt):
            continue
        cx = int(sum(p[0] for p in box) / 4)
        cy = int(sum(p[1] for p in box) / 4)
        out.append((cx, cy, txt, float(score)))
    return out


def has_text(img, kws, score_min=0.5):
    return find_text(img, kws, score_min) is not None


def find_row_button(img, row_kw, btn_kw, row_score=0.5, btn_score=0.5,
                    max_dy=70, btn_right_of_row=True):
    """给定行标题关键词与按钮关键词，返回按钮中心 (bx,by)。

    行标题可能多个（如多个「去观看」），取与 row_kw 所在 y 最近、且在其右侧的按钮。
    """
    row = find_text(img, row_kw, row_score)
    if not row:
        return None
    ry = row[1]
    cands = find_all(img, btn_kw, btn_score)
    if not cands:
        return None
    valid = [c for c in cands if abs(c[1]-ry) <= max_dy and (not btn_right_of_row or c[0] > row[0])]
    if not valid:
        valid = [c for c in cands if abs(c[1]-ry) <= max_dy]
    if not valid:
        return None
    best = min(valid, key=lambda c: abs(c[1]-ry))
    return (best[0], best[1])


def is_checkin_page(img):
    """OCR 判定是否在 365 天打卡页：顶部标题「完成365天打卡任务 白拿好礼」。
    比颜色判定可靠——任务展开/完成后底部日历格子位置变化会让颜色判定失效。"""
    return find_text(img, ['完成365天打卡任务', '365天打卡任务', '打卡任务白拿好礼'], 0.6) is not None


def is_earn_page(img):
    """OCR 判定任务中心：顶部「金币：/现金：」+「日常任务」+打卡卡片。"""
    has_gold = find_text(img, ['金币：', '金币:', '瓜分百亿'], 0.6) is not None
    has_daily = find_text(img, ['日常任务', '连续打卡'], 0.6) is not None
    return has_gold and has_daily


def read_count_near(img, anchor_kw, score_min=0.5, dy=70, x_right=True):
    """在 anchor 文字附近读取括号计数 (N/total)，如「观看广告 (0/20)」。
    返回 (n, total) 或 None。OCR 常把 (0/20) 识别成 '0/20' 或 '(0/20)'。"""
    a = find_text(img, anchor_kw, score_min)
    if not a:
        return None
    ax, ay = a[0], a[1]
    for box, txt, score in ocr_texts(img):
        if score < score_min:
            continue
        cx = int(sum(p[0] for p in box) / 4)
        cy = int(sum(p[1] for p in box) / 4)
        if abs(cy-ay) > dy:
            continue
        if x_right and cx <= ax:
            continue
        m = re.search(r"[（(]?\s*(\d+)\s*/\s*(\d+)\s*[)）]?", txt)
        if m:
            return (int(m.group(1)), int(m.group(2)))
    return None
