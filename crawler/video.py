"""YouTube：中繼資料（yt-dlp，不下載影片）、官方字幕、storyboard 代表畫面。"""
import io
import re

import cv2
import numpy as np
import yt_dlp
from PIL import Image
from youtube_transcript_api import YouTubeTranscriptApi

from fetch import download

LANG_PREF = ["zh-TW", "zh-Hant", "zh", "zh-HK", "zh-Hans", "yue-HK", "en", "en-US"]
# 字幕出現這些地名/文化詞時，該時間點的畫面優先保留
PLACE_WORDS = ["觀音", "燈塔", "白沙岬", "沙丘", "草漯", "藻礁", "飛機", "蓮花", "百合", "白千層",
               "隧道", "甘泉寺", "廟", "海", "石門水庫", "遷村", "移民", "地圖", "村", "老街", "客家"]


def video_id(url):
    m = re.search(r"(?:youtu\.be/|shorts/|v=|embed/)([\w-]{11})", url)
    return m.group(1) if m else None


def hms(sec):
    sec = int(sec)
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def metadata(vid):
    with yt_dlp.YoutubeDL({"skip_download": True, "quiet": True, "no_warnings": True}) as y:
        return y.extract_info("https://www.youtube.com/watch?v=" + vid, download=False)


def transcript(vid):
    """回傳 (字幕片段 list, 語言說明, 失敗原因)。只用平台公開提供的字幕，不做語音辨識。"""
    try:
        tl = YouTubeTranscriptApi().list(vid)
    except Exception as e:
        return [], "", f"{type(e).__name__}：此影片未提供字幕或逐字稿"
    items = list(tl)
    manual = [t for t in items if not t.is_generated]
    pick = None
    for pool in (manual, items):
        for lang in LANG_PREF:
            pick = next((t for t in pool if t.language_code == lang), None)
            if pick:
                break
        if pick:
            break
    pick = pick or items[0]
    snips = [{"start": s.start, "duration": s.duration, "text": s.text.replace("\n", " ").strip()}
             for s in pick.fetch()]
    kind = "自動產生字幕" if pick.is_generated else "上傳者提供字幕"
    return snips, f"{pick.language}（{pick.language_code}，{kind}）", ""


def paragraphs(snips, span=45):
    """把字幕片段依時間每 ~45 秒或停頓 >3 秒切段，保留段首時間戳。"""
    paras, cur, t0, last_end = [], [], None, 0
    for s in snips:
        if cur and (s["start"] - t0 > span or s["start"] - last_end > 3):
            paras.append((t0, "".join(cur) if _cjk(cur) else " ".join(cur)))
            cur, t0 = [], None
        if t0 is None:
            t0 = s["start"]
        cur.append(s["text"])
        last_end = s["start"] + s["duration"]
    if cur:
        paras.append((t0, "".join(cur) if _cjk(cur) else " ".join(cur)))
    return paras


def _cjk(parts):
    return sum(bool(re.search(r"[一-鿿]", p)) for p in parts) > len(parts) / 2


def storyboard_frames(info):
    """解析 storyboard 雪碧圖，切成 (秒數, 影像) 序列。這是 YouTube 播放器公開的預覽圖，不是影片檔。"""
    sbs = [f for f in info.get("formats", []) if f.get("format_note") == "storyboard"]
    if not sbs:
        return []
    sb = max(sbs, key=lambda f: f.get("width", 0))
    w, h, rows, cols = sb["width"], sb["height"], sb["rows"], sb["columns"]
    interval = 1 / sb["fps"] if sb.get("fps") else info["duration"] / (rows * cols)
    frames, idx = [], 0
    for frag in sb.get("fragments", []):
        data, _ = download(frag["url"])
        if not data:
            continue
        sheet = Image.open(io.BytesIO(data)).convert("RGB")
        for r in range(rows):
            for c in range(cols):
                t = idx * interval
                idx += 1
                if t > info["duration"]:
                    break
                tile = sheet.crop((c * w, r * h, (c + 1) * w, (r + 1) * h))
                if np.asarray(tile).std() > 8:  # 全黑/全白的轉場格不要
                    frames.append((t, tile))
    return frames


def _subtitle_at(snips, t, window=6):
    near = [s["text"] for s in snips if s["start"] - window <= t <= s["start"] + s["duration"] + window]
    return " ".join(near)[:120]


def pick_frames(frames, snips, n=5):
    """代表畫面挑選：影片切成 n 段，每段選分數最高的一格。
    分數 = 清晰度（Laplacian 變異數）+ 該時間字幕含地名/文化詞的加分；並排除與已選畫面太像的格。"""
    if not frames:
        return []
    dur = frames[-1][0] or 1
    n = min(n, max(3, len(frames) // 3)) if len(frames) >= 3 else len(frames)
    chosen, hists = [], []
    for k in range(n):
        lo, hi = dur * k / n, dur * (k + 1) / n
        seg = [f for f in frames if lo <= f[0] <= hi and f[0] > 2]
        scored = []
        for t, img in seg:
            arr = cv2.cvtColor(np.asarray(img), cv2.COLOR_RGB2GRAY)
            sharp = cv2.Laplacian(arr, cv2.CV_64F).var()
            sub = _subtitle_at(snips, t)
            bonus = sum(w in sub for w in PLACE_WORDS) * 150
            hist = cv2.calcHist([np.asarray(img)], [0, 1, 2], None, [8, 8, 8], [0, 256] * 3)
            hist = cv2.normalize(hist, hist).flatten()
            if any(cv2.compareHist(hist, h0, cv2.HISTCMP_CORREL) > 0.95 for h0 in hists):
                continue  # 跟已選畫面幾乎一樣
            scored.append((sharp + bonus, t, img, sub, hist))
        if scored:
            s, t, img, sub, hist = max(scored, key=lambda x: x[0])
            chosen.append({"t": t, "img": img, "subtitle": sub})
            hists.append(hist)
    return chosen
