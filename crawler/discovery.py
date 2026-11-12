"""可擴充的外部來源發現器。

這個模組把「找新來源」和「解析文章」分開：發現器只回傳網址或結構化資料，
真正的 HTML 擷取仍交給 crawler.py。所有 HTTP 呼叫都使用公開 API、逾時和
User-Agent；API 暫時不可用時回傳空結果，不會中斷既有種子爬取。
"""
from __future__ import annotations

import csv
import json
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import requests


UA = "GuanyinCrawler/2.0 (educational project; source discovery)"
TIMEOUT = 20
DEFAULT_LIMIT = 30
_last_request = 0.0


def _get(url: str, params: dict | None = None) -> requests.Response:
    """小幅限速的 GET；發現器不使用瀏覽器，也不繞過登入或驗證。"""
    global _last_request
    gap = time.time() - _last_request
    if gap < 0.5:
        time.sleep(0.5 - gap)
    response = requests.get(
        url,
        params=params,
        headers={"User-Agent": UA, "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.6"},
        timeout=TIMEOUT,
    )
    _last_request = time.time()
    response.raise_for_status()
    return response


def _row(url: str, title: str, provider: str, kind: str = "discovered", **extra) -> dict:
    return {
        "url": url,
        "title": title.strip(),
        "provider": provider,
        "kind": kind,
        **extra,
    }


def discover_wikipedia(query: str, limit: int = DEFAULT_LIMIT, language: str = "zh") -> tuple[list[dict], list[dict]]:
    """用 MediaWiki 公開搜尋 API 找文章，不解析搜尋引擎結果頁。"""
    endpoint = f"https://{language}.wikipedia.org/w/api.php"
    try:
        data = _get(endpoint, {
            "action": "query", "list": "search", "srsearch": query,
            "srlimit": min(limit, 50), "format": "json", "utf8": 1,
        }).json()
    except (requests.RequestException, ValueError):
        return [], []
    rows, related = [], []
    for item in data.get("query", {}).get("search", []):
        title = item.get("title", "").strip()
        if not title or title.startswith(("Special:", "Talk:")):
            continue
        url = f"https://{language}.wikipedia.org/wiki/{quote(title.replace(' ', '_'), safe='')}"
        rows.append(_row(url, title, "wikipedia", query=query))
        related.append({"provider": "wikipedia", "query": query, "title": title,
                        "snippet": re.sub(r"<[^>]+>", "", item.get("snippet", "")),
                        "url": url})
    return rows, related


def discover_commons(query: str, limit: int = DEFAULT_LIMIT) -> tuple[list[dict], list[dict]]:
    """找 Wikimedia Commons 檔案頁，並保存基本授權與尺寸中繼資料。"""
    endpoint = "https://commons.wikimedia.org/w/api.php"
    try:
        data = _get(endpoint, {
            "action": "query", "generator": "search", "gsrsearch": query,
            "gsrnamespace": 6, "gsrlimit": min(limit, 50),
            "prop": "imageinfo", "iiprop": "url|extmetadata|size",
            "iiurlwidth": 1600, "format": "json", "utf8": 1,
        }).json()
    except (requests.RequestException, ValueError):
        return [], []
    rows, related = [], []
    pages = data.get("query", {}).get("pages", {}).values()
    for item in pages:
        title = item.get("title", "").strip()
        if not title:
            continue
        imageinfo = (item.get("imageinfo") or [{}])[0]
        metadata = imageinfo.get("extmetadata") or {}
        url = imageinfo.get("descriptionurl") or (
            "https://commons.wikimedia.org/wiki/" + quote(title.replace(" ", "_"), safe=""))
        related_record = {
            "provider": "wikimedia_commons", "query": query,
            "title": title, "url": url,
            "download_url": imageinfo.get("thumburl") or imageinfo.get("url", ""),
            "width": imageinfo.get("width", 0), "height": imageinfo.get("height", 0),
            "license": (metadata.get("LicenseShortName") or {}).get("value", ""),
            "artist": (metadata.get("Artist") or {}).get("value", ""),
            "attribution": (metadata.get("Attribution") or {}).get("value", ""),
        }
        rows.append(_row(url, title, "wikimedia_commons", kind="media", data=related_record))
        related.append(related_record)
    return rows, related


def discover_osm(bbox: str, limit: int = DEFAULT_LIMIT) -> tuple[list[dict], list[dict]]:
    """透過 Overpass 公開 API 找觀音區的景點、道路與文化設施。"""
    endpoint = "https://overpass-api.de/api/interpreter"
    # bbox 格式 south,west,north,east；限制節點、面積與回傳欄位，避免大量資料。
    query = f'[out:json][timeout:25];(nwr["name"]({bbox}););out center;'
    try:
        data = _get(endpoint, {"data": query}).json()
    except (requests.RequestException, ValueError):
        return [], []
    rows, related = [], []
    for item in data.get("elements", [])[:limit]:
        tags = item.get("tags") or {}
        name = tags.get("name") or tags.get("name:zh") or "OSM 未命名地點"
        osm_type, osm_id = item.get("type", ""), item.get("id", "")
        if not osm_type or not osm_id:
            continue
        url = f"https://www.openstreetmap.org/{osm_type}/{osm_id}"
        center = item.get("center") or item
        record = {
            "provider": "openstreetmap", "name": name,
            "osm_type": osm_type, "osm_id": osm_id,
            "latitude": center.get("lat"), "longitude": center.get("lon"),
            "tags": tags, "url": url,
        }
        rows.append(_row(url, name, "openstreetmap", kind="poi", data=record))
        related.append(record)
    return rows, related


def fetch_open_meteo(latitude: float, longitude: float) -> dict | None:
    """取得免 API key 的觀音座標當下天氣，作為與地點相關的時間資料。"""
    try:
        data = _get("https://api.open-meteo.com/v1/forecast", {
            "latitude": latitude, "longitude": longitude,
            "current": "temperature_2m,relative_humidity_2m,precipitation,wind_speed_10m",
            "timezone": "Asia/Taipei",
        }).json()
    except (requests.RequestException, ValueError):
        return None
    return {
        "provider": "open_meteo", "latitude": latitude, "longitude": longitude,
        "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "timezone": data.get("timezone"), "current": data.get("current", {}),
        "units": data.get("current_units", {}),
    }


def discover_sitemap(url: str, limit: int = DEFAULT_LIMIT) -> tuple[list[dict], list[dict]]:
    """讀取網站公開 sitemap，適合沒有搜尋 API 的政府網站。"""
    try:
        root = ET.fromstring(_get(url).content)
    except (requests.RequestException, ET.ParseError):
        return [], []
    namespace = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    urls = [el.text.strip() for el in root.findall(".//sm:loc", namespace) if el.text]
    # 某些 sitemap 沒有 namespace。
    urls += [el.text.strip() for el in root.findall(".//loc") if el.text]
    rows = [_row(u, u, "sitemap", sitemap=url) for u in list(dict.fromkeys(urls))[:limit]]
    return rows, [{"provider": "sitemap", "sitemap": url, "url": r["url"]} for r in rows]


def discover_from_config(config_path: str | Path, topic: str, limit: int = DEFAULT_LIMIT) -> tuple[list[dict], list[dict]]:
    """依 CSV 設定執行多個 provider；新增 provider 只需擴充此函式與設定檔。"""
    rows, related = [], []
    path = Path(config_path)
    if not path.exists():
        return rows, related
    with path.open(encoding="utf-8-sig", newline="") as f:
        configs = csv.DictReader(f)
        for cfg in configs:
            if str(cfg.get("enabled", "1")).strip().lower() in {"0", "false", "no"}:
                continue
            provider = (cfg.get("provider") or "").strip().lower()
            query = (cfg.get("query") or topic).replace("{topic}", topic).strip()
            try:
                n = min(int(cfg.get("limit") or limit), limit)
            except ValueError:
                n = limit
            try:
                if provider == "wikipedia":
                    found, data = discover_wikipedia(query, n, cfg.get("language") or "zh")
                elif provider == "commons":
                    found, data = discover_commons(query, n)
                elif provider == "openstreetmap":
                    found, data = discover_osm(cfg.get("bbox") or "24.96,121.00,25.10,121.18", n)
                elif provider == "sitemap":
                    found, data = discover_sitemap(cfg.get("endpoint", ""), n)
                else:
                    continue
            except (ValueError, TypeError):
                continue
            rows.extend(found)
            related.extend(data)
    # 相同 URL 只保留一次，並保留最早 provider。
    unique = {}
    for row in rows:
        unique.setdefault(row["url"], row)
    return list(unique.values()), related


def write_discovery_outputs(outdir: str | Path, rows: list[dict], related: list[dict], weather: dict | None = None) -> None:
    """把發現結果存成可檢視的 CSV/JSONL，讓 API 資料與文章一樣可追溯。"""
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    with (out / "discovered_sources.csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, ["url", "title", "provider", "kind", "query", "sitemap"])
        writer.writeheader()
        writer.writerows({key: row.get(key, "") for key in writer.fieldnames} for row in rows)
    records = list(related)
    if weather:
        records.append(weather)
    with (out / "related_data.jsonl").open("w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
