# -*- coding: utf-8 -*-
"""失物招领数据存储：单个 JSON 文件 + 原子写入 + 线程锁。

并发策略：读全部走内存列表；写入时持锁、写临时文件再 os.replace，
保证多线程（gunicorn threads）下文件不会写坏。
"""
import json
import os
import threading
import time
import uuid
from pathlib import Path

DATA_PATH = Path(os.environ.get("DATA_PATH", Path(__file__).resolve().parent / "data" / "items.json"))
SEED_DEMO = os.environ.get("SEED_DEMO", "1") == "1"

SEEDS = [
    {"kind": "found", "title": "黑色长柄雨伞", "category": "雨伞", "color": "黑色",
     "place": "图书馆二楼", "features": ["长柄", "伞柄有磨损"],
     "detail": "图书馆二楼失物柜 · 伞柄有磨损 · 登记人 李** · 联系方式不公开"},
    {"kind": "found", "title": "蓝色保温杯", "category": "保温杯", "color": "蓝色",
     "place": "三教 305", "features": ["杯身有贴纸"],
     "detail": "三教 305 教室讲台 · 杯身有贴纸 · 登记人 王**（教师）· 联系方式不公开"},
    {"kind": "found", "title": "白色无线耳机", "category": "耳机", "color": "白色",
     "place": "体育馆羽毛球场", "features": ["带充电盒"],
     "detail": "体育馆羽毛球场 3 号场边 · 带充电盒 · 登记人 陈** · 联系方式不公开"},
    {"kind": "found", "title": "校园卡（学号 20**）", "category": "校园卡", "color": "",
     "place": "二食堂", "features": ["芯片面有划痕"],
     "detail": "二食堂收银台 · 芯片面有划痕 · 请凭有效证件到保卫处核验后领取"},
    {"kind": "found", "title": "黑色双肩包", "category": "双肩包", "color": "黑色",
     "place": "一号教学楼 302", "features": ["内有笔记本和钥匙"],
     "detail": "一号教学楼 302 自习室 · 内有笔记本和钥匙 · 登记人 周** · 联系方式不公开"},
]


class Store:
    def __init__(self, path=None, seed=None):
        self.path = Path(path) if path else DATA_PATH
        self.lock = threading.RLock()
        self.items = []
        self.seed = SEED_DEMO if seed is None else seed
        self._key = (0, 0)
        self._load()

    def _stat_key(self):
        try:
            info = self.path.stat()
            return (info.st_mtime_ns, info.st_size)
        except Exception:
            return (0, 0)

    def maybe_reload(self):
        """别的进程（或多 worker）写过文件时重新载入，保证查询能看见最新登记。"""
        with self.lock:
            if self._stat_key() != self._key:
                self._load()
                return True
            return False

    def _load(self):
        with self.lock:
            self._key = self._stat_key()
            if self.path.exists():
                try:
                    raw = json.loads(self.path.read_text(encoding="utf-8"))
                    self.items = raw.get("items", []) if isinstance(raw, dict) else list(raw)
                    return
                except Exception:
                    self.items = []
            self.items = []
            if self.seed:
                now = time.strftime("%Y-%m-%d %H:%M:%S")
                for row in SEEDS:
                    item = dict(row)
                    item.setdefault("contact", "")
                    item["id"] = uuid.uuid4().hex[:12]
                    item["created_at"] = now
                    item["extracted_by"] = "seed"
                    self.items.append(item)
                self._flush()

    def _flush(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = {"version": 1, "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"), "items": self.items}
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)
        self._key = self._stat_key()

    def all(self):
        self.maybe_reload()
        with self.lock:
            return list(self.items)

    def count(self):
        self.maybe_reload()
        with self.lock:
            return len(self.items)

    def add(self, record):
        with self.lock:
            item = dict(record)
            item["id"] = uuid.uuid4().hex[:12]
            item["created_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            self.items.append(item)
            self._flush()
            return item
