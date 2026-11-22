"""快手打卡倒计时数字识别（纯 cv2 + PIL 系统字体模板，无外部 OCR 依赖）。

倒计时挂件形态（真机 1080x2340，右侧中部）：
  「倒计时 29:44」绿色胶囊 / 「倒计时 30:00」黑字，数字为系统无衬线粗体，
  字号高度约 28~34px。方案：连通域分割每个字符 -> 按尺寸过滤出数字 ->
  逐字符与多种系统字体渲染的 0-9 模板匹配取最高分 -> 组装 MM:SS。

对外接口：
  read_mmss(img, roi=None) -> (text:str|None, score:float)
  text 形如 "29:44"。roi 为 (x0,y0,x1,y1) 像素框，默认右侧挂件条带。
"""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

# 右侧倒计时挂件数字条带（1080x2340 基准）
ROI = (900, 855, 1075, 915)

_FONT_CANDIDATES = [
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/Dengb.ttf",
]


def _render_digit_templates():
    """[(digit, bin_img)]，二值 255=前景，多字体多字号，归一到高 40。"""
    out = []
    for fp in _FONT_CANDIDATES:
        if not Path(fp).exists():
            continue
        for size in (40, 36, 32, 28):
            try:
                font = ImageFont.truetype(fp, size)
            except Exception:
                continue
            for d in "0123456789":
                img = Image.new("L", (60, 60), 0)
                ImageDraw.Draw(img).text((30, 30), d, font=font, fill=255, anchor="mm")
                arr = np.array(img)
                ys, xs = np.where(arr > 120)
                if len(xs) < 20:
                    continue
                g = arr[max(0,ys.min()-1):ys.max()+2, max(0,xs.min()-1):xs.max()+2]
                scale = 40.0 / g.shape[0]
                g = cv2.resize(g, (max(1, int(g.shape[1]*scale)), 40),
                               interpolation=cv2.INTER_AREA)
                out.append((d, (g > 120).astype(np.uint8)))
    return out


_TPL = None


def _templates():
    global _TPL
    if _TPL is None:
        _TPL = _render_digit_templates()
    return _TPL


def _match_char(char_bin):
    """返回 (best_digit, best_score)。char_bin: 二值 uint8(0/1)。"""
    best_d, best_s = None, -1.0
    h, w = char_bin.shape
    for d, tpl in _templates():
        th, tw = tpl.shape
        # 按高度缩放模板到字符高度
        r = h / th
        nw = max(1, int(tw*r))
        if nw > w*1.6 or nw < w*0.5:
            continue
        t = cv2.resize(tpl, (nw, h), interpolation=cv2.INTER_AREA)
        # IoU-like：以较小宽度对齐比较
        if nw > w:
            t = t[:, :w]
        elif nw < w:
            pad = w - nw
            t = np.pad(t, ((0,0),(pad//2, pad-pad//2)))
        inter = (t & char_bin).sum()
        union = (t | char_bin).sum()
        s = inter/union if union else 0
        if s > best_s:
            best_s, best_d = s, d
    return best_d, float(best_s)


def read_mmss(img, roi=None, debug=False):
    crop = img.crop(roi) if roi is not None else img.crop(ROI)
    g = np.array(crop.convert("L"))
    # 数字在绿底/白底上均为深色 -> 反相为前景
    inv = 255 - g
    lo, hi = np.percentile(inv, 25), np.percentile(inv, 95)
    if hi > lo:
        inv = np.clip((inv-lo)*255.0/(hi-lo), 0, 255).astype(np.uint8)
    bw = (inv > 110).astype(np.uint8)

    # 连通域
    n, lab, stats, cent = cv2.connectedComponentsWithStats(bw, 8)
    cands = []
    H = bw.shape[0]
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        # 数字高度约占 ROI 高度 45%~100%；太窄的点/线是冒号或噪点
        if h < H*0.35 or h > H*1.3:
            continue
        # 宽高比过滤放宽，允许窄字符"1"（宽度可小至 H*0.09）
        if area < 12 or w < H*0.085 or w > H*1.2:
            continue
        ch = (lab[y:y+h, x:x+w] > 0).astype(np.uint8)
        d, s = _match_char(ch)
        cands.append({"x":x, "y":y, "w":w, "h":h, "d":d, "s":s})

    cands.sort(key=lambda c: c["x"])
    # 只保留同基准线（y 相近）
    if cands:
        ymid = np.median([c["y"] for c in cands])
        cands = [c for c in cands if abs(c["y"]-ymid) < H*0.3]

    digs = [c["d"] for c in cands if c["d"] and c["s"] >= 0.45]
    scores = [c["s"] for c in cands if c["d"] and c["s"] >= 0.45]
    text, avg = None, 0.0
    if len(digs) >= 4:
        # 取连续 4 位均分最高窗口
        best, ba = None, -1
        for i in range(len(digs)-3):
            a = sum(scores[i:i+4])/4
            if a > ba:
                ba, best = a, "".join(digs[i:i+4])
        text, avg = f"{best[:2]}:{best[2:]}", ba
    out = (text, float(avg))
    if debug:
        dbg = {"cands": cands, "inv": inv, "crop": crop}
        return out, dbg
    return out


if __name__ == "__main__":
    from PIL import Image as _Im
    sys_path = str(Path(__file__).resolve().parent)
    import sys
    sys.path.insert(0, sys_path)
    import ks_quick_checkin as k
    im = _Im.open(k.SHOTS / "drama_feed.png").convert("RGB")
    out, dbg = read_mmss(im, debug=True)
    print("read", out)
    print("cands", [(c["d"], round(c["s"],2), c["x"], c["w"], c["h"]) for c in dbg["cands"]])
