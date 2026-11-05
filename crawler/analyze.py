"""任務 6：重複/相似文章偵測、主題分類、關鍵字，並把查證結果併入。
用法：python crawler/analyze.py   （讀 output/index.jsonl，原地補上 category / keywords 欄位）"""
import csv
import re
import json
import os

import jieba
import jieba.analyse
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "output")

# 地名與專有名詞加進字典，避免被切碎（例如「白沙岬燈塔」不要切成「白沙/岬/燈塔」）
LOCAL_TERMS = ["觀音區", "白沙岬燈塔", "白沙岬", "甘泉寺", "石觀音", "觀新藻礁", "藻礁", "草漯沙丘",
               "大潭發電廠", "大堀溪", "觀音溪", "觀音坑溪", "蓮花園", "蓮花季", "白千層", "綠色隧道",
               "樹林村", "保障村", "新坡", "崙坪", "石門水庫", "移民新村", "領海基點", "海客",
               "客家", "石滬", "觀音工業區", "觀音產業園區", "觀音飛機", "燈塔", "藻礁生態系",
               "百合花", "樹園子", "牛家莊", "泰雅", "遷村", "甘泉井", "埤塘"]
for t in LOCAL_TERMS:
    jieba.add_word(t, freq=20000)
jieba.setLogLevel(60)
STOP = {"觀音", "桃園", "桃園市", "可以", "這裡", "我們", "一個", "還有", "非常", "因為", "所以",
        "就是", "沒有", "時候", "大家", "其中", "以及", "位於", "地方", "這個", "目前", "許多",
        "編輯", "參考", "資料", "來源", "網站", "提供", "照片", "圖片", "原文", "說明",
        "中文", "存檔", "內容", "原始", "相關", "連結", "截圖", "網路", "時間", "這邊", "覺得", "起來", "https", "www"}

# 主題分類：沿用作業給的四類，再補兩類以涵蓋觀音地方學的八大主題
CATEGORIES = {
    "自然景觀與生態": ["沙丘", "藻礁", "海岸", "溪", "河", "濕地", "生態", "保護區", "植物", "鳥",
                  "地質", "海洋", "防風林", "沙灘", "地形", "水文", "藻"],
    "歷史文化與信仰": ["歷史", "日治", "清", "乾隆", "古蹟", "寺", "廟", "信仰", "祭", "客家", "海客",
                  "聚落", "移民", "遷村", "族", "文化", "古", "沿革", "泰雅", "記憶"],
    "地方產業與能源": ["工業區", "產業", "電廠", "發電", "天然氣", "工廠", "鋼", "公司", "廠",
                  "能源", "經濟", "製造", "風電"],
    "農業與飲食": ["蓮花", "農", "稻", "農場", "田", "餐廳", "料理", "美食", "小吃", "食堂", "菜",
               "百合", "花海", "休閒農業"],
    "觀光景點與休閒": ["景點", "打卡", "一日遊", "旅遊", "旅行", "遊客", "門票", "交通", "停車",
                  "開放時間", "推薦", "步道", "拍照", "營業"],
    "海洋法制與當代議題": ["領海", "基點", "海洋法", "主權", "保育", "永續", "開發", "爭議", "公投"],
}


def plain(text):
    """去掉 Markdown 符號、網址、維基引用標記，只留文字給斷詞。"""
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"\[\s*\d+\s*\]|[#|*>\-]{2,}|[|#*>]", " ", text)
    return text


def classify(text, title):
    # 標題權重 x5：標題最能代表文章主題
    scores = {c: sum(text.count(w) + 5 * title.count(w) for w in ws) for c, ws in CATEGORIES.items()}
    ranked = sorted(scores.items(), key=lambda x: -x[1])
    main = ranked[0][0] if ranked[0][1] else "未分類"
    second = [c for c, s in ranked[1:3] if s >= ranked[0][1] * 0.5 and s > 0]
    return main, second, scores


def keywords(text, title):
    tags = jieba.analyse.extract_tags(plain(title + " " + title + " " + text), topK=30)
    out = [t for t in tags if t not in STOP and len(t) >= 2
           and re.search(r"[一-鿿]|^[A-Za-z]{3,}$", t)]
    return out[:5]


def main():
    docs = [json.loads(l) for l in open(os.path.join(OUT, "index.jsonl"), encoding="utf-8")]
    ok = [d for d in docs if d["crawl_status"].startswith("成功") and len(d["content"]) >= 100]

    # 1) 相似度：字元 2~3-gram TF-IDF（中文不用斷詞也能比）+ 餘弦相似度
    vec = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 3), min_df=1, sublinear_tf=True)
    X = vec.fit_transform([d["content"] for d in ok])
    sim = cosine_similarity(X)
    pairs = []
    for i in range(len(ok)):
        for j in range(i + 1, len(ok)):
            if sim[i, j] >= 0.35:
                level = "高度相似（疑似轉載/同源）" if sim[i, j] >= 0.6 else "主題相近"
                pairs.append({"a": ok[i]["id"], "b": ok[j]["id"], "similarity": round(float(sim[i, j]), 3),
                              "level": level, "a_title": ok[i]["title"], "b_title": ok[j]["title"]})
    pairs.sort(key=lambda p: -p["similarity"])
    with open(os.path.join(OUT, "similar_pairs.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, ["a", "b", "similarity", "level", "a_title", "b_title"])
        w.writeheader()
        w.writerows(pairs)

    # 2) 分類與 3) 關鍵字
    rows = []
    sim_map = {}
    for p in pairs:
        sim_map.setdefault(p["a"], []).append({"id": p["b"], "similarity": p["similarity"]})
        sim_map.setdefault(p["b"], []).append({"id": p["a"], "similarity": p["similarity"]})
    for d in docs:
        if not d["crawl_status"].startswith("成功"):
            d["category"], d["sub_categories"], d["keywords"], d["similar_docs"] = "（爬取失敗不分類）", [], [], []
            continue
        text = d["content"] or (d.get("description") or "")
        cat, sub, _ = classify(text, d["title"] or "")
        d["category"], d["sub_categories"] = cat, sub
        d["keywords"] = keywords(text, d["title"] or "") if text else []
        d["similar_docs"] = sim_map.get(d["id"], [])
        rows.append({"id": d["id"], "title": d["title"], "category": cat,
                     "sub_categories": "、".join(sub), "keywords": "、".join(d["keywords"])})
    with open(os.path.join(OUT, "classification.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, ["id", "title", "category", "sub_categories", "keywords"])
        w.writeheader()
        w.writerows(rows)

    # 4) 查證結果（人工查證寫在 fact_check.json，這裡併回對應文章）
    fc_path = os.path.join(OUT, "fact_check.json")
    if os.path.exists(fc_path):
        checks = json.load(open(fc_path, encoding="utf-8"))
        for d in docs:
            d["fact_checks"] = [c["claim_id"] for c in checks if d["id"] in c["source_docs"]]
    with open(os.path.join(OUT, "index.jsonl"), "w", encoding="utf-8") as f:
        for d in docs:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
    print(f"分類 {len(rows)} 筆、相似配對 {len(pairs)} 組（高度相似 "
          f"{sum(p['similarity'] >= 0.6 for p in pairs)} 組）")


if __name__ == "__main__":
    main()
