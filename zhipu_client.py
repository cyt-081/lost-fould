# -*- coding: utf-8 -*-
"""智谱 GLM 封装：把一段杂乱的自然语言抽成结构化字段。

设计要点：
  1. Key 只从环境变量读，绝不写进代码；
  2. 失败/超时/没配 Key 时自动降级到本地规则抽取，登记接口永远可用；
  3. 返回 (字段, 来源)，来源是 llm / rule / rule_fallback，方便前端和日志展示。
"""
import json
import os
import re
import time

import requests

API_URL = os.environ.get("ZHIPU_API_URL", "https://open.bigmodel.cn/api/paas/v4/chat/completions")
DEFAULT_MODEL = os.environ.get("ZHIPU_MODEL", "glm-4-flash")
TIMEOUT = float(os.environ.get("ZHIPU_TIMEOUT", "20"))

CATEGORY_ENUM = ["雨伞", "雨衣", "保温杯", "水壶", "耳机", "双肩包", "充电器", "充电宝",
                 "校园卡", "学生证", "钥匙", "围巾", "眼镜", "手机", "电脑", "钱包", "其他"]

SYSTEM_PROMPT = (
    "你是校园失物招领平台的信息抽取引擎。用户会给你一段杂乱的自然语言，"
    "你要抽取成 JSON，只输出 JSON，不要解释、不要代码块标记。字段要求："
    'action_type：必填，只能是 "lost"（寻物启事，失主在找东西）或 "found"（失物招领，有人捡到东西）；'
    "title：物品名，4-10 个字，只写物品本身，不要加「失物招领」「丢失」这类词；"
    "category：必须从这组词里原样选一个 —— " + "、".join(CATEGORY_ENUM) + "；"
    "color：颜色词，没有就给空字符串；"
    "place：地点，尽量具体到楼栋、楼层、房间号，没有就给空字符串；"
    "features：字符串数组，最多 4 条，只写外观或内物特征，不要把时间、心情、过程写进去；"
    "contact：仅当用户明确给出了电话/微信/邮箱时才填，否则必须为空字符串，不要写任何占位说明。"
)
USER_TEMPLATE = "请抽取下面这段描述：\n{text}"

COLORS = ["黑色", "白色", "蓝色", "红色", "灰色", "银色", "黄色", "绿色", "紫色", "粉色", "橙色", "棕色"]
CATEGORIES = [
    ("雨伞", ["雨伞", "伞"]), ("雨衣", ["雨衣"]), ("保温杯", ["保温杯", "水杯"]), ("水壶", ["水壶"]),
    ("耳机", ["耳机", "airpods", "蓝牙耳机"]), ("双肩包", ["双肩包", "书包", "背包"]),
    ("充电器", ["充电器", "电源", "适配器"]), ("充电宝", ["充电宝", "移动电源"]),
    ("校园卡", ["校园卡", "饭卡", "一卡通"]), ("学生证", ["学生证", "证件"]),
    ("钥匙", ["钥匙"]), ("围巾", ["围巾"]), ("眼镜", ["眼镜"]), ("手机", ["手机"]),
    ("电脑", ["电脑", "笔记本", "平板"]), ("钱包", ["钱包"]), ("其他", []),
]
PLACES = ["图书馆", "食堂", "教学楼", "体育馆", "操场", "宿舍", "校医院", "活动中心", "自习室", "实验室", "机房"]


def api_key():
    return (os.environ.get("ZHIPU_API_KEY") or "").strip()


def _strip_json(text):
    if not text:
        return None
    cleaned = text.strip()
    cleaned = re.sub(r"^[^\[{]*", "", cleaned)
    start_obj, start_arr = cleaned.find("{"), cleaned.find("[")
    if start_obj < 0 and start_arr < 0:
        return None
    start = start_obj if start_obj >= 0 and (start_arr < 0 or start_obj < start_arr) else start_arr
    end = max(cleaned.rfind("}"), cleaned.rfind("]"))
    if end <= start:
        return None
    try:
        return json.loads(cleaned[start:end + 1])
    except Exception:
        return None


TITLE_NOISE = ("失物招领", "失物", "招领", "寻物", "丢失", "拾获", "捡到", "寻找")
CONTACT_PLACEHOLDER = ("提供", "无", "未", "如", "例", "暂", "未知", "(", "（", "？", "?")


def _clean_title(title):
    text = str(title or "").strip()
    for word in TITLE_NOISE:
        text = text.replace(word, "")
    return text.strip()[:12]


def _clean_contact(value):
    text = str(value or "").strip()
    if not text or len(text) > 24:
        return ""
    if any(bad in text for bad in CONTACT_PLACEHOLDER):
        return ""
    return text


def _clean_fields(data):
    if not isinstance(data, dict):
        return None
    features = data.get("features") or []
    if isinstance(features, str):
        features = [part for part in re.split(r"[、,，;；\s]+", features) if part]
    raw_type = str(data.get("action_type") or data.get("kind") or "").strip().lower()
    if raw_type.startswith("lost"):
        action_type = "lost"
    elif raw_type.startswith("found"):
        action_type = "found"
    else:
        action_type = "found"
    cleaned = {
        "action_type": action_type,
        "kind": action_type,
        "title": _clean_title(data.get("title")),
        "category": str(data.get("category") or "其他").strip()[:12],
        "color": str(data.get("color") or "").strip()[:8],
        "place": str(data.get("place") or "").strip()[:30],
        "features": [str(f).strip()[:20] for f in list(features)[:4] if str(f).strip()],
        "contact": _clean_contact(data.get("contact")),
    }
    if not cleaned["title"]:
        return None
    if cleaned["category"] not in CATEGORY_ENUM:
        lowered = cleaned["category"]
        mapped = ""
        for name, keys in CATEGORIES:
            if any(key in lowered or lowered in key for key in keys) or lowered == name:
                mapped = name
                break
        cleaned["category"] = mapped or "其他"
    return cleaned


def rule_extract(text):
    """本地兜底抽取：没有 Key 或接口挂了也不至于登记失败。"""
    raw = str(text or "")
    color = next((c for c in COLORS if c in raw), "")
    category = "其他"
    for name, keys in CATEGORIES:
        if any(key in raw for key in keys):
            category = name
            break
    place_hits = [p for p in PLACES if p in raw]
    room = re.search(r"([0-9]{2,4})\s*(教室|室|号)", raw)
    place = "".join(place_hits)
    if room:
        place = (place + " " + room.group(1)).strip()
    TIME_WORDS = ("昨天", "今天", "上午", "下午", "晚上", "中午", "刚才", "早上", "走的时候", "丢在", "落在", "丢了")
    ATTRIBUTES = ("磨损", "划痕", "裂", "脏", "贴纸", "挂件", "带", "内", "壳", "颜色", "牌", "号", "旧", "新")
    segments = [part.strip() for part in re.split(r"[，。,.；;、\s]+", raw) if 2 <= len(part.strip()) <= 12]
    picked = [s for s in segments if any(attr in s for attr in ATTRIBUTES) and not any(t in s for t in TIME_WORDS)]
    if not picked:
        picked = [s for s in segments if not any(t in s for t in TIME_WORDS)]
    features = picked[:3]
    action_type = "lost" if any(word in raw for word in ("丢", "找", "寻", "丢失", "遗失", "不见", "落在", "忘在", "遗忘")) else "found"
    title = "%s%s" % (color, category if category != "其他" else "物品")
    return {
        "action_type": action_type,
        "kind": action_type,
        "title": title[:20],
        "category": category,
        "color": color,
        "place": place[:30],
        "features": features,
        "contact": "",
    }


def llm_extract(text):
    key = api_key()
    if not key:
        return None, "no_key"
    payload = {
        "model": DEFAULT_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": USER_TEMPLATE.format(text=str(text)[:800])},
        ],
        "temperature": 0.1,
        "max_tokens": 400,
    }
    headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
    last = "error"
    for attempt in range(2):
        try:
            resp = requests.post(API_URL, json=payload, headers=headers, timeout=TIMEOUT)
            if resp.status_code in (401, 403):
                return None, "auth_failed"
            if resp.status_code == 429:
                last = "rate_limited"
                time.sleep(1.5)
                continue
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            fields = _clean_fields(_strip_json(content))
            if fields:
                return fields, "llm"
            last = "bad_output"
        except requests.Timeout:
            last = "timeout"
        except Exception as exc:
            last = "error:%s" % type(exc).__name__
        if attempt == 0:
            time.sleep(0.8)
    return None, last


def extract(text):
    fields, source = llm_extract(text)
    if fields:
        return fields, source
    fallback = rule_extract(text)
    return fallback, ("rule_fallback:" + source if source != "no_key" else "rule")
