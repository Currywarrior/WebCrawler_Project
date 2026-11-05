"""任務 2 對照圖：原始網頁截圖 vs 清理後 Markdown 的渲染截圖。
用法：python crawler/screenshots.py page_001 page_005 page_023 ..."""
import html
import os
import re
import sys

from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "output")
SHOT = os.path.join(ROOT, "report", "screenshots")


def md_to_html(md):
    """極簡 Markdown 渲染，只為了截圖看清理結果。"""
    out = []
    for block in md.split("\n\n"):
        b = block.strip()
        m = re.match(r"!\[(.*?)\]\((.*?)\)", b)
        if m:
            src = os.path.abspath(os.path.join(OUT, "articles", m.group(2))).replace("\\", "/")
            out.append(f'<img src="file:///{src}" style="max-width:100%">')
        elif b.startswith("#"):
            lvl = len(b) - len(b.lstrip("#"))
            out.append(f"<h{lvl}>{html.escape(b.lstrip('# '))}</h{lvl}>")
        elif b.startswith("|"):
            rows = [r for r in b.splitlines() if not r.startswith("|---")]
            out.append("<table border=1 cellspacing=0>" + "".join(
                "<tr>" + "".join(f"<td>{html.escape(c.strip())}</td>" for c in r.strip("|").split("|")) + "</tr>"
                for r in rows) + "</table>")
        elif b.startswith("- "):
            out.append("<ul>" + "".join(f"<li>{html.escape(l[2:])}</li>" for l in b.splitlines()) + "</ul>")
        else:
            out.append(f"<p>{html.escape(b)}</p>")
    return ("<html><head><meta charset=utf-8><style>body{font-family:'Microsoft JhengHei';"
            "max-width:820px;margin:24px auto;line-height:1.7;color:#222}em,i{color:#666}</style>"
            "</head><body>" + "\n".join(out) + "</body></html>")


def main(ids):
    import json
    docs = {json.loads(l)["id"]: json.loads(l) for l in open(os.path.join(OUT, "index.jsonl"), encoding="utf-8")}
    os.makedirs(SHOT, exist_ok=True)
    with sync_playwright() as p:
        b = p.chromium.launch()
        for pid in ids:
            d = docs[pid]
            pg = b.new_page(viewport={"width": 1280, "height": 900}, ignore_https_errors=True, locale="zh-TW")
            pg.goto(d["source_url"], timeout=60000, wait_until="domcontentloaded")
            pg.wait_for_timeout(5000)
            pg.screenshot(path=os.path.join(SHOT, f"{pid}_original.png"), full_page=True, timeout=60000)
            md = open(os.path.join(OUT, "articles", f"{pid}.md"), encoding="utf-8").read()
            tmp = os.path.join(SHOT, f"_{pid}.html")
            open(tmp, "w", encoding="utf-8").write(md_to_html(md))
            pg.goto("file:///" + tmp.replace("\\", "/"))
            pg.wait_for_timeout(800)
            pg.screenshot(path=os.path.join(SHOT, f"{pid}_cleaned.png"), full_page=True)
            os.remove(tmp)
            pg.close()
            print(pid, "ok")
        b.close()


if __name__ == "__main__":
    main(sys.argv[1:])
