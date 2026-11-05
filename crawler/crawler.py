"""觀音地方知識爬蟲主程式。

用法：
  python crawler/crawler.py                 # 爬 urls.csv 全部網址 + 一層延伸探索
  python crawler/crawler.py --only 5,26     # 只重跑 urls.csv 第 5、26 筆（現場展示用）
  python crawler/crawler.py --url <網址>    # 臨時爬任一網址，輸出到 output/demo/
"""
import argparse
import csv
import hashlib
import io
import json
import os
import sys
from datetime import datetime
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

import imagehash
from PIL import Image

sys.path.insert(0, os.path.dirname(__file__))
import extract  # noqa: E402
import fetch  # noqa: E402
import video  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "output")

# 延伸探索限制：深度 1、每個種子最多 3 個相關連結、全部最多 30 頁、只限下列站點範圍
EXT_PER_SEED = 3
EXT_TOTAL = 30
EXT_DOMAINS = ("wikipedia.org", "gov.tw", "travel.tycg.gov.tw", "culture.tw")
SKIP_EXT = (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".zip", ".doc", ".docx", ".xls", ".mp4")
MAX_IMAGES = 15
MIN_SIDE = 150

REPORT_FIELDS = ["no", "id", "type", "depth", "parent_id", "url", "final_url", "method",
                 "http_status", "crawl_status", "reason", "title", "text_chars", "n_images",
                 "n_links", "crawled_at"]

_img_hashes = {}  # sha1 / phash → 已存檔名，跨頁去重


def normalize(url):
    """去掉追蹤參數（si、utm_*、guccounter…），用於去重。"""
    p = urlparse(url.strip())
    q = [(k, v) for k, v in parse_qsl(p.query) if not k.startswith(("utm_", "si", "guc", "fbclid"))]
    return urlunparse((p.scheme, p.netloc.lower(), p.path, "", urlencode(q), ""))


def now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def save_image(cand, page_id, k, referer, img_dir):
    data, ctype = fetch.download(cand["src_url"], referer)
    if not data:
        return None, f"下載失敗（{ctype}）"
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
    except Exception:
        return None, "不是可解析的圖片"
    w, h = im.size
    if min(w, h) < MIN_SIDE:
        return None, f"實際尺寸過小 {w}x{h}"
    if max(w, h) / min(w, h) > 4:
        return None, f"長寬比 {w}x{h} 過扁，判定為橫幅 Banner"
    sha = hashlib.sha1(data).hexdigest()
    ph = str(imagehash.phash(im.convert("RGB")))
    for key in (sha, ph):
        if key in _img_hashes:
            return {"file": _img_hashes[key], "duplicate": True}, None
    name = f"{page_id}_image_{k:02d}.jpg"
    im.convert("RGB").save(os.path.join(img_dir, name), "JPEG", quality=88)
    _img_hashes[sha] = _img_hashes[ph] = name
    return {"file": name, "duplicate": False, "size": f"{w}x{h}"}, None


def crawl_page(no, url, page_id, depth=0, parent=None, outdir=OUT):
    rec = {"no": no, "id": page_id, "type": "webpage", "depth": depth, "parent_id": parent or "",
           "url": url, "crawled_at": now()}
    res = fetch.fetch(url)
    rec.update(final_url=res.get("final_url", url), method=res.get("method"),
               http_status=res.get("status"))
    reason = res.get("note", "")
    if not res.get("html"):
        rec.update(crawl_status=res.get("kind", "無法存取"), reason=res["error"])
        return rec, None
    info, blocks, page_links, how = extract.parse(res["html"], res["final_url"])
    title = info.get("h1") or info["title"]

    img_dir = os.path.join(outdir, "images")
    images, image_map, rejected, k = [], {}, [], 0
    for c in extract.candidate_images(blocks, res["final_url"]):
        if c["reject"]:
            rejected.append({"src_url": c["src_url"], "reason": c["reject"]})
            continue
        if k >= MAX_IMAGES:
            rejected.append({"src_url": c["src_url"], "reason": f"超過每頁上限 {MAX_IMAGES} 張"})
            continue
        saved, why = save_image(c, page_id, k + 1, res["final_url"], img_dir)
        if not saved:
            rejected.append({"src_url": c["src_url"], "reason": why})
            continue
        if not saved["duplicate"]:
            k += 1
        entry = {"file": saved["file"], "caption": c["caption"], "src_url": c["src_url"],
                 "duplicate_of_existing": saved["duplicate"], "context": ""}
        images.append(entry)
        image_map[id(c["img"])] = entry

    body = extract.render_markdown(info, blocks, image_map)
    text_only = "\n".join(l for l in body.splitlines() if not l.startswith(("![", "*圖：")))
    status = "成功"
    if rec["http_status"] and rec["http_status"] >= 400:
        status, reason = "失敗", f"HTTP {rec['http_status']}：{title}"
    elif len(text_only) < 80:
        status, reason = "失敗", (reason + "；" if reason else "") + "取得頁面但擷取不到正文（<80 字）"
    elif len(text_only) < 200:
        status = "成功（內容極短）"
        reason = (reason + "；" if reason else "") + "正文少於 200 字，需對照原網頁確認是內容本身短還是擷取遺漏"

    header = [f"# {title}", "",
              f"- 原始網址：{url}", f"- 網站名稱：{info['source_name']}",
              f"- 作者：{info['author'] or '（未標示）'}",
              f"- 發表日期：{info['published'] or '（未標示）'}",
              f"- 爬取時間：{rec['crawled_at']}　取得方式：{rec['method']}　正文定位：{how}",
              f"- 圖片：{len(images)} 張" + ("" if images else "（正文內沒有可取得或適合保留的圖片）"),
              "", "---", ""]
    md = "\n".join(header) + body
    with open(os.path.join(outdir, "articles", f"{page_id}.md"), "w", encoding="utf-8") as f:
        f.write(md + "\n")
    with open(os.path.join(outdir, "links", f"{page_id}_links.csv"), "w", newline="",
              encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, ["url", "text", "in_content", "related"])
        w.writeheader()
        w.writerows(page_links)

    rec.update(crawl_status=status, reason=reason, title=title, text_chars=len(text_only),
               n_images=len(images), n_links=len(page_links))
    doc = {
        "id": page_id, "type": "webpage", "title": title, "source_url": url,
        "final_url": rec["final_url"], "source_name": info["source_name"],
        "author": info["author"], "published": info["published"],
        "content": text_only.strip(), "content_file": f"articles/{page_id}.md",
        "images": images, "images_rejected": rejected,
        "links": [l for l in page_links if l["in_content"] or l["related"]][:150],
        "links_file": f"links/{page_id}_links.csv", "links_total": len(page_links),
        "timestamps": [], "crawl_status": status, "crawl_note": reason,
        "depth": depth, "parent_id": parent, "crawled_at": rec["crawled_at"],
    }
    return rec, doc


def crawl_video(no, url, vid_id, outdir=OUT):
    rec = {"no": no, "id": vid_id, "type": "video", "depth": 0, "parent_id": "", "url": url,
           "final_url": url, "method": "yt-dlp(僅中繼資料)+youtube-transcript-api",
           "crawled_at": now()}
    yid = video.video_id(url)
    try:
        info = video.metadata(yid)
    except Exception as e:
        rec.update(crawl_status="無法存取", reason=f"中繼資料取得失敗：{str(e)[:150]}")
        return rec, None
    snips, lang, terr = video.transcript(yid)
    if terr:
        terr += "；本組未取得該影片音訊的使用授權，依作業規範不下載音訊做語音辨識"

    fdir = os.path.join(outdir, "videos", f"{vid_id}_frames")
    os.makedirs(fdir, exist_ok=True)
    picks = video.pick_frames(video.storyboard_frames(info), snips)
    frames = []
    for i, p in enumerate(picks, 1):
        name = f"{vid_id}_frame_{i:02d}_{video.hms(p['t']).replace(':', '')}.jpg"
        p["img"].save(os.path.join(fdir, name), "JPEG", quality=92)
        frames.append({"time": video.hms(p["t"]), "seconds": round(p["t"], 1),
                       "file": f"videos/{vid_id}_frames/{name}",
                       "subtitle": p["subtitle"] or "（此時間點無字幕）", "description": ""})

    paras = video.paragraphs(snips) if snips else []
    up = info.get("upload_date") or ""
    up = f"{up[:4]}-{up[4:6]}-{up[6:]}" if len(up) == 8 else up
    md = [f"# {info.get('title')}", "",
          f"- 影片網址：{url}", f"- 頻道：{info.get('channel') or info.get('uploader')}",
          f"- 上傳日期：{up}　長度：{video.hms(info.get('duration') or 0)}　觀看數：{info.get('view_count')}",
          f"- 字幕來源：{lang or '無'}", f"- 爬取時間：{rec['crawled_at']}", "",
          "## 影片說明", "", (info.get("description") or "（無）").strip(), "",
          "## 代表畫面", "",
          "> 畫面取自 YouTube 播放器公開提供的 storyboard 預覽圖（320x180），未下載影片檔、未繞過任何平台限制。", ""]
    for fr in frames:
        md += [f"![{fr['time']}](./{os.path.basename(fdir)}/{os.path.basename(fr['file'])})", "",
               f"**{fr['time']}**　字幕：{fr['subtitle']}", ""]
    md += ["## 逐字稿", ""]
    if paras:
        md += [f"**[{video.hms(t)}]** {txt}\n" for t, txt in paras]
    else:
        md += [f"無法取得文字：{terr}"]
    with open(os.path.join(outdir, "videos", f"{vid_id}.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(md) + "\n")

    content = "\n".join(txt for _, txt in paras)
    status = "成功" if content else "成功（無字幕）"
    rec.update(crawl_status=status, reason=terr, title=info.get("title"),
               text_chars=len(content), n_images=len(frames), n_links=0)
    doc = {
        "id": vid_id, "type": "video", "title": info.get("title"), "source_url": url,
        "final_url": info.get("webpage_url"), "source_name": info.get("channel"),
        "author": info.get("uploader"), "published": up, "duration": info.get("duration"),
        "description": info.get("description"), "tags": info.get("tags"),
        "content": content, "content_file": f"videos/{vid_id}.md", "subtitle_language": lang,
        "images": frames, "links": [],
        "timestamps": [{"start": round(s["start"], 2), "time": video.hms(s["start"]),
                        "text": s["text"]} for s in snips],
        "frame_timestamps": [f["time"] for f in frames],
        "crawl_status": status, "crawl_note": terr, "depth": 0, "parent_id": None,
        "crawled_at": rec["crawled_at"],
    }
    return rec, doc


def pick_extensions(doc, seen):
    seed_host = urlparse(doc["final_url"]).netloc
    out = []
    for l in sorted(doc["links"], key=lambda l: (not l["in_content"], not l["related"])):
        u = normalize(l["url"])
        host = urlparse(u).netloc
        if not l["related"] or u in seen or u.lower().endswith(SKIP_EXT):
            continue
        if not (host == seed_host or host.endswith(EXT_DOMAINS)):
            continue
        if host.endswith("wikipedia.org") and (":" in urlparse(u).path.split("/")[-1]
                                               or "action=" in u):
            continue  # 維基的分類、檔案、編輯頁不算文章
        out.append((u, l["text"]))
        seen.add(u)
        if len(out) >= EXT_PER_SEED:
            break
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="只跑 urls.csv 中的這些編號，例如 5,26")
    ap.add_argument("--url", help="臨時爬一個網址（展示用），輸出到 output/demo/")
    ap.add_argument("--no-extend", action="store_true", help="不做延伸探索")
    a = ap.parse_args()

    outdir = os.path.join(OUT, "demo") if a.url else OUT
    for d in ("articles", "images", "videos", "links"):
        os.makedirs(os.path.join(outdir, d), exist_ok=True)

    if a.url:
        rows = [{"no": "demo", "url": a.url}]
    else:
        with open(os.path.join(ROOT, "urls.csv"), encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    # 去重：正規化後相同的網址只爬一次，但每一筆仍寫進紀錄
    seen, uniq, dups = set(), [], []
    for r in rows:
        u = normalize(r["url"])
        (dups if u in seen else uniq).append(r)
        seen.add(u)
    only = set(a.only.split(",")) if a.only else None

    # 編號固定：網頁 page_001…、影片 video_001…，依 urls.csv 順序，局部重跑時編號不變
    np_, nv = 0, 0
    jobs = []
    for r in uniq:
        is_vid = "youtu" in urlparse(r["url"]).netloc
        if is_vid:
            nv += 1
            jid = f"video_{nv:03d}"
        else:
            np_ += 1
            jid = f"page_{np_:03d}"
        if a.url:
            jid = "demo_" + jid
        if only is None or r["no"] in only:
            jobs.append((r, is_vid, jid))

    records, docs = [], []
    for r, is_vid, jid in jobs:
        print(f"[{r['no']}] {jid} {r['url'][:80]}", flush=True)
        if is_vid:
            rec, doc = crawl_video(r["no"], r["url"], jid, outdir)
        else:
            rec, doc = crawl_page(r["no"], r["url"], jid, outdir=outdir)
        print(f"     → {rec['crawl_status']} {rec.get('reason', '')[:80]}", flush=True)
        records.append(rec)
        if doc:
            docs.append(doc)
    for r in dups:
        records.append({"no": r["no"], "id": "-", "url": r["url"], "crawl_status": "略過",
                        "reason": "與前面網址重複", "crawled_at": now()})

    if not a.no_extend and not a.url and only is None:
        ext_n = 0
        for doc in list(docs):
            if doc["type"] != "webpage" or ext_n >= EXT_TOTAL:
                continue
            for u, text in pick_extensions(doc, seen):
                if ext_n >= EXT_TOTAL:
                    break
                ext_n += 1
                eid = f"ext_{ext_n:03d}"
                print(f"  [ext] {eid} ← {doc['id']} {text[:20]} {u[:70]}", flush=True)
                rec, d = crawl_page(f"E{ext_n}", u, eid, depth=1, parent=doc["id"])
                records.append(rec)
                if d:
                    docs.append(d)
    fetch.close_browser()

    # 局部重跑時合併進既有的 index / report，不覆蓋其他筆
    idx_path = os.path.join(outdir, "index.jsonl")
    rep_path = os.path.join(outdir, "crawl_report.csv")
    if only is not None:
        new_ids = {d["id"] for d in docs} | {r["id"] for r in records}
        if os.path.exists(idx_path):
            old = [json.loads(l) for l in open(idx_path, encoding="utf-8")]
            docs = [d for d in old if d["id"] not in new_ids] + docs
        if os.path.exists(rep_path):
            old = list(csv.DictReader(open(rep_path, encoding="utf-8-sig")))
            records = [r for r in old if r["id"] not in new_ids] + records
    with open(idx_path, "w", encoding="utf-8") as f:
        for d in docs:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
    with open(rep_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, REPORT_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(sorted(records, key=lambda r: (str(r["id"]).startswith("ext"), str(r["id"]))))
    print(f"完成：{len(docs)} 筆資料，紀錄 {len(records)} 筆 → {outdir}")


if __name__ == "__main__":
    main()
