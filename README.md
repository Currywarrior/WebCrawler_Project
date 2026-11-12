# 從網頁到知識庫：觀音地方知識多媒體爬蟲

以「桃園市觀音區地方知識」為主題，把教師提供的 31 個網址（28 個網頁、3 部 YouTube 影片）自動爬取、去雜訊、擷取圖片與影片字幕／代表畫面，整理成可追溯來源的結構化資料（`output/index.jsonl`），並做相似文章偵測、主題分類、關鍵字與人工查證。現在也提供來源發現器，不再受限於原始清單：可從 Wikipedia、Wikimedia Commons、OpenStreetMap Overpass 與網站 sitemap 找新頁面，並將 OSM POI 與 Open-Meteo 即時天氣等相關資料另存為 JSONL。

## 環境安裝

```powershell
cd WebCrawler_Project
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m playwright install chromium   # 爬 JS 渲染網站與截圖用
```

## 執行方式

```powershell
# 1. 爬全部網址 + 一層延伸探索（約 15~20 分鐘，有每站 1 秒限速）
.venv\Scripts\python crawler\crawler.py

# 1a. 加入策展的額外來源（sources_extra.csv，目前 5 筆）
.venv\Scripts\python crawler\crawler.py --include-extra

# 1b. 執行來源發現器並爬取最多 30 個新網址
.venv\Scripts\python crawler\crawler.py --discover --max-discovered 30 --topic "觀音 桃園"

# 2. 現場展示：只重跑 urls.csv 第 5、26 筆（編號固定，結果合併回 index.jsonl）
.venv\Scripts\python crawler\crawler.py --only 5,26

# 3. 現場展示：老師臨時給任一網址（網頁或 YouTube 都可），輸出到 output\demo\
.venv\Scripts\python crawler\crawler.py --url "https://zh.wikipedia.org/wiki/白沙岬燈塔"

# 4. 任務 6：相似文章、主題分類、關鍵字，並把查證結果併回 index.jsonl
.venv\Scripts\python crawler\analyze.py

# 5. 任務 2：產生原始網頁 vs 清理結果的對照截圖
.venv\Scripts\python crawler\screenshots.py page_001 page_005 page_019 page_023
```

## 系統功能與程式對應

| 程式 | 負責 |
|---|---|
| `crawler/fetch.py` | 讀 robots.txt、每站限速；requests 優先，遇 SSL 憑證鏈錯誤或 JS 渲染站改用 Playwright（Chromium），並分段捲動觸發 lazy loading |
| `crawler/extract.py` | 中繼資料（標題、站名、作者、日期，含 JSON-LD）、全頁連結清單、三層雜訊清除、正文定位、轉 Markdown、圖片候選與過濾規則 |
| `crawler/video.py` | yt-dlp 只取中繼資料（不下載影片）、youtube-transcript-api 取官方字幕、storyboard 代表畫面挑選 |
| `crawler/crawler.py` | 主流程：網址去重、網頁／影片分流、圖片下載與去重、延伸探索、輸出 index.jsonl 與 crawl_report.csv |
| `crawler/discovery.py` | 來源發現與相關資料：MediaWiki、Wikimedia Commons、OSM Overpass、sitemap、Open-Meteo |
| `crawler/analyze.py` | TF-IDF 相似度、主題分類、jieba 關鍵字、併入查證結果 |
| `crawler/screenshots.py` | 任務 2 對照截圖 |

## 輸出檔案

| 路徑 | 內容 |
|---|---|
| `output/index.jsonl` | 每行一筆結構化資料：id、type、title、source_url、source_name、content、images、links、timestamps、crawl_status、crawled_at，以及 category、keywords、similar_docs、fact_checks |
| `output/crawl_report.csv` | 每個網址（含延伸連結）的取得方式、HTTP 狀態、成功／失敗／無法存取與原因 |
| `output/articles/page_XXX.md` | 清理後可直接閱讀的正文，圖片插在原文位置並標來源網址 |
| `output/images/page_XXX_image_NN.jpg` | 文章內圖片；對應關係與被排除的圖片及原因記在 index.jsonl |
| `output/links/page_XXX_links.csv` | 該頁所有超連結，標記是否在正文內、是否與觀音主題相關 |
| `output/videos/video_XXX.md`、`video_XXX_frames/` | 影片基本資料、帶時間戳的逐字稿、代表畫面 |
| `output/similar_pairs.csv`、`classification.csv`、`fact_check.json` | 任務 6 成果 |
| `output/discovered_sources.csv` | `--discover` 找到的去重網址、provider 與查詢條件 |
| `output/related_data.jsonl` | `--discover` 取得的 Wikipedia 摘要、Commons 檔案中繼資料、OSM POI 與天氣快照 |
| `report/report.md` | 實作報告（含失敗網址說明） |

編號規則：`urls.csv` 中的網頁依序為 `page_001`～`page_028`、影片為 `video_001`～`video_003`，延伸探索的頁面為 `ext_001` 起；`crawl_report.csv` 的 `no` 欄位對回 `urls.csv` 的編號。

## 擴充來源的實作方法

### A. 加入固定來源

把一列 `no,url,note,source_kind` 加到 `sources_extra.csv`，再執行 `--include-extra`。`source_kind=curated` 會寫進 `index.jsonl` 與 `crawl_report.csv`，因此可以區分教師指定、策展與頁內延伸來源。網址仍會先經過追蹤參數清理與正規化去重。

### B. 由公開 API 發現新網站

`discovery_sources.csv` 是 provider 設定檔，每列可開關、指定查詢與數量上限。目前內建：

1. `wikipedia`：MediaWiki `list=search`，回傳可直接交給 HTML 爬蟲的文章網址。
2. `commons`：Wikimedia Commons namespace 6 檔案搜尋，保存檔案頁、縮圖、尺寸、作者與授權短名，完整授權仍以原頁為準。
3. `openstreetmap`：Overpass `nwr["name"]`，把觀音區有名稱的 POI 轉成 OSM 頁面，完整 tags、座標寫入 `related_data.jsonl`。
4. `sitemap`：讀公開 XML sitemap，適合沒有搜尋 API 的政府或觀光網站；新增一列即可使用。

執行 `crawler.py --discover --topic "觀音 桃園" --max-discovered 30` 後，發現結果會和原始種子一起進行相同的 robots、限速、HTML 正文與圖片流程。API 失敗只會得到空結果，不會覆蓋既有爬取資料。程式另以固定座標呼叫免金鑰的 Open-Meteo，將查詢時間、單位與 current 值存檔，避免把會變動的天氣誤當成文章正文。

### C. 新增 provider

在 `crawler/discovery.py` 新增 `discover_<name>() -> (rows, related)`，`rows` 至少含 `url/title/provider`，`related` 放需要保存的結構化欄位；再於 `discover_from_config()` 加一個 provider 分支。最後在 `discovery_sources.csv` 增加設定列。網頁解析、圖片過濾與輸出格式不需修改。

### D. 來源與資料的追溯欄位

所有新文章會多出 `source_kind`、`discovery_provider` 與 `source_note`；API 原始資料則保留 provider、查詢或 bbox、來源 URL、座標／tags（OSM）與取得時間（天氣）。因此可以由 `index.jsonl` 回到文章頁，再由 `discovered_sources.csv` 或 `related_data.jsonl` 回到發現方法與原始資料。
