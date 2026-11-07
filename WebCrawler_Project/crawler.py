"""Collect article and video metadata from the seed URLs in urls.csv."""

from __future__ import annotations

import argparse
import csv
import html
import json
import re
import sys
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlencode, urljoin, urlparse
from urllib.request import Request, urlopen


PROJECT_DIR = Path(__file__).resolve().parent
VENDOR_DIR = PROJECT_DIR / "vendor"
URLS_FILE = PROJECT_DIR / "urls.csv"
OUTPUT_DIR = PROJECT_DIR / "output"
API_URL = "https://zh.wikipedia.org/w/api.php"
USER_AGENT = "CourseworkWikiCrawler/1.0 (educational demonstration)"
TIMEOUT_SECONDS = 25
MAX_WORKERS = 4
WIKIPEDIA_SEMAPHORE = threading.Semaphore(1)
IMAGE_QUERY_BATCH = 50
MAX_GENERIC_IMAGES_PER_SOURCE = 8
SUBTITLE_LANG_PREFERENCE = ("zh-Hant", "zh-TW", "zh", "en")
MIN_IMAGE_WIDTH = 220
MIN_IMAGE_HEIGHT = 140
EXCLUDED_IMAGE_WORDS = (
    "logo",
    "icon",
    "edit",
    "wikivoyage-logo",
    "commons-logo",
    "taiwan hakka",
    "tw phw",
    "tw chw",
    "banner",
    "avatar",
    "sprite",
    "button",
    "badge",
    "qrcode",
)


def fetch_json(url: str) -> dict:
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    for attempt in range(4):
        try:
            with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                data = json.loads(response.read().decode("utf-8"))
            if urlparse(url).hostname == "zh.wikipedia.org":
                time.sleep(0.3)
            return data
        except HTTPError as error:
            if error.code != 429 and not 500 <= error.code < 600 or attempt == 3:
                raise
            time.sleep(2 * (attempt + 1))
        except URLError as error:
            if attempt == 3 or "timed out" not in str(error).lower():
                raise
            time.sleep(2 * (attempt + 1))
    raise RuntimeError("JSON request retries exhausted")


def fetch_bytes(url: str) -> bytes:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        return response.read()


def title_from_wikipedia_url(source_url: str) -> str:
    parsed = urlparse(source_url)
    if parsed.hostname not in {"zh.wikipedia.org", "zh.m.wikipedia.org"}:
        raise ValueError("This starter crawler accepts zh.wikipedia.org article URLs only.")
    if not parsed.path.startswith(("/wiki/", "/zh-tw/")):
        raise ValueError("The URL does not look like a Wikipedia article URL.")
    raw_title = parsed.path.rsplit("/", 1)[-1]
    title = unquote(raw_title).replace("_", " ").strip()
    if not title:
        raise ValueError("Could not read an article title from the URL.")
    return title


def api_url(params: dict[str, str]) -> str:
    return f"{API_URL}?{urlencode(params)}"


def query_article(title: str) -> dict:
    payload = fetch_json(
        api_url(
            {
                "action": "query",
                "format": "json",
                "formatversion": "2",
                "prop": "extracts",
                "explaintext": "1",
                "exsectionformat": "wiki",
                "titles": title,
            }
        )
    )
    pages = payload.get("query", {}).get("pages", [])
    if not pages or pages[0].get("missing"):
        raise ValueError(f"Wikipedia article was not found: {title}")
    return pages[0]


def query_all_image_titles(title: str) -> list[str]:
    """Follow MediaWiki continuation until every article image title is listed."""
    params = {
        "action": "query",
        "format": "json",
        "formatversion": "2",
        "prop": "images",
        "imlimit": "max",
        "titles": title,
    }
    titles: list[str] = []
    while True:
        payload = fetch_json(api_url(params))
        pages = payload.get("query", {}).get("pages", [])
        if pages:
            titles.extend(
                image.get("title", "")
                for image in pages[0].get("images", [])
                if image.get("title", "").startswith("File:")
            )
        continuation = payload.get("continue")
        if not continuation:
            break
        params.update(continuation)
    # Preserve API order while removing duplicate file titles.
    return list(dict.fromkeys(titles))


def query_image_metadata(file_titles: list[str]) -> list[dict]:
    if not file_titles:
        return []
    result = []
    for offset in range(0, len(file_titles), IMAGE_QUERY_BATCH):
        batch = file_titles[offset : offset + IMAGE_QUERY_BATCH]
        payload = fetch_json(
            api_url(
                {
                    "action": "query",
                    "format": "json",
                    "formatversion": "2",
                    "prop": "imageinfo",
                    "iiprop": "url|extmetadata|size|mime",
                    "iiurlwidth": "1600",
                    "titles": "|".join(batch),
                }
            )
        )
        for page in payload.get("query", {}).get("pages", []):
            imageinfo = (page.get("imageinfo") or [{}])[0]
            metadata = imageinfo.get("extmetadata", {})
            result.append(
                {
                    "title": page.get("title", ""),
                    "source_url": imageinfo.get("descriptionurl", ""),
                    "download_url": imageinfo.get("thumburl") or imageinfo.get("url", ""),
                    "license": metadata.get("LicenseShortName", {}).get("value", ""),
                    "artist": metadata.get("Artist", {}).get("value", ""),
                    "attribution": metadata.get("Attribution", {}).get("value", ""),
                    "width": imageinfo.get("width", 0),
                    "height": imageinfo.get("height", 0),
                    "mime": imageinfo.get("mime", ""),
                }
            )
    return result


def image_selection_reason(image: dict) -> tuple[bool, str]:
    """Keep likely article photographs/maps; mark obvious decoration for review."""
    title = image.get("title", "").casefold()
    if any(word in title for word in EXCLUDED_IMAGE_WORDS):
        return False, "名稱符合標誌、介面圖示或道路編號圖示的排除規則"
    width = int(image.get("width") or 0)
    height = int(image.get("height") or 0)
    if width < MIN_IMAGE_WIDTH or height < MIN_IMAGE_HEIGHT:
        return False, f"圖片尺寸偏小（{width}×{height}）"
    if width and height and width / height > 4.5:
        return False, f"長寬比過寬，疑似橫幅（{width}×{height}）"
    return True, "尺寸與名稱符合正文圖片初篩，仍需人工確認"


def safe_extension(download_url: str) -> str:
    suffix = Path(urlparse(download_url).path).suffix.lower()
    return suffix if re.fullmatch(r"\.[a-z0-9]{1,5}", suffix) else ".img"


def clean_text(value: str) -> str:
    value = value.replace("\r", "")
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def load_urls() -> list[str]:
    with URLS_FILE.open("r", encoding="utf-8-sig", newline="") as file:
        return [row["url"].strip() for row in csv.DictReader(file) if row.get("url", "").strip()]


class ArticleHTMLParser(HTMLParser):
    """Extract readable text, image candidates, and links from static HTML."""

    IGNORED_TAGS = {"script", "style", "noscript", "svg", "nav", "footer", "header", "aside", "form", "button"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.meta_title = ""
        self.meta_description = ""
        self.text_parts: list[str] = []
        self.images: list[dict] = []
        self.links: list[str] = []
        self._ignored_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): value or "" for key, value in attrs}
        if tag == "meta":
            meta_key = (values.get("property") or values.get("name") or "").lower()
            if meta_key in {"og:title", "twitter:title"} and values.get("content"):
                self.meta_title = values["content"]
            elif meta_key in {"description", "og:description", "twitter:description"} and values.get("content"):
                self.meta_description = values["content"]
        if tag in self.IGNORED_TAGS:
            self._ignored_depth += 1
            return
        if self._ignored_depth:
            return
        if tag == "title":
            self._in_title = True
        if tag == "img":
            src = values.get("src") or values.get("data-src") or values.get("data-original") or values.get("data-lazy-src")
            srcset = values.get("data-srcset") or values.get("srcset")
            if not src and srcset:
                src = srcset.split(",", 1)[0].strip().split(" ", 1)[0]
            if src:
                self.images.append({
                    "src": src,
                    "alt": values.get("alt", ""),
                    "title": values.get("title", ""),
                    "width": values.get("width", ""),
                    "height": values.get("height", ""),
                })
        elif tag == "source" and (values.get("srcset") or values.get("data-srcset")):
            srcset = values.get("data-srcset") or values.get("srcset", "")
            src = srcset.split(",", 1)[0].strip().split(" ", 1)[0]
            if src:
                self.images.append({"src": src, "alt": values.get("alt", ""), "title": values.get("title", ""), "width": "", "height": ""})
        elif tag == "a" and values.get("href"):
            self.links.append(values["href"])
        if tag in {"p", "br", "div", "li", "h1", "h2", "h3", "h4", "tr"}:
            self.text_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.IGNORED_TAGS and self._ignored_depth:
            self._ignored_depth -= 1
            return
        if self._ignored_depth:
            return
        if tag == "title":
            self._in_title = False
        if tag in {"p", "br", "div", "li", "h1", "h2", "h3", "h4", "tr"}:
            self.text_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        text = data.strip()
        if not text:
            return
        if self._in_title:
            self.title_parts.append(text)
        self.text_parts.append(text + " ")


def image_dimension(value: str) -> int:
    match = re.search(r"\d+", value or "")
    return int(match.group(0)) if match else 0


def fetch_html(source_url: str) -> tuple[str, str]:
    request = Request(source_url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"})
    for attempt in range(2):
        try:
            with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                content_type = response.headers.get("Content-Type", "")
                if content_type and "html" not in content_type.lower() and "xml" not in content_type.lower():
                    raise ValueError(f"Source did not return an HTML page ({content_type}).")
                charset = response.headers.get_content_charset() or "utf-8"
                data = response.read()
            try:
                return data.decode(charset, errors="replace"), charset
            except LookupError:
                return data.decode("utf-8", errors="replace"), "utf-8"
        except HTTPError as error:
            if error.code != 429 and not 500 <= error.code < 600 or attempt == 1:
                raise
            time.sleep(3)
        except URLError as error:
            if attempt == 1 or "timed out" not in str(error).lower():
                raise
            time.sleep(3)
    raise RuntimeError("HTML request retries exhausted")


def video_id_from_url(source_url: str) -> str:
    parsed = urlparse(source_url)
    if parsed.hostname in {"youtu.be", "www.youtu.be"}:
        return parsed.path.strip("/").split("/", 1)[0]
    query_id = dict(re.findall(r"([^=&]+)=([^&]*)", parsed.query)).get("v", "")
    if query_id:
        return query_id
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 2 and parts[0] in {"shorts", "embed", "live"}:
        return parts[1]
    return ""


def extract_json_object(source: str, marker: str) -> dict | None:
    """Read the JSON object assigned to a JavaScript marker without a JS runtime."""
    start = source.find(marker)
    if start < 0:
        return None
    start = source.find("{", start + len(marker))
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(source)):
        char = source[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(source[start : index + 1])
                except json.JSONDecodeError:
                    return None
    return None


def fetch_subtitles(source_url: str) -> tuple[dict, list[dict]]:
    """Return selected caption track metadata and timestamped transcript segments."""
    video_id = video_id_from_url(source_url)
    if not video_id:
        raise ValueError("無法從 YouTube 網址解析影片 ID")
    watch_url = f"https://www.youtube.com/watch?v={video_id}&hl=zh-TW"
    page, _charset = fetch_html(watch_url)
    player = (
        extract_json_object(page, "ytInitialPlayerResponse =")
        or extract_json_object(page, "var ytInitialPlayerResponse =")
        or extract_json_object(page, "ytplayer.config =")
    )
    if not player:
        raise ValueError("YouTube 頁面未提供可讀取的播放器資料，可能需要登入或遭到限制")
    details = player.get("videoDetails", {})
    captions = player.get("captions", {}).get("playerCaptionsTracklistRenderer", {})
    tracks = captions.get("captionTracks", [])
    if not tracks:
        raise ValueError("影片沒有公開字幕軌道")
    preferred = sorted(
        tracks,
        key=lambda track: (
            0 if track.get("languageCode") in SUBTITLE_LANG_PREFERENCE else 1,
            SUBTITLE_LANG_PREFERENCE.index(track.get("languageCode")) if track.get("languageCode") in SUBTITLE_LANG_PREFERENCE else 99,
            1 if track.get("kind") == "asr" else 0,
        ),
    )[0]
    base_url = html.unescape(preferred.get("baseUrl", ""))
    if not base_url:
        raise ValueError("字幕軌道缺少下載網址")
    if "fmt=" not in base_url:
        base_url += "&fmt=srv3"
    xml_text, _charset = fetch_html(base_url)
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as error:
        raise ValueError(f"字幕 XML 無法解析：{error}") from error
    segments = []
    for node in root.findall(".//text"):
        text_value = clean_text(html.unescape("".join(node.itertext())))
        if not text_value:
            continue
        try:
            start = float(node.attrib.get("start", "0"))
            duration = float(node.attrib.get("dur", "0"))
        except ValueError:
            start, duration = 0.0, 0.0
        segments.append({"start": start, "duration": duration, "text": text_value})
    if not segments:
        raise ValueError("字幕軌道沒有可讀取的文字片段")
    return {
        "video_id": video_id,
        "language_code": preferred.get("languageCode", ""),
        "language_name": preferred.get("name", {}).get("simpleText", ""),
        "kind": "auto-generated" if preferred.get("kind") == "asr" else "manual",
        "track_url": base_url.split("&fmt=", 1)[0],
        "video_title": details.get("title", ""),
    }, segments


def fetch_subtitles_with_ytdlp(source_url: str) -> tuple[dict, list[dict]]:
    """Use the vendored yt-dlp Android client when YouTube blocks direct timedtext."""
    if not VENDOR_DIR.exists():
        raise ValueError("專案缺少 vendor/yt-dlp，無法使用字幕備援擷取器")
    import sys as _sys

    if str(VENDOR_DIR) not in _sys.path:
        _sys.path.insert(0, str(VENDOR_DIR))
    from yt_dlp import YoutubeDL

    options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "ignoreerrors": False,
        "noplaylist": True,
        "extractor_args": {"youtube": {"player_client": ["android"]}},
    }
    with YoutubeDL(options) as ydl:
        info = ydl.extract_info(source_url, download=False)
        if not info:
            raise ValueError("yt-dlp 無法取得影片資訊")
        manual_tracks = info.get("subtitles", {}) or {}
        auto_tracks = info.get("automatic_captions", {}) or {}
        tracks = manual_tracks or auto_tracks
        track_kind = "manual" if manual_tracks else "auto-generated"
        if not tracks:
            raise ValueError("影片沒有 yt-dlp 可讀取的公開字幕")
        language = next((lang for lang in SUBTITLE_LANG_PREFERENCE if lang in tracks), next(iter(tracks)))
        entries = tracks[language]
        entry = next((item for item in entries if item.get("ext") == "json3"), None)
        entry = entry or next((item for item in entries if item.get("ext") in {"srv3", "vtt", "srt"}), None)
        if not entry or not entry.get("url"):
            raise ValueError(f"字幕語言 {language} 沒有可下載格式")
        raw = ydl.urlopen(entry["url"]).read().decode("utf-8", errors="replace")
        segments = []
        if entry.get("ext") == "json3":
            payload = json.loads(raw)
            for event in payload.get("events", []):
                pieces = [segment.get("utf8", "") for segment in event.get("segs", [])]
                text_value = clean_text("".join(pieces))
                if not text_value or "tStartMs" not in event:
                    continue
                segments.append({
                    "start": float(event.get("tStartMs", 0)) / 1000,
                    "duration": float(event.get("dDurationMs", 0)) / 1000,
                    "text": text_value,
                })
        else:
            # yt-dlp normally exposes json3, but keep a small fallback for SRT/VTT.
            for block in re.split(r"\n\s*\n", raw.replace("\r", "")):
                lines = [line.strip() for line in block.split("\n") if line.strip()]
                timing = next((line for line in lines if " --> " in line), "")
                if not timing:
                    continue
                text_value = clean_text(" ".join(line for line in lines if line != timing and not line.isdigit()))
                start_value = timing.split(" --> ", 1)[0].replace(",", ".")
                try:
                    hours, minutes, seconds = start_value.split(":")
                    start = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
                except (ValueError, IndexError):
                    continue
                segments.append({"start": start, "duration": 0.0, "text": text_value})
        if not segments:
            raise ValueError("字幕下載成功但沒有可讀取的文字片段")
        return {
            "video_id": info.get("id", video_id_from_url(source_url)),
            "language_code": language,
            "language_name": (entry.get("name") or language),
            "kind": track_kind,
            "track_url": entry["url"].split("&fmt=", 1)[0],
            "video_title": info.get("title", ""),
        }, segments


def format_timestamp(seconds: float, decimal: str = ",") -> str:
    total_ms = max(0, round(seconds * 1000))
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{decimal}{millis:03d}"


def write_subtitle_files(record: dict, track: dict, segments: list[dict]) -> tuple[str, str]:
    subtitles_dir = OUTPUT_DIR / "subtitles"
    subtitles_dir.mkdir(parents=True, exist_ok=True)
    language = re.sub(r"[^A-Za-z0-9_-]+", "_", track.get("language_code") or "und")
    base_name = f"{record['id']}_{language}"
    srt_path = subtitles_dir / f"{base_name}.srt"
    md_path = subtitles_dir / f"{base_name}.md"
    srt_blocks = []
    md_lines = [
        f"# {record['title']} 字幕",
        "",
        f"- 語言：{track.get('language_name') or track.get('language_code') or '未標示'}",
        f"- 類型：{track.get('kind', '')}",
        f"- 來源影片：{record['source_url']}",
        "",
    ]
    for index, segment in enumerate(segments, start=1):
        start = segment["start"]
        end = start + max(segment.get("duration", 0), 1.0)
        text_value = segment["text"]
        srt_blocks.append(f"{index}\n{format_timestamp(start)} --> {format_timestamp(end)}\n{text_value}\n")
        md_lines.append(f"- `{format_timestamp(start).replace(',', '.')}` {text_value}")
    srt_path.write_text("\n".join(srt_blocks), encoding="utf-8")
    md_path.write_text("\n".join(md_lines) + "\n", encoding="utf-8")
    return f"subtitles/{md_path.name}", f"subtitles/{srt_path.name}"


def make_record(source_url: str, sequence: int) -> dict:
    return {
        "id": f"page_{sequence:03d}",
        "type": "article",
        "title": "",
        "source_url": source_url,
        "source_name": urlparse(source_url).hostname or "",
        "content": "",
        "images": [],
        "links": [],
        "timestamps": [],
        "crawl_status": "failed",
        "crawl_reason": "",
        "crawled_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def crawl_generic_page(source_url: str, sequence: int) -> tuple[dict, str]:
    record = make_record(source_url, sequence)
    try:
        raw_html, _charset = fetch_html(source_url)
        parser = ArticleHTMLParser()
        parser.feed(raw_html)
        record["title"] = clean_text(" ".join(parser.title_parts)) or parser.meta_title or source_url
        record["content"] = clean_text(" ".join(parser.text_parts))
        if len(record["content"]) < 80 and parser.meta_description:
            record["content"] = clean_text(record["content"] + "\n\n" + parser.meta_description)
        if len(record["content"]) < 80:
            raise ValueError("Page returned too little readable text; it may require JavaScript or block automated access.")
        record["links"] = list(dict.fromkeys(urljoin(source_url, href) for href in parser.links if href.strip()))[:200]

        images_dir = OUTPUT_DIR / "images"
        images_dir.mkdir(parents=True, exist_ok=True)
        candidates = []
        seen = set()
        for raw in parser.images:
            image_url = urljoin(source_url, raw["src"])
            parsed = urlparse(image_url)
            if parsed.scheme not in {"http", "https"} or image_url in seen:
                continue
            seen.add(image_url)
            candidate = {
                "title": raw["alt"] or raw["title"] or Path(parsed.path).name or image_url,
                "source_url": image_url,
                "download_url": image_url,
                "license": "請依來源網站確認",
                "artist": "",
                "attribution": "",
                "width": image_dimension(raw["width"]),
                "height": image_dimension(raw["height"]),
            }
            selected, reason = image_selection_reason(candidate)
            candidate["selected"] = selected
            candidate["selection_reason"] = reason
            candidate["download_status"] = "待下載" if selected else "skipped by image filter"
            candidate["file"] = ""
            candidates.append(candidate)

        selected_count = 0
        for image_index, image in enumerate(candidates, start=1):
            if not image["selected"]:
                continue
            if selected_count >= MAX_GENERIC_IMAGES_PER_SOURCE:
                image["selected"] = False
                image["selection_reason"] = f"每頁下載上限 {MAX_GENERIC_IMAGES_PER_SOURCE} 張"
                image["download_status"] = "skipped by per-page download cap"
                continue
            selected_count += 1
            filename = f"{record['id']}_image_{image_index:02d}{safe_extension(image['download_url'])}"
            try:
                (images_dir / filename).write_bytes(fetch_bytes(image["download_url"]))
                image["file"] = f"images/{filename}"
                image["download_status"] = "success"
            except (HTTPError, URLError, TimeoutError, OSError) as error:
                image["download_status"] = f"failed: {error}"
            time.sleep(0.1)

        record["images"] = candidates
        record["image_candidate_count"] = len(candidates)
        record["image_selected_count"] = sum(image["selected"] for image in candidates)
        record["crawl_status"] = "success"
        markdown = [f"# {record['title']}", "", f"- 來源：[{record['source_name']}]({source_url})", "", record["content"]]
        if candidates:
            markdown.extend(["", f"## 圖片候選（共 {len(candidates)} 張；初篩保留 {record['image_selected_count']} 張）", ""])
            for image in candidates:
                label = "已下載" if image["file"] else "已略過"
                markdown.append(f"- {label}：{image['title']} — {image['selection_reason']} — {image['source_url']}")
        return record, "\n".join(markdown) + "\n"
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as error:
        record["crawl_reason"] = str(error)
        record["title"] = record["title"] or source_url
        return record, f"# 擷取失敗\n\n- 來源：{source_url}\n- 原因：{error}\n"


def crawl_youtube(source_url: str, sequence: int) -> tuple[dict, str]:
    record = make_record(source_url, sequence)
    record["type"] = "video"
    record["source_name"] = "YouTube"
    try:
        oembed_url = "https://www.youtube.com/oembed?" + urlencode({"url": source_url, "format": "json"})
        try:
            data = fetch_json(oembed_url)
        except (HTTPError, URLError, TimeoutError, OSError):
            # oEmbed rejects some Shorts URLs; the watch page can still expose metadata.
            data = {}
        subtitle_status = "未嘗試"
        subtitle_track = {}
        subtitle_segments = []
        try:
            subtitle_track, subtitle_segments = fetch_subtitles(source_url)
            subtitle_status = "success"
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as error:
            try:
                subtitle_track, subtitle_segments = fetch_subtitles_with_ytdlp(source_url)
                subtitle_status = "success (yt-dlp fallback)"
            except (HTTPError, URLError, TimeoutError, OSError, ValueError, ImportError, json.JSONDecodeError) as fallback_error:
                subtitle_status = f"failed: {fallback_error}"

        if not data:
            page_html, _charset = fetch_html(f"https://www.youtube.com/watch?v={video_id_from_url(source_url)}&hl=zh-TW")
            player = extract_json_object(page_html, "ytInitialPlayerResponse =")
            details = (player or {}).get("videoDetails", {})
            data = {"title": details.get("title", source_url), "author_name": details.get("author", ""), "thumbnail_url": ""}
        record["title"] = data.get("title", source_url)
        record["subtitle_status"] = subtitle_status
        record["subtitle_track"] = subtitle_track
        record["subtitle_segment_count"] = len(subtitle_segments)
        record["content"] = f"影片標題：{record['title']}\n頻道：{data.get('author_name', '')}\n影片網址：{source_url}\n字幕狀態：{subtitle_status}"
        if subtitle_segments:
            record["content"] += "\n\n" + "\n".join(
                f"[{format_timestamp(segment['start']).replace(',', '.')}] {segment['text']}"
                for segment in subtitle_segments
            )
        thumbnail = data.get("thumbnail_url", "")
        if thumbnail:
            record["images"] = [{
                "title": "YouTube 縮圖",
                "source_url": thumbnail,
                "license": "請依 YouTube 與影片權利人條款確認",
                "artist": data.get("author_name", ""),
                "selected": False,
                "selection_reason": "僅記錄縮圖網址，未下載影片或縮圖",
                "file": "",
                "download_status": "not downloaded",
            }]
        record["crawl_status"] = "success"
        subtitle_link = record.get("subtitle_file", "")
        if subtitle_segments:
            subtitle_link, subtitle_srt_link = write_subtitle_files(record, subtitle_track, subtitle_segments)
            record["subtitle_file"] = subtitle_link
            record["subtitle_srt_file"] = subtitle_srt_link
        markdown = [
            f"# {record['title']}",
            "",
            f"- 來源：[YouTube]({source_url})",
            f"- 頻道：{data.get('author_name', '')}",
            f"- 字幕狀態：{subtitle_status}",
        ]
        if subtitle_link:
            markdown.append(f"- 字幕檔：{subtitle_link}")
        if subtitle_segments:
            markdown.extend(["", "## 時間軸字幕", ""])
            markdown.extend(f"- `{format_timestamp(segment['start']).replace(',', '.')}` {segment['text']}" for segment in subtitle_segments)
        else:
            markdown.extend(["", "未取得可讀字幕。"])
        return record, "\n".join(markdown) + "\n"
    except (HTTPError, URLError, TimeoutError, OSError, ValueError, KeyError) as error:
        record["crawl_reason"] = str(error)
        record["title"] = source_url
        return record, f"# 擷取失敗\n\n- 來源：{source_url}\n- 原因：{error}\n"


def remove_obsolete_downloads(records: list[dict]) -> None:
    """Remove only this crawler's generated files that are no longer selected."""
    images_dir = OUTPUT_DIR / "images"
    if not images_dir.exists():
        return
    kept = {
        image["file"]
        for record in records
        for image in record.get("images", [])
        if image.get("file")
    }
    for path in images_dir.glob("page_[0-9][0-9][0-9]_image_*"):
        relative_path = path.relative_to(OUTPUT_DIR).as_posix()
        if relative_path not in kept:
            path.unlink()


def crawl_one(source_url: str, sequence: int) -> tuple[dict, str]:
    hostname = (urlparse(source_url).hostname or "").lower()
    if hostname in {"zh.wikipedia.org", "zh.m.wikipedia.org"}:
        return crawl_wikipedia(source_url, sequence)
    if hostname in {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "www.youtu.be"}:
        return crawl_youtube(source_url, sequence)
    return crawl_generic_page(source_url, sequence)


def crawl_wikipedia(source_url: str, sequence: int) -> tuple[dict, str]:
    with WIKIPEDIA_SEMAPHORE:
        return _crawl_wikipedia_serial(source_url, sequence)


def _crawl_wikipedia_serial(source_url: str, sequence: int) -> tuple[dict, str]:
    record = make_record(source_url, sequence)
    record["source_name"] = "維基百科（中文）"
    try:
        requested_title = title_from_wikipedia_url(source_url)
        page = query_article(requested_title)
        record["title"] = page.get("title", requested_title)
        record["content"] = clean_text(page.get("extract", ""))
        if not record["content"]:
            raise ValueError("The API returned no article text.")

        file_titles = query_all_image_titles(requested_title)
        metadata_items = query_image_metadata(file_titles)

        images_dir = OUTPUT_DIR / "images"
        images_dir.mkdir(parents=True, exist_ok=True)
        for image_index, item in enumerate(metadata_items, start=1):
            image_record = {key: item[key] for key in ("title", "source_url", "license", "artist", "attribution")}
            image_record["width"] = item.get("width", 0)
            image_record["height"] = item.get("height", 0)
            selected, selection_reason = image_selection_reason(item)
            image_record["selected"] = selected
            image_record["selection_reason"] = selection_reason
            download_url = item.get("download_url", "")
            if selected and download_url:
                filename = f"{record['id']}_image_{image_index:02d}{safe_extension(download_url)}"
                try:
                    (images_dir / filename).write_bytes(fetch_bytes(download_url))
                    image_record["file"] = f"images/{filename}"
                    image_record["download_status"] = "success"
                except (HTTPError, URLError, TimeoutError, OSError) as error:
                    image_record["file"] = ""
                    image_record["download_status"] = f"failed: {error}"
            elif selected:
                image_record["file"] = ""
                image_record["download_status"] = "no downloadable URL returned"
            else:
                image_record["file"] = ""
                image_record["download_status"] = "skipped by image filter"
            record["images"].append(image_record)
            time.sleep(0.1)

        record["image_candidate_count"] = len(metadata_items)
        record["image_selected_count"] = sum(image.get("selected", False) for image in record["images"])

        record["crawl_status"] = "success"
        markdown = [f"# {record['title']}", "", f"- 來源：[{record['source_name']}]({source_url})", "", record["content"]]
        if record["images"]:
            markdown.extend(["", f"## 圖片候選與篩選（共 {record['image_candidate_count']} 張；初篩保留 {record['image_selected_count']} 張）", ""])
            for image in record["images"]:
                if image["selected"]:
                    attribution = image.get("license") or "授權資訊未提供"
                    markdown.append(f"- 已下載：{image.get('file') or image['title']} — [{attribution}]({image.get('source_url') or source_url})")
                else:
                    markdown.append(f"- 已略過：{image['title']} — {image['selection_reason']}")
        return record, "\n".join(markdown) + "\n"
    except (HTTPError, URLError, TimeoutError, OSError, ValueError, KeyError) as error:
        record["crawl_reason"] = str(error)
        record["title"] = record["title"] or source_url
        return record, f"# 擷取失敗\n\n- 來源：{source_url}\n- 原因：{error}\n"


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Crawl the source URLs listed in urls.csv")
    parser.add_argument("--limit", type=int, default=None, help="Only process the first N URLs")
    parser.add_argument("--retry-failed", action="store_true", help="Retry failed URLs from the existing index.jsonl while preserving prior results")
    parser.add_argument("--refresh-videos", action="store_true", help="Reprocess all YouTube URLs to retrieve subtitles")
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "articles").mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "videos").mkdir(parents=True, exist_ok=True)
    urls = load_urls()
    records_by_index = {}
    if (args.retry_failed or args.refresh_videos) and (OUTPUT_DIR / "index.jsonl").exists():
        for line in (OUTPUT_DIR / "index.jsonl").read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            old_record = json.loads(line)
            old_index = int(old_record["id"].split("_")[-1])
            records_by_index[old_index] = old_record
        targets = []
        for record in records_by_index.values():
            is_video = record.get("type") == "video" or (urlparse(record.get("source_url", "")).hostname or "").lower() in {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "www.youtu.be"}
            if (args.retry_failed and record.get("crawl_status") != "success") or (args.refresh_videos and is_video):
                targets.append((int(record["id"].split("_")[-1]), record["source_url"]))
    else:
        if args.limit is not None:
            urls = urls[: max(0, args.limit)]
        targets = list(enumerate(urls, start=1))

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(crawl_one, source_url, index): (index, source_url)
            for index, source_url in targets
        }
        for future in as_completed(futures):
            index, source_url = futures[future]
            try:
                record, markdown = future.result()
            except Exception as error:
                record = make_record(source_url, index)
                record["crawl_reason"] = f"Unexpected error: {error}"
                record["title"] = source_url
                markdown = f"# 擷取失敗\n\n- 來源：{source_url}\n- 原因：{error}\n"
            records_by_index[index] = record
            if record["type"] == "video":
                output_path = OUTPUT_DIR / "videos" / f"video_{index:03d}.md"
            else:
                output_path = OUTPUT_DIR / "articles" / f"{record['id']}.md"
            output_path.write_text(markdown, encoding="utf-8")
            print(f"{record['id']}: {record['crawl_status']} - {record['title']}", flush=True)

    records = [records_by_index[index] for index in sorted(records_by_index)]

    remove_obsolete_downloads(records)

    with (OUTPUT_DIR / "index.jsonl").open("w", encoding="utf-8") as file:
        for record in records:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")

    with (OUTPUT_DIR / "crawl_report.csv").open("w", encoding="utf-8-sig", newline="") as file:
        fields = ["id", "type", "title", "source_url", "crawl_status", "crawl_reason", "subtitle_status", "subtitle_segment_count", "subtitle_file", "subtitle_srt_file", "crawled_at"]
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows({key: record.get(key, "") for key in fields} for record in records)

    with (OUTPUT_DIR / "image_candidates.csv").open("w", encoding="utf-8-sig", newline="") as file:
        fields = ["article_id", "title", "selected", "selection_reason", "width", "height", "file", "download_status", "source_url", "license", "artist"]
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        rows = []
        for record in records:
            for image in record.get("images", []):
                rows.append({"article_id": record["id"], **{key: image.get(key, "") for key in fields[1:]}})
        writer.writerows(rows)

    failed = sum(record["crawl_status"] != "success" for record in records)
    print(f"Finished: {len(records) - failed}/{len(records)} succeeded. Output: {OUTPUT_DIR}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
