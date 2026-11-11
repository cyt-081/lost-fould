# -*- coding: utf-8 -*-
"""隐私数据清洗：把真实手机号 / 邮箱 / 证件号 / 姓名 / 原始描述替换成虚构占位。

设计要点：
  1. 幂等：已经是 1380000xxxx 这类占位号、或已在虚构名单里的姓名，不会被二次改写；
  2. 保留业务可用性：清洗后仍能演示"姓名核验"和"脱敏展示"；
  3. 只动内容，不动 id / 状态 / 时间戳。
"""
import re

PHONE_RE = re.compile(r"1[3-9]\d{9}")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
ID_RE = re.compile(r"(?<!\d)\d{15,18}(?!\d)")
FAKE_PREFIX = "1380000"
FAKE_NAMES = ("张同学", "李同学", "王同学", "赵同学", "刘同学", "陈同学",
              "杨同学", "黄同学", "周同学", "吴同学")
RAW_MARK = "（原始描述已清理）"


def fake_phone(state):
    state["phone"] = state.get("phone", 0) + 1
    return "138%08d" % state["phone"]


def fake_email(state):
    state["email"] = state.get("email", 0) + 1
    return "user%02d@example.com" % state["email"]


def fake_name(state):
    index = state.get("name", 0)
    state["name"] = index + 1
    return FAKE_NAMES[index % len(FAKE_NAMES)]


def mask_phone(text):
    digits = re.sub(r"\D", "", str(text or ""))
    if len(digits) >= 8:
        return digits[:3] + "****" + digits[-4:]
    return str(text or "")


def clean_inline(text, state):
    """把字符串里夹杂的手机号/邮箱/证件号替换掉。"""
    if not isinstance(text, str) or not text:
        return text
    def _phone(found):
        value = found.group(0)
        if value.startswith(FAKE_PREFIX):
            return value
        return fake_phone(state)
    text = PHONE_RE.sub(_phone, text)
    text = EMAIL_RE.sub(lambda found: found.group(0) if found.group(0).endswith("example.com") else fake_email(state), text)
    text = ID_RE.sub(lambda found: found.group(0) if set(found.group(0)) == {"0"} else "0000" + "0" * (len(found.group(0)) - 4), text)
    return text


def scrub_contact(value, state):
    if not value:
        return ""
    text = str(value)
    if "@" in text:
        if text.endswith("example.com"):
            return text
        return fake_email(state)
    digits = re.sub(r"\D", "", text)
    if digits.startswith(FAKE_PREFIX):
        return digits
    return fake_phone(state)


def scrub_item(item, state):
    """就地清洗单条记录，返回是否发生了变化。"""
    changed = False

    contact = scrub_contact(item.get("contact"), state)
    if contact != (item.get("contact") or ""):
        item["contact"] = contact
        changed = True

    owner = str(item.get("owner_name") or "").strip()
    if owner and owner not in FAKE_NAMES:
        item["owner_name"] = clean_inline(fake_name(state), state)
        changed = True

    for key in ("title", "place", "color", "category"):
        if isinstance(item.get(key), str):
            new = clean_inline(item[key], state)
            if new != item[key]:
                item[key] = new
                changed = True

    features = item.get("features")
    if isinstance(features, list):
        cleaned = [clean_inline(str(f), state) for f in features]
        if cleaned != features:
            item["features"] = cleaned
            changed = True

    raw = str(item.get("raw_text") or "")
    if raw and not raw.startswith(RAW_MARK):
        item["raw_text"] = "%s物品：%s；特征：%s；地点：%s" % (
            RAW_MARK, item.get("title", ""),
            "、".join(map(str, item.get("features") or [])) or "无",
            item.get("place", "") or "未填写")
        state["raw"] = state.get("raw", 0) + 1
        changed = True

    for log_key, name_key in (("audit_log", "operator"), ("apply_log", "applicant")):
        entries = item.get(log_key)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            who = str(entry.get(name_key) or "").strip()
            if who and who not in FAKE_NAMES:
                entry[name_key] = fake_name(state)
                state[name_key] = state.get(name_key, 0) + 1
                changed = True
            if entry.get("contact"):
                new_contact = scrub_contact(entry["contact"], state)
                if new_contact != entry["contact"]:
                    entry["contact"] = new_contact
                    changed = True

    parts = []
    if item.get("place"):
        parts.append("地点：%s" % item["place"])
    if item.get("features"):
        parts.append("特征：%s" % "、".join(map(str, item["features"])))
    parts.append("联系：%s" % (mask_phone(item.get("contact")) if item.get("contact") else "通过平台留言"))
    detail = " · ".join(parts)
    if detail != item.get("detail"):
        item["detail"] = detail
        state["detail"] = state.get("detail", 0) + 1
        changed = True

    return changed


def scrub_items(items):
    state = {}
    touched = 0
    for item in items:
        if isinstance(item, dict) and scrub_item(item, state):
            touched += 1
    return touched, state


def audit(items):
    """清洗效果核对：返回残留的真实敏感数据统计。"""
    text = ""
    for item in items:
        text += " " + str(item.get("contact") or "")
        text += " " + str(item.get("raw_text") or "")
        text += " " + " ".join(map(str, item.get("features") or []))
        for log_key, name_key in (("audit_log", "operator"), ("apply_log", "applicant")):
            for entry in item.get(log_key) or []:
                if isinstance(entry, dict):
                    text += " " + str(entry.get(name_key) or "") + " " + str(entry.get("contact") or "")
    real_phones = [p for p in PHONE_RE.findall(text) if not p.startswith(FAKE_PREFIX)]
    return {
        "残留真实手机号": len(real_phones),
        "残留邮箱": len([e for e in EMAIL_RE.findall(text) if not e.endswith("example.com")]),
        "占位手机号数": len([p for p in PHONE_RE.findall(text) if p.startswith(FAKE_PREFIX)]),
    }
