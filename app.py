# -*- coding: utf-8 -*-
"""校园失物招领 · 云端版后端（Flask）。

接口：
  GET  /                 前端页面
  GET  /api/health       健康检查
  POST /api/register     登记（走大模型抽取）
  GET/POST /api/query    查询（只返回 status=active）
  GET  /api/items        记录列表（可按 status 过滤）
  POST /api/resolve      标记已找回（二次确认 + 姓名核验 + 审计留痕）
  POST /api/recover      撤销误操作，恢复为 active
"""
import hmac
import os
import re
import threading
import time

from flask import Flask, jsonify, render_template, request

import matcher
import zhipu_client
from store import Store

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def load_env_file():
    try:
        with open(os.path.join(BASE_DIR, ".env"), encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                name, value = line.split("=", 1)
                name = name.strip()
                value = value.strip().strip('"').strip("'")
                if name and name not in os.environ:
                    os.environ[name] = value
    except FileNotFoundError:
        pass


load_env_file()

app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False

store = Store()
MODEL = os.environ.get("ZHIPU_MODEL", "glm-4-flash")
ALLOW_CORS = os.environ.get("ALLOW_CORS", "1") == "1"
MAX_TEXT = int(os.environ.get("MAX_TEXT", "600"))
RATE_LIMIT = int(os.environ.get("RATE_LIMIT_PER_MIN", "12"))
STARTED_AT = time.time()

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")
PHONE_RE = re.compile(r"^1\d{10}$")

STATUS_ACTIVE = "active"
STATUS_RESOLVED = "resolved"
NAME_SUFFIXES = ("同学", "老师", "先生", "女士", "师傅", "学姐", "学长")

_index_rows = []
_index_key = None
_index_lock = threading.Lock()
_rate_lock = threading.Lock()
_rate_hits = {}


# ---------------------------------------------------------------- 工具函数
def mask_contact(text):
    text = (text or "").strip()
    if not text:
        return ""
    digits = re.sub(r"\D", "", text)
    if len(digits) >= 8:
        return digits[:3] + "****" + digits[-4:]
    if len(text) == 1:
        return text + "**"
    return text[0] + "***"


def mask_name(text):
    text = (text or "").strip()
    if not text:
        return ""
    if len(text) == 1:
        return text + "*"
    return text[0] + "*" * (len(text) - 1)


def normalize_name(text):
    text = (text or "").strip().replace("　", "")
    text = re.sub(r"\s+", "", text)
    return text.lower()


def name_matches(operator, owner):
    operator = normalize_name(operator)
    owner = normalize_name(owner)
    if not operator or not owner:
        return None
    if operator == owner:
        return True
    for suffix in NAME_SUFFIXES:
        operator = operator.replace(suffix, "") if operator.endswith(suffix) else operator
        owner = owner.replace(suffix, "") if owner.endswith(suffix) else owner
    return operator == owner


def extract_owner_name(text):
    text = str(text or "")
    for pattern in (r"我叫([\u4e00-\u9fa5]{2,4})", r"我是([\u4e00-\u9fa5]{2,4})",
                    r"联系人[:：]?\s*([\u4e00-\u9fa5]{2,4})", r"([\u4e00-\u9fa5]{1,3}(?:同学|老师))"):
        found = re.search(pattern, text)
        if found:
            return found.group(1)
    return ""


def build_detail(fields):
    parts = []
    if fields.get("place"):
        parts.append("地点：%s" % fields["place"])
    if fields.get("features"):
        parts.append("特征：%s" % "、".join(fields["features"]))
    contact = mask_contact(fields.get("contact"))
    parts.append("联系：%s" % contact if contact else "联系：通过平台留言")
    return " · ".join(parts)


def flush_store():
    with store.lock:
        store._flush()


def fill_defaults(item):
    changed = False
    if item.get("status") not in (STATUS_ACTIVE, STATUS_RESOLVED):
        item["status"] = STATUS_ACTIVE
        changed = True
    if not isinstance(item.get("audit_log"), list):
        item["audit_log"] = []
        changed = True
    if not isinstance(item.get("apply_log"), list):
        item["apply_log"] = []
        changed = True
    if "owner_name" not in item:
        item["owner_name"] = extract_owner_name(item.get("raw_text", ""))
        changed = True
    return changed


def ensure_schema():
    touched = False
    for item in store.all():
        if fill_defaults(item):
            touched = True
    if touched:
        flush_store()


def find_item(item_id):
    for item in store.all():
        if item.get("id") == item_id:
            return item
    return None


def update_item(item_id, mutator):
    with store.lock:
        if hasattr(store, "maybe_reload"):
            store.maybe_reload()
        for item in store.items:
            if item.get("id") == item_id:
                mutator(item)
                store._flush()
                return item
    return None


def active_rows():
    return [row for row in index_rows() if row["item"].get("status") == STATUS_ACTIVE]


def index_rows():
    global _index_rows, _index_key
    items = store.all()
    active = [item for item in items if item.get("status") == STATUS_ACTIVE]
    key = (len(items), len(active), sum(len(item.get("audit_log") or []) for item in items))
    with _index_lock:
        if _index_key != key:
            _index_rows = matcher.build_index(active)
            _index_key = key
        return _index_rows


def public_item(item):
    copy = dict(item)
    audit = []
    for entry in item.get("audit_log") or []:
        row = dict(entry)
        if row.get("contact"):
            row["contact"] = mask_contact(row["contact"])
        if row.get("operator"):
            row["operator"] = mask_name(row["operator"])
        audit.append(row)
    copy["audit_log"] = audit
    applies = []
    for entry in item.get("apply_log") or []:
        row = dict(entry)
        if row.get("contact"):
            row["contact"] = mask_contact(row["contact"])
        if row.get("applicant"):
            row["applicant"] = mask_name(row["applicant"])
        applies.append(row)
    copy["apply_log"] = applies
    copy["apply_count"] = len(item.get("apply_log") or [])
    copy["owner_hint"] = mask_name(item.get("owner_name", ""))
    copy["has_owner"] = bool((item.get("owner_name") or "").strip())
    return copy


def rate_ok(ip, limit=None, window=60):
    limit = limit or RATE_LIMIT
    now = time.time()
    with _rate_lock:
        bucket = _rate_hits.setdefault(ip, [])
        bucket[:] = [t for t in bucket if now - t < window]
        if len(bucket) >= limit:
            return False
        bucket.append(now)
        return True


ensure_schema()


@app.after_request
def add_headers(resp):
    if ALLOW_CORS:
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


# ---------------------------------------------------------------- 页面与健康
@app.route("/")
def home():
    items = store.all()
    return render_template(
        "index.html",
        model=MODEL,
        llm_ready=bool(zhipu_client.api_key()),
        active=len([i for i in items if i.get("status") == STATUS_ACTIVE]),
        resolved=len([i for i in items if i.get("status") == STATUS_RESOLVED]),
    )


@app.route("/api/health")
def health():
    items = store.all()
    return jsonify({
        "ok": True,
        "service": "campus-lostfound",
        "items": len(items),
        "active": len([i for i in items if i.get("status") == STATUS_ACTIVE]),
        "resolved": len([i for i in items if i.get("status") == STATUS_RESOLVED]),
        "model": MODEL,
        "llm_ready": bool(zhipu_client.api_key()),
        "uptime_sec": round(time.time() - STARTED_AT, 1),
    })


# ---------------------------------------------------------------- 登记
@app.route("/api/register", methods=["POST", "OPTIONS"])
def register():
    if request.method == "OPTIONS":
        return ("", 204)
    ip = (request.headers.get("X-Forwarded-For") or request.remote_addr or "?").split(",")[0].strip()
    if not rate_ok(ip):
        return jsonify({"ok": False, "error": "登记太频繁了，请稍后再试"}), 429

    payload = request.get_json(silent=True) or {}
    text = (payload.get("text") or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "请先写一段描述"}), 400
    text = text[:MAX_TEXT]

    started = time.time()
    fields, source = zhipu_client.extract(text)
    if payload.get("contact"):
        fields["contact"] = str(payload["contact"])[:40]
    owner = (payload.get("owner_name") or "").strip() or extract_owner_name(text)

    fields["raw_text"] = text
    fields["owner_name"] = owner
    fields["detail"] = build_detail(fields)
    fields["extracted_by"] = source
    fields["status"] = STATUS_ACTIVE
    fields["audit_log"] = []
    item = store.add(fields)
    index_rows()

    return jsonify({
        "ok": True,
        "item": public_item(item),
        "extracted_by": source,
        "llm_used": source == "llm",
        "cost_ms": round((time.time() - started) * 1000, 1),
    })


# ---------------------------------------------------------------- 查询
@app.route("/api/query", methods=["GET", "POST", "OPTIONS"])
def query():
    if request.method == "OPTIONS":
        return ("", 204)
    if request.method == "POST":
        payload = request.get_json(silent=True) or {}
        q = (payload.get("q") or payload.get("query") or "").strip()
        limit = int(payload.get("limit") or 10)
    else:
        q = (request.args.get("q") or "").strip()
        limit = int(request.args.get("limit") or 10)
    limit = max(1, min(limit, 30))
    if not q:
        return jsonify({"ok": False, "error": "请输入要找的物品"}), 400

    started = time.time()
    rows = active_rows()
    cards = matcher.search(store.all(), rows, q, limit=limit)
    by_id = {row["item"]["id"]: row["item"] for row in rows}
    for card in cards:
        item = by_id.get(card["id"], {})
        card["owner_hint"] = mask_name(item.get("owner_name", ""))
        card["has_owner"] = bool((item.get("owner_name") or "").strip())
        card["apply_count"] = len(item.get("apply_log") or [])
    cost = round((time.time() - started) * 1000, 2)
    return jsonify({"ok": True, "query": q, "scanned": len(rows), "cost_ms": cost, "cards": cards})


@app.route("/api/items")
def items():
    limit = max(1, min(int(request.args.get("limit") or 50), 200))
    wanted = (request.args.get("status") or "active").strip().lower()
    rows = store.all()
    if wanted in (STATUS_ACTIVE, STATUS_RESOLVED):
        rows = [item for item in rows if item.get("status") == wanted]
    rows = sorted(rows, key=lambda r: r.get("created_at", ""), reverse=True)[:limit]
    return jsonify({"ok": True, "total": store.count(), "status": wanted, "items": [public_item(r) for r in rows]})


# ---------------------------------------------------------------- 状态流转
@app.route("/api/resolve", methods=["POST", "OPTIONS"])
def resolve():
    if request.method == "OPTIONS":
        return ("", 204)
    payload = request.get_json(silent=True) or {}
    item_id = str(payload.get("id") or "").strip()
    operator = str(payload.get("operator") or "").strip()
    contact = str(payload.get("contact") or "").strip()
    if not item_id:
        return jsonify({"ok": False, "error": "缺少物品 ID"}), 400
    if not operator:
        return jsonify({"ok": False, "error": "请填写操作人姓名"}), 400
    if not contact:
        return jsonify({"ok": False, "error": "请填写联系方式"}), 400
    if len(operator) > 20 or len(contact) > 40:
        return jsonify({"ok": False, "error": "姓名或联系方式过长"}), 400

    item = find_item(item_id)
    if item is None:
        return jsonify({"ok": False, "error": "没有找到这条记录"}), 404
    if item.get("status") == STATUS_RESOLVED:
        return jsonify({"ok": False, "error": "这条记录已经是已找回状态"}), 409

    owner = (item.get("owner_name") or "").strip()
    matched = name_matches(operator, owner)
    if matched is False:
        return jsonify({
            "ok": False,
            "error": "姓名与登记人不一致，无法确认为失主",
            "owner_hint": mask_name(owner),
        }), 403

    now = time.strftime("%Y-%m-%d %H:%M:%S")

    def mutate(target):
        target["status"] = STATUS_RESOLVED
        target["resolved_at"] = now
        target.setdefault("audit_log", []).append({
            "action": "resolve",
            "operator": operator,
            "contact": contact,
            "at": now,
            "name_checked": matched is True,
        })

    updated = update_item(item_id, mutate)
    if updated is None:
        return jsonify({"ok": False, "error": "更新失败，请重试"}), 500
    index_rows()
    return jsonify({
        "ok": True,
        "message": "已标记为已找回",
        "name_checked": matched is True,
        "item": public_item(updated),
    })


@app.route("/api/recover", methods=["POST", "OPTIONS"])
def recover():
    if request.method == "OPTIONS":
        return ("", 204)
    payload = request.get_json(silent=True) or {}
    item_id = str(payload.get("id") or "").strip()
    operator = str(payload.get("operator") or "").strip() or "系统撤销"
    contact = str(payload.get("contact") or "").strip() or "-"
    if not item_id:
        return jsonify({"ok": False, "error": "缺少物品 ID"}), 400

    item = find_item(item_id)
    if item is None:
        return jsonify({"ok": False, "error": "没有找到这条记录"}), 404
    if item.get("status") != STATUS_RESOLVED:
        return jsonify({"ok": False, "error": "这条记录当前不是已找回状态"}), 409

    now = time.strftime("%Y-%m-%d %H:%M:%S")

    def mutate(target):
        target["status"] = STATUS_ACTIVE
        target["recovered_at"] = now
        target.pop("resolved_at", None)
        target.setdefault("audit_log", []).append({
            "action": "recover",
            "operator": operator,
            "contact": contact,
            "at": now,
        })

    updated = update_item(item_id, mutate)
    if updated is None:
        return jsonify({"ok": False, "error": "更新失败，请重试"}), 500
    index_rows()
    return jsonify({"ok": True, "message": "已撤销，记录回到可查询状态", "item": public_item(updated)})


@app.route("/api/apply-contact", methods=["POST", "OPTIONS"])
def apply_contact():
    if request.method == "OPTIONS":
        return ("", 204)
    payload = request.get_json(silent=True) or {}
    item_id = str(payload.get("id") or "").strip()
    applicant = str(payload.get("name") or payload.get("applicant") or "").strip()
    contact = str(payload.get("contact") or "").strip()
    if not item_id:
        return jsonify({"ok": False, "error": "缺少物品 ID"}), 400
    if not applicant:
        return jsonify({"ok": False, "error": "请填写您的姓名"}), 400
    if not contact:
        return jsonify({"ok": False, "error": "请填写您的联系方式"}), 400
    if len(applicant) > 20 or len(contact) > 40:
        return jsonify({"ok": False, "error": "姓名或联系方式过长"}), 400

    phone = re.sub(r"[\s\-]", "", contact)
    if not PHONE_RE.match(phone):
        return jsonify({"ok": False, "error": "号码格式不正确，请输入 1 开头的 11 位手机号"}), 400
    contact = phone

    ip = (request.headers.get("X-Forwarded-For") or request.remote_addr or "?").split(",")[0].strip()
    if not rate_ok(ip + ":apply", limit=20):
        return jsonify({"ok": False, "error": "提交太频繁了，请稍后再试"}), 429

    item = find_item(item_id)
    if item is None:
        return jsonify({"ok": False, "error": "没有找到这条记录"}), 404
    if item.get("status") == STATUS_RESOLVED:
        return jsonify({"ok": False, "error": "该记录已标记为已找回，无需再申请联系"}), 409

    now = time.strftime("%Y-%m-%d %H:%M:%S")

    def mutate(target):
        target.setdefault("apply_log", []).append({
            "action": "apply_contact",
            "applicant": applicant,
            "contact": contact,
            "at": now,
            "state": "pending",
        })

    updated = update_item(item_id, mutate)
    if updated is None:
        return jsonify({"ok": False, "error": "提交失败，请重试"}), 500
    index_rows()
    return jsonify({
        "ok": True,
        "message": "申请已提交！管理员核实后，会将完整联系方式发送给您。请留意通知。",
        "apply_count": len(updated.get("apply_log") or []),
        "item": public_item(updated),
    })


# ---------------------------------------------------------------- 管理员
def admin_ok(candidate):
    candidate = (candidate or "").strip()
    return bool(candidate) and hmac.compare_digest(candidate, ADMIN_PASSWORD)


@app.route("/api/admin-applications", methods=["GET", "POST", "OPTIONS"])
def admin_applications():
    if request.method == "OPTIONS":
        return ("", 204)
    payload = request.get_json(silent=True) or {}
    supplied = (request.args.get("password")
                or payload.get("password")
                or request.headers.get("X-Admin-Password")
                or "")
    if not admin_ok(supplied):
        return jsonify({"ok": False, "error": "管理员密码不正确"}), 401

    rows = []
    for item in store.all():
        applies = item.get("apply_log") or []
        if not applies:
            continue
        rows.append({
            "id": item.get("id"),
            "title": item.get("title", ""),
            "category": item.get("category", ""),
            "place": item.get("place", ""),
            "status": item.get("status", STATUS_ACTIVE),
            "created_at": item.get("created_at", ""),
            "apply_count": len(applies),
            "latest_at": applies[-1].get("at", ""),
            "applications": [
                {"applicant": row.get("applicant", ""),
                 "contact": row.get("contact", ""),
                 "at": row.get("at", ""),
                 "state": row.get("state", "pending")}
                for row in applies
            ],
        })
    rows.sort(key=lambda r: r.get("latest_at") or "", reverse=True)
    return jsonify({"ok": True, "total": len(rows), "items": rows})


@app.errorhandler(404)
def not_found(_error):
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": "not found"}), 404
    return render_template("index.html", model=MODEL, llm_ready=bool(zhipu_client.api_key()), active=0, resolved=0), 404


@app.errorhandler(500)
def server_error(_error):
    return jsonify({"ok": False, "error": "服务器内部错误"}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
