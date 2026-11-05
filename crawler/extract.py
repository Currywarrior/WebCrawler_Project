"""從 HTML 擷取：中繼資料、連結清單、正文（去雜訊後轉 Markdown）、文章內圖片。"""
import json
import re
from urllib.parse import urljoin, urlparse, unquote

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

# 觀音主題關鍵字：用來判斷延伸連結是否相關
TOPIC_WORDS = ["觀音", "guanyin", "kuanyin", "白沙岬", "甘泉寺", "石觀音", "藻礁", "觀新", "草漯",
               "蓮花", "大潭", "大堀溪", "觀音溪", "樹林", "保障", "新坡", "崙坪", "白千層", "富林",
               "塔腳", "大園", "新屋", "海客", "石門水庫", "領海基點", "武威", "坑尾", "保生"]

DROP_TAGS = ["script", "style", "nav", "footer", "aside", "form", "button", "iframe",
             "select", "input", "svg", "canvas", "template", "header", "dialog"]

# class/id 拆成單字後，只要含有以下任何一個就視為雜訊區塊
NOISE_WORDS = {
    "nav", "navbar", "navigation", "menu", "menubar", "footer", "sidebar", "aside", "breadcrumb",
    "breadcrumbs", "share", "sharing", "sharebar", "social", "sns", "ad", "ads", "advert",
    "advertisement", "adsense", "adsbygoogle", "sponsor", "sponsored", "promo", "promotion",
    "cookie", "cookies", "consent", "gdpr", "subscribe", "newsletter", "popup", "modal",
    "related", "recommend", "recommended", "recommendation", "comment", "comments", "disqus",
    "login", "signup", "toolbar", "pagination", "pager", "gotop", "backtotop", "float",
    "floating", "fixed", "sticky", "widget", "tagcloud", "author-box", "copyright", "banner",
    "hot", "popular", "trending", "more-articles", "outbrain", "taboola", "ecommerce", "shop",
    # 維基百科專用
    "navbox", "mw-editsection", "catlinks", "noprint", "metadata", "ambox", "mw-jump-link",
    "sistersitebox", "vertical-navbox", "toc", "mw-indicators", "printfooter",
}
# 頁面文字中常見的站務雜訊句
NOISE_LINES = re.compile(
    r"(分享到|分享此|加入好友|追蹤我們|訂閱|按讚|立即下載|下載 ?app|廣告|贊助|copyright|©|版權所有|"
    r"all rights reserved|上一篇|下一篇|回到頂端|閱讀更多|看更多|留言|登入|註冊|使用 cookie|"
    r"cookie 政策|隱私權政策|本文出自|延伸閱讀|^tags?[:：]|^標籤|line 好友|facebook|instagram)",
    re.I)
# 遇到這些小標題代表正文結束、後面是推薦文章
TAIL_HEADINGS = re.compile(r"^(延伸閱讀|相關文章|推薦閱讀|你可能也喜歡|猜你喜歡|熱門文章|更多文章|"
                           r"看更多|相關新闻|相關新聞|最新文章|other posts|related posts)", re.I)

# 各網站的正文容器（先用已知規則，找不到再用通用評分）
SITE_MAIN = {
    "wikipedia.org": "#mw-content-text .mw-parser-output",
    "vocus.cc": "article",
    "smiletaiwan.cw.com.tw": ".article__content, .article-content, article",
    "supertaste.tvbs.com.tw": ".article__content, .article_content, article",
    "tw.news.yahoo.com": ".caas-body, article",
    "pixnet.net": "#article-content-inner, .article-content-inner, .article-content",
    "travel.tycg.gov.tw": "main",
    "ezgo.ardswc.gov.tw": "main, .content",
}
GENERIC_MAIN = ["[itemprop=articleBody]", "article .entry-content", ".entry-content",
                ".post-content", ".article-content", ".article-body", "article", "main",
                "[role=main]", "#content", ".content"]

BAD_IMG = re.compile(r"(logo|icon|avatar|sprite|pixel|tracking|blank|spacer|loading|placeholder|"
                     r"emoji|button|btn|share|facebook|fb_|line_|twitter|qrcode|qr-code|badge|"
                     r"doubleclick|googlesyndication|adservice|/ads?/|banner|gravatar|wpcom-smileys|"
                     r"\.svg(\?|$)|data:image)", re.I)
LAZY_ATTRS = ["data-src", "data-lazy-src", "data-original", "data-lazy", "data-url",
              "data-echo", "data-orig-file", "data-large-file"]


def _tokens(el):
    raw = " ".join(el.get("class", []) or []) + " " + (el.get("id") or "")
    raw = re.sub(r"([a-z])([A-Z])", r"\1 \2", raw).lower()
    parts = set(re.split(r"[\s\-_]+", raw))
    return parts | set(raw.split())


def _ptext_len(el):
    return sum(len(p.get_text(strip=True)) for p in el.find_all(["p", "li", "td"]))


def meta(soup, url):
    def m(*keys):
        for k in keys:
            t = soup.find("meta", attrs={"property": k}) or soup.find("meta", attrs={"name": k})
            if t and t.get("content"):
                return t["content"].strip()
        return ""
    info = {
        "title": m("og:title", "twitter:title") or (soup.title.get_text(strip=True) if soup.title else ""),
        "source_name": m("og:site_name", "application-name"),
        "author": m("author", "article:author", "dable:author"),
        "published": m("article:published_time", "pubdate", "publishdate", "date",
                       "og:article:published_time"),
        "description": m("og:description", "description"),
    }
    # JSON-LD 常帶作者與日期
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(s.string or "")
        except (ValueError, TypeError):
            continue
        for d in (data if isinstance(data, list) else data.get("@graph", [data])):
            if not isinstance(d, dict):
                continue
            info["published"] = info["published"] or str(d.get("datePublished", ""))
            a = d.get("author")
            if not info["author"] and a:
                a = a[0] if isinstance(a, list) else a
                info["author"] = a.get("name", "") if isinstance(a, dict) else str(a)
    # 沒有 og:title 時才用 h1（部落格的 h1 常是站名）；去掉「 - 維基百科」這類站名尾巴
    h1 = soup.find("h1")
    if not soup.find("meta", attrs={"property": "og:title"}) and h1 and len(h1.get_text(strip=True)) > 3:
        info["h1"] = h1.get_text(" ", strip=True)
    info["title"] = re.split(r"\s+[-|｜–]\s+(維基百科|桃園觀光導覽網|美食人生)", info["title"])[0]
    if not info["source_name"]:
        info["source_name"] = urlparse(url).netloc
    return info


def links(soup, base, main=None):
    """全頁超連結清單；標記是否在正文內、是否與觀音主題相關。"""
    in_main = set(id(a) for a in main.find_all("a")) if main else set()
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        u = urljoin(base, href).split("#")[0]
        if not u.startswith("http") or u in seen:
            continue
        seen.add(u)
        text = a.get_text(" ", strip=True)[:80]
        hay = (text + " " + unquote(u)).lower()
        out.append({"url": u, "text": text, "in_content": id(a) in in_main,
                    "related": any(w in hay for w in TOPIC_WORDS)})
    return out


def _fix_lazy(soup, base):
    """把延遲載入圖片的真正網址寫回 src，noscript 裡的備援圖也還原出來。"""
    for ns in soup.find_all("noscript"):
        inner = BeautifulSoup(ns.decode_contents(), "lxml").find("img")
        prev = ns.find_previous_sibling("img")
        if inner and inner.get("src"):
            if prev is not None:
                prev["src"] = inner["src"]
                ns.decompose()
            else:
                ns.replace_with(inner)
        else:
            ns.decompose()
    for img in soup.find_all("img"):
        real = next((img[a] for a in LAZY_ATTRS if img.get(a) and not img[a].startswith("data:")), None)
        srcset = img.get("data-srcset") or img.get("srcset")
        if not real and srcset:
            # srcset 取寬度最大那一張
            cands = [c.strip().split(" ") for c in srcset.split(",") if c.strip()]
            cands.sort(key=lambda c: int(re.sub(r"\D", "", c[1]) or 0) if len(c) > 1 else 0)
            real = cands[-1][0] if cands else None
        if real:
            img["src"] = real
        if img.get("src"):
            img["src"] = urljoin(base, img["src"])


def clean(soup):
    for c in soup.find_all(string=lambda s: isinstance(s, Comment)):
        c.extract()
    for t in soup.find_all(DROP_TAGS):
        if getattr(t, "decomposed", False):
            continue
        # ASP.NET 網站整頁包在 <form> 裡，只拆外殼不刪內容
        if t.name == "form":
            t.unwrap()
            continue
        # header 若是文章內小標頭（含 h1/h2 且不在 body 頂層）就保留
        if t.name == "header" and t.find(["h1", "h2"]) and t.find_parent("article"):
            continue
        t.decompose()
    total = _ptext_len(soup) or 1
    for el in soup.find_all(True):
        if getattr(el, "decomposed", False):
            continue
        if el.name in ("html", "body", "main", "article"):
            continue
        if _tokens(el) & NOISE_WORDS:
            # 保護：佔全頁段落文字 40% 以上的區塊不會是雜訊（例如 class="has-sidebar" 的外層）
            if _ptext_len(el) / total < 0.4:
                el.decompose()
    for el in soup.select('[style*="display:none"], [style*="display: none"], [hidden], [aria-hidden=true]'):
        if el.name != "img":
            el.decompose()


def find_main(soup, url):
    host = urlparse(url).netloc
    for site, sel in SITE_MAIN.items():
        if host.endswith(site):
            for s in sel.split(","):
                el = soup.select_one(s.strip())
                if el and _ptext_len(el) > 150:
                    return el, f"站點規則 {s.strip()}"
    for sel in GENERIC_MAIN:
        el = soup.select_one(sel)
        if el and _ptext_len(el) > 300:
            return el, f"通用規則 {sel}"
    # 通用評分：段落文字多、連結文字比例低的容器
    best, best_score = None, 0
    for el in soup.find_all(["div", "section", "td"]):
        text = _ptext_len(el)
        if text < 150:
            continue
        link_len = sum(len(a.get_text(strip=True)) for a in el.find_all("a"))
        density = link_len / max(len(el.get_text(strip=True)), 1)
        score = text * (1 - density) - 0.15 * len(el.find_all(["div", "section"]))
        if score > best_score:
            best, best_score = el, score
    return (best or soup.body or soup), "文字密度評分"


def _inline(el):
    txt = el.get_text(" ", strip=True) if isinstance(el, Tag) else str(el).strip()
    return re.sub(r"\s+", " ", txt).replace(" ，", "，").replace(" 。", "。")


def _table_md(t):
    rows = []
    for tr in t.find_all("tr"):
        cells = [_inline(c).replace("|", "／") for c in tr.find_all(["th", "td"])]
        if any(cells):
            rows.append(cells)
    if not rows:
        return ""
    w = max(len(r) for r in rows)
    rows = [r + [""] * (w - len(r)) for r in rows]
    md = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * w]
    md += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(md)


def _caption(img):
    fig = img.find_parent("figure")
    if fig and fig.find("figcaption"):
        return _inline(fig.find("figcaption"))
    wrap = img.find_parent(class_=re.compile("thumb|caption|wp-caption|image"))
    if wrap:
        c = wrap.find(class_=re.compile("caption|thumbcaption"))
        if c and c is not img:
            return _inline(c)
    return (img.get("alt") or img.get("title") or "").strip()


def to_blocks(main):
    """把正文 DOM 轉成依序排列的區塊：heading / para / list / table / image。"""
    blocks = []

    def walk(node):
        for ch in node.children:
            if isinstance(ch, NavigableString):
                t = str(ch).strip()
                if len(t) > 15 and node.name in ("div", "section", "article", "main", "td"):
                    blocks.append(("para", re.sub(r"\s+", " ", t)))
                continue
            if not isinstance(ch, Tag):
                continue
            n = ch.name
            if n in ("h1", "h2", "h3", "h4", "h5", "h6"):
                t = _inline(ch)
                if t:
                    blocks.append(("heading", (int(n[1]), t)))
            elif n == "img":
                blocks.append(("image", ch))
            elif n == "table":
                for img in ch.find_all("img"):
                    blocks.append(("image", img))
                md = _table_md(ch)
                if md:
                    blocks.append(("table", md))
            elif n in ("ul", "ol"):
                items = [_inline(li) for li in ch.find_all("li", recursive=False)]
                items = [i for i in items if i]
                for img in ch.find_all("img"):
                    blocks.append(("image", img))
                if items:
                    blocks.append(("list", items))
            elif n in ("p", "blockquote", "dd", "dt", "figcaption", "pre"):
                for img in ch.find_all("img"):
                    blocks.append(("image", img))
                t = _inline(ch)
                if t and n != "figcaption":
                    blocks.append(("quote" if n == "blockquote" else "para", t))
            elif n == "br":
                continue
            else:
                walk(ch)
    walk(main)
    return blocks


def candidate_images(blocks, base):
    """正文中的圖片候選，先用網址/屬性規則篩掉 logo、廣告、追蹤像素。"""
    out, seen = [], set()
    for kind, v in blocks:
        if kind != "image":
            continue
        src = v.get("src", "")
        if not src.startswith("http"):
            continue
        reason = ""
        if BAD_IMG.search(src) or BAD_IMG.search(" ".join(v.get("class", []) or [])):
            reason = "網址/屬性符合 logo、icon、廣告或社群按鈕規則"
        try:
            w, h = int(v.get("width", 999)), int(v.get("height", 999))
            if w < 100 or h < 80:
                reason = reason or f"標註尺寸過小 {w}x{h}（追蹤像素或圖示）"
        except ValueError:
            pass
        key = re.sub(r"/\d+px-", "/", src.split("?")[0])
        if key in seen:
            reason = reason or "同頁重複圖片"
        seen.add(key)
        out.append({"img": v, "src_url": src, "caption": _caption(v), "reject": reason})
    return out


def render_markdown(meta_info, blocks, image_map):
    """區塊序列轉 Markdown；圖片插在原文位置並標來源，維持文字與圖片的對照。"""
    lines, cut = [], False
    last_para = ""
    for kind, v in blocks:
        if cut:
            break
        if kind == "heading":
            lvl, t = v
            if TAIL_HEADINGS.search(t):
                cut = True
                continue
            lines.append("#" * min(lvl + 1, 6) + " " + t)
        elif kind in ("para", "quote"):
            if NOISE_LINES.search(v) and len(v) < 60:
                continue
            if v == last_para:
                continue
            last_para = v
            lines.append(("> " if kind == "quote" else "") + v)
        elif kind == "list":
            items = [i for i in v if not (NOISE_LINES.search(i) and len(i) < 40)]
            if items:
                lines.append("\n".join("- " + i for i in items))
        elif kind == "table":
            lines.append(v)
        elif kind == "image":
            im = image_map.get(id(v))
            if im:
                cap = im["caption"] or "（原文無圖片說明）"
                lines.append(f"![{cap}](../images/{im['file']})\n\n"
                             f"*圖：{cap}｜來源：{im['src_url']}*")
                im["context"] = last_para[:120]
    return "\n\n".join(lines)


def parse(html, url):
    soup = BeautifulSoup(html, "lxml")
    info = meta(soup, url)
    all_links_soup = BeautifulSoup(html, "lxml")  # 連結要在清理前從完整頁面擷取
    _fix_lazy(soup, url)
    clean(soup)
    main, how = find_main(soup, url)
    page_links = links(all_links_soup, url, None)
    in_main = {urljoin(url, a["href"]).split("#")[0] for a in main.find_all("a", href=True)}
    for l in page_links:
        l["in_content"] = l["url"] in in_main
    blocks = to_blocks(main)
    return info, blocks, page_links, how
