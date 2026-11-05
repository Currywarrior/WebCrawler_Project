# 從網頁到知識庫：觀音地方知識多媒體爬蟲

以「桃園市觀音區地方知識」為主題，把教師提供的 31 個網址（28 個網頁、3 部 YouTube 影片）自動爬取、去雜訊、擷取圖片與影片字幕／代表畫面，整理成可追溯來源的結構化資料（`output/index.jsonl`），並做相似文章偵測、主題分類、關鍵字與人工查證。

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
| `report/report.md` | 實作報告（含失敗網址說明） |

編號規則：`urls.csv` 中的網頁依序為 `page_001`～`page_028`、影片為 `video_001`～`video_003`，延伸探索的頁面為 `ext_001` 起；`crawl_report.csv` 的 `no` 欄位對回 `urls.csv` 的編號。
