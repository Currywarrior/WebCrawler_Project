"""現場展示用：從 index.jsonl 找出一筆資料，印出重點欄位
用法：python crawler/show.py page_005
"""
import json
import os
import sys

path = os.path.join(os.path.dirname(__file__), "..", "output", "index.jsonl")
with open(path, encoding="utf-8") as f:
    for line in f:
        r = json.loads(line)
        if r["id"] == sys.argv[1]:
            keys = ("id", "type", "title", "source_url", "source_name", "crawl_status",
                    "crawled_at", "category", "keywords")
            show = {k: r.get(k) for k in keys}
            show["content"] = f"正文 {len(r.get('content') or '')} 字"
            show["images"] = f"{len(r.get('images') or [])} 張圖片／畫面"
            show["links"] = f"{len(r.get('links') or [])} 條連結"
            show["timestamps"] = f"{len(r.get('timestamps') or [])} 段字幕時間戳"
            print(json.dumps(show, ensure_ascii=False, indent=2))
            break
    else:
        print("找不到", sys.argv[1])
