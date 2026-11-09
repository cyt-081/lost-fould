# -*- coding: utf-8 -*-
"""毫秒级关键词检索：不调用大模型，纯内存打分。

打分项：类别命中、颜色命中、地点命中、特征词命中、整段文本的中文二元组重合度。
所有字段在登记时已经结构化，所以查询只需要做集合运算。
"""
import unicodedata

WEIGHT_CATEGORY = 34
WEIGHT_COLOR = 18
WEIGHT_PLACE = 16
WEIGHT_FEATURE = 9
WEIGHT_BIGRAM = 40


def normalize(text):
    text = unicodedata.normalize("NFKC", str(text or "")).lower()
    return "".join(ch for ch in text if not ch.isspace())


def bigrams(text):
    text = normalize(text)
    return {text[i:i + 2] for i in range(len(text) - 1)} if len(text) > 1 else {text}


def build_index(items):
    rows = []
    for item in items:
        features = item.get("features") or []
        blob = " ".join([
            str(item.get("title", "")), str(item.get("category", "")), str(item.get("color", "")),
            str(item.get("place", "")), " ".join(map(str, features)), str(item.get("detail", "")),
        ])
        rows.append({
            "item": item,
            "category": normalize(item.get("category", "")),
            "color": normalize(item.get("color", "")),
            "place": normalize(item.get("place", "")),
            "features": [normalize(f) for f in features],
            "title": normalize(item.get("title", "")),
            "grams": bigrams(blob),
        })
    return rows


def score_row(query_norm, query_grams, row):
    score = 0.0
    if row["category"] and row["category"] in query_norm:
        score += WEIGHT_CATEGORY
    if row["color"] and row["color"] in query_norm:
        score += WEIGHT_COLOR
    if row["place"] and (row["place"] in query_norm or any(part and part in query_norm for part in row["place"].split())):
        score += WEIGHT_PLACE
    for feature in row["features"]:
        if feature and feature in query_norm:
            score += WEIGHT_FEATURE
    if row["title"] and row["title"] in query_norm:
        score += 20
    if query_grams:
        overlap = len(query_grams & row["grams"]) / len(query_grams)
        score += overlap * WEIGHT_BIGRAM
    return score


def search(items, index, query, limit=10):
    query_norm = normalize(query)
    if not query_norm:
        return []
    query_grams = bigrams(query)
    scored = []
    for row in index:
        score = score_row(query_norm, query_grams, row)
        if score > 0:
            scored.append((round(min(score, 100), 1), row["item"]))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    cards = []
    for score, item in scored[:limit]:
        cards.append({
            "id": item.get("id"),
            "title": item.get("title", ""),
            "detail": item.get("detail", ""),
            "category": item.get("category", ""),
            "color": item.get("color", ""),
            "place": item.get("place", ""),
            "features": item.get("features", []),
            "kind": item.get("kind", "found"),
            "created_at": item.get("created_at", ""),
            "score": score,
        })
    return cards
