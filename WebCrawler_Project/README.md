# 觀音區維基百科爬蟲範例（舊版）

> 這是早期的單檔範例與 vendor 備份。正式流程請回到上一層執行 `crawler/crawler.py`；來源擴充、API 發現器與相關資料輸出都已整合在上一層的 `discovery_sources.csv`、`sources_extra.csv` 與 `crawler/discovery.py`。

來源清單 `urls.csv` 已匯入來源文件中的 31 個網址。爬蟲透過 MediaWiki API 擷取維基百科頁面，對一般網站擷取公開 HTML 文字、連結和圖片候選；YouTube 讀取公開標題、頻道與字幕，不下載影片。字幕先嘗試播放器頁面的公開字幕軌道；若直接端點被限制，改用專案內 `vendor/yt-dlp` 的 Android 公開播放器客戶端讀取字幕。圖片先依檔名、尺寸與長寬比初篩；一般網站每頁最多下載 8 張，篩選結果仍應人工確認。

## 執行方式

需要 Python 3.10 以上版本，不需要安裝第三方套件。

```powershell
python crawler.py
```

只處理清單前一筆：

```powershell
python crawler.py --limit 1
```

重試上次失敗的網址並保留其他結果：

```powershell
python crawler.py --retry-failed
```

重新處理所有 YouTube 來源並更新字幕：

```powershell
python crawler.py --refresh-videos
```

## 輸出

- `output/articles/page_001.md`：文章內容與圖片來源
- `output/videos/`：影片基本資料與時間軸字幕摘要
- `output/subtitles/`：成功取得的 `.srt` 字幕檔與可讀的 `.md` 字幕檔
- `output/images/`：成功下載的圖片
- `output/image_candidates.csv`：全部候選圖片、篩選結果、尺寸與來源／授權資訊
- `output/index.jsonl`：文章、來源、圖片授權與爬取狀態
- `output/crawl_report.csv`：每筆網址的成功或失敗紀錄

字幕欄位會寫在 `output/index.jsonl` 的 `subtitle_status`、`subtitle_track`、`subtitle_segment_count`、`subtitle_file` 和 `subtitle_srt_file`。沒有公開字幕、需要登入、影片被限制或字幕端點拒絕時，程式會保留影片基本資料並把原因寫入 `subtitle_status`。

## 加入其他網址

將網址新增到 `urls.csv`，每列一個網址。維基百科、YouTube 和一般網站使用不同的解析方式。程式會略過名稱含標誌、介面圖示和道路編號圖示關鍵字的圖片，也會略過太小或過寬的圖；這些規則是初篩，請檢查 `image_candidates.csv` 後再使用圖片。下載圖片和使用字幕前請確認原始頁面的授權條件，並保留輸出的來源與授權資訊。
