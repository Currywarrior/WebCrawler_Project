"""取得網頁 HTML：requests 優先，內容太少或 SSL 失敗時改用 Playwright（真瀏覽器渲染）。"""
import time
import urllib.robotparser
from urllib.parse import urlparse

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/141.0 Safari/537.36 GuanyinCrawler/1.0 (educational project)")
HEADERS = {"User-Agent": UA, "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.6"}
DELAY = 1.0  # 同一站點兩次請求間隔（秒），避免造成對方負擔

# 已知是前端 JS 渲染（SPA）的站點：靜態 HTML 只有空殼，直接用瀏覽器
JS_SITES = {"travel.tycg.gov.tw"}

_last_hit = {}
_robots = {}
_browser = None
_pw = None


def _wait_turn(host):
    gap = time.time() - _last_hit.get(host, 0)
    if gap < DELAY:
        time.sleep(DELAY - gap)
    _last_hit[host] = time.time()


def robots_allowed(url):
    """依 robots.txt 判斷是否允許爬取；robots.txt 本身讀不到時視為允許。"""
    p = urlparse(url)
    base = f"{p.scheme}://{p.netloc}"
    if base not in _robots:
        rp = urllib.robotparser.RobotFileParser()
        try:
            r = requests.get(base + "/robots.txt", headers=HEADERS, timeout=10, verify=False)
            rp.parse(r.text.splitlines() if r.status_code == 200 else [])
        except requests.RequestException:
            rp.parse([])
        _robots[base] = rp
    return _robots[base].can_fetch(UA, url)


def _get_browser():
    global _browser, _pw
    if _browser is None:
        from playwright.sync_api import sync_playwright
        _pw = sync_playwright().start()
        _browser = _pw.chromium.launch()
    return _browser


def close_browser():
    global _browser, _pw
    if _browser:
        _browser.close()
        _pw.stop()
    _browser = _pw = None


def fetch_browser(url, screenshot=None):
    """用 Chromium 開頁、捲動到底觸發 lazy loading，回傳渲染後 HTML。"""
    page = _get_browser().new_page(viewport={"width": 1280, "height": 900},
                                   user_agent=UA, ignore_https_errors=True,
                                   locale="zh-TW")
    try:
        resp = page.goto(url, timeout=60000, wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        # 分段捲動，讓延遲載入的圖片把真正網址寫進 src
        for _ in range(12):
            page.mouse.wheel(0, 1500)
            page.wait_for_timeout(300)
        page.wait_for_timeout(800)
        if screenshot:
            page.evaluate("window.scrollTo(0,0)")
            page.screenshot(path=screenshot, full_page=True)
        return {"html": page.content(), "status": resp.status if resp else 0,
                "final_url": page.url, "method": "playwright"}
    finally:
        page.close()


def fetch(url):
    """回傳 dict(html, status, final_url, method, note)；失敗時 html=None 並附 error。"""
    host = urlparse(url).netloc
    if not robots_allowed(url):
        return {"html": None, "status": 0, "final_url": url, "method": "-",
                "error": "robots.txt 不允許爬取", "kind": "無法存取"}
    _wait_turn(host)
    note = ""
    if host not in JS_SITES:
        try:
            r = requests.get(url, headers=HEADERS, timeout=25)
        except requests.exceptions.SSLError:
            # 政府網站常見：伺服器沒送中繼憑證，Python 驗不過但瀏覽器可以，改用瀏覽器
            note = "requests SSL 憑證鏈驗證失敗，改用瀏覽器取得"
            r = None
        except requests.RequestException as e:
            return {"html": None, "status": 0, "final_url": url, "method": "requests",
                    "error": f"{type(e).__name__}: {str(e)[:150]}", "kind": "無法存取"}
        if r is not None:
            if r.encoding in (None, "ISO-8859-1"):
                r.encoding = r.apparent_encoding
            res = {"html": r.text, "status": r.status_code, "final_url": r.url,
                   "method": "requests", "note": note}
            if r.status_code in (401, 403, 451):
                res.update(error=f"HTTP {r.status_code} 網站拒絕存取", kind="無法存取")
            elif r.status_code >= 400:
                res.update(error=f"HTTP {r.status_code}", kind="失敗")
            return res
    else:
        note = "已知 JS 渲染站點，直接用瀏覽器"
    try:
        res = fetch_browser(url)
        res["note"] = note
        if res["status"] >= 400:
            res.update(error=f"HTTP {res['status']}", kind="失敗")
        return res
    except Exception as e:
        return {"html": None, "status": 0, "final_url": url, "method": "playwright",
                "error": f"{note}；瀏覽器也失敗：{type(e).__name__}: {str(e)[:120]}",
                "kind": "無法存取"}


def download(url, referer=None):
    """下載圖片等二進位資源，回傳 (bytes, content_type) 或 (None, 錯誤原因)。"""
    host = urlparse(url).netloc
    _wait_turn(host)
    h = dict(HEADERS)
    if referer:
        h["Referer"] = referer
    try:
        r = requests.get(url, headers=h, timeout=25, verify=False)
    except requests.RequestException as e:
        return None, type(e).__name__
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}"
    return r.content, r.headers.get("Content-Type", "")
