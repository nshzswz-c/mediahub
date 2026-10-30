"""本地有声书库：直接扫描 NAS 上的音频目录，不依赖 Leelaa。

目录约定（两种都支持）：
    <root>/<书名>/<音频文件>              平铺
    <root>/<书名>/<卷目录>/<音频文件>      分卷，章节标题会自动带卷名

对外暴露的方法名与 main.py 里原来的 Leelaa 客户端一致，
这样语音入口那套逻辑几乎不用改。
"""
from __future__ import annotations

import hashlib
import json
import re
import socket
import threading
import time
from pathlib import Path
from typing import Callable, Optional

AUDIO_EXT = {
    ".mp3", ".m4a", ".aac", ".flac", ".wav", ".ogg", ".m4b",
    ".wma", ".ape", ".opus", ".aiff", ".alac",
}
IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
COVER_STEMS = ("cover", "folder", "front", "poster", "album", "封面", "专辑")

_NUM = re.compile(r"(\d+)")
_BRACKET_WM = re.compile(r"[\[【][^\]】]{0,40}(www\.|\.com|\.net|\.org|hackfans|盘搜|更多|下载)[^\]】]{0,40}[\]】]", re.I)
_BRACKET_ANY = re.compile(r"[\[【][^\]】]{0,30}[\]】]")
_LEAD_NUM = re.compile(r"^\s*\d{1,5}\s*[-_.、]\s*")


def _natkey(s: str):
    """自然排序：'第10集' 排在 '第9集' 之后"""
    return [int(t) if t.isdigit() else t.lower() for t in _NUM.split(s)]


def _stable_id(s: str) -> int:
    """把路径映射成稳定的整数 id，前端/接口里当主键用"""
    return int(hashlib.md5(s.encode("utf-8")).hexdigest()[:12], 16) % 2_000_000_000


def clean_text(stem: str) -> str:
    s = _BRACKET_WM.sub("", stem)
    s = _BRACKET_ANY.sub("", s)
    s = _LEAD_NUM.sub("", s)
    s = s.replace("_", " ")
    s = re.sub(r"\s+", " ", s)
    return s.strip(" -·.") or stem


def parse_book_name(name: str):
    """从目录名里拆出 (书名, 作者, 演播)

    '绍宋.榴弹怕水.演播伍壹先生.2021.AAC.96~128kbps-Zm' -> ('绍宋', '榴弹怕水', '伍壹先生')
    '周建龙 - 鬼吹灯I和II合集 全套'                      -> ('鬼吹灯I和II合集', '', '周建龙')
    """
    s = (name or "").strip()
    author = narrator = ""
    if "." in s:
        parts = [p.strip() for p in s.split(".") if p.strip()]
        if len(parts) >= 2:
            s = parts[0]
            for p in parts[1:]:
                low = p.lower()
                if p.startswith("演播"):
                    narrator = narrator or p[2:].strip()
                elif re.fullmatch(r"(19|20)\d{2}", p):
                    continue
                elif "kbps" in low or "aac" in low or "mp3" in low or "m4a" in low:
                    continue
                elif re.search(r"(著|作者|演播|主编)", p):
                    continue
                elif not author and re.fullmatch(r"[\u4e00-\u9fa5·]{2,10}", p):
                    author = p
    else:
        m = re.match(r"^(.{2,12}?)\s*[-–—]\s*(.+)$", s)
        if m:
            narrator = m.group(1).strip()
            s = m.group(2).strip()
    s = re.sub(r"\s*(全套|合集|全本|完)$", lambda m: "" if m.group(1) == "全套" else m.group(1), s).strip()
    return s or name, author, narrator


def _vol_short(name: str) -> str:
    return clean_text(re.sub(r"\([^)]*\)|（[^）]*）|\[[^\]]*\]|【[^】]*】", "", name))


def chapter_title(stem: str, book_title: str) -> str:
    """章节标题：去掉书名重复前缀，末尾的数字转成「第 N 集」

    '绍宋-第0000集：简介'      -> '第0集：简介'
    '鬼吹灯-第1部-精绝古城9'   -> '第1部-精绝古城 第9集'
    """
    s = stem
    cands = []
    if book_title:
        cands.append(book_title)
        cands.append(book_title.replace(" ", ""))
        core = re.match(r"^[\u4e00-\u9fa5]{2,}", book_title)
        if core:
            cands.append(core.group(0))
    hit = False
    for pref in sorted({c for c in cands if c}, key=len, reverse=True):
        if s.startswith(pref):
            s = s[len(pref):].lstrip(" -·_—")
            hit = True
            break
    if not hit:
        s = s.lstrip(" -·_—")
    elif re.match(r"^\d", s):
        s = stem          # 「鬼吹灯2-巫峡棺山」这类：删前缀会把序号留下，干脆整体保留
    m = re.match(r"^(.*?)(\d{1,4})$", s)
    if m and m.group(1).strip(" -·_"):
        s = f"{m.group(1).strip(' -·_')} 第{int(m.group(2))}集"
    s = re.sub(r"第0+(\d)", r"第\1", s)
    return s.strip() or stem


def lan_ip() -> str:
    """拿本机在局域网里的 IP（host 网络下就是 NAS 的 IP），给小爱音箱播直链用"""
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("223.5.5.5", 80))
        return s.getsockname()[0]
    except Exception:  # noqa
        return "127.0.0.1"
    finally:
        try:
            s and s.close()
        except Exception:  # noqa
            pass


class ProgressStore:
    """播放进度：一个小 JSON 文件，够用且不依赖数据库"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text("utf-8"))
        except Exception:  # noqa
            return {}

    def _save(self, d: dict):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(d, ensure_ascii=False, indent=1), "utf-8")
        except Exception:  # noqa
            pass

    def get(self, bid) -> dict:
        return (self._load().get(str(bid)) or {})

    def set(self, bid, chapter_id=None, position=None, chapter_index=None, chapter_title=""):
        with self._lock:
            d = self._load()
            cur = d.get(str(bid)) or {}
            if chapter_id is not None:
                cur["chapter_id"] = chapter_id
            if chapter_index is not None:
                cur["chapter_index"] = chapter_index
            if chapter_title:
                cur["chapter_title"] = chapter_title
            if position is not None:
                cur["last_position"] = float(position)
            cur["updated"] = time.time()
            d[str(bid)] = cur
            self._save(d)
            return cur

    def recent(self, limit: int = 20) -> list:
        d = self._load()
        rows = sorted(d.items(), key=lambda kv: kv[1].get("updated") or 0, reverse=True)
        return [{"book_id": int(k), **v} for k, v in rows[:limit]]


class LocalBooks:
    """扫目录得到的书库。结构缓存 30 秒，避免每次请求都走一遍磁盘。"""

    CACHE_TTL = 30

    def __init__(self, root_getter: Callable[[], Path], progress: ProgressStore, base_url_getter: Callable[[], str]):
        self._root_getter = root_getter
        self._prog = progress
        self._base_url_getter = base_url_getter
        self._cache: dict = {}
        self._cache_at: float = 0
        self._lock = threading.Lock()

    # ---------- 扫描 ----------
    def root(self) -> Path:
        try:
            return Path(self._root_getter())
        except Exception:  # noqa
            return Path("/audiobooks")

    def _cover_of(self, d: Path) -> Optional[Path]:
        try:
            entries = sorted(d.iterdir(), key=lambda p: p.name)
        except Exception:  # noqa
            return None
        for p in entries:
            if p.is_file() and p.suffix.lower() in IMG_EXT and p.stem.lower() in COVER_STEMS:
                return p
        for p in entries:
            if p.is_file() and p.suffix.lower() in IMG_EXT:
                return p
        return None

    def _audio_of(self, d: Path) -> list:
        out = []
        try:
            for p in d.rglob("*"):
                if p.is_file() and p.suffix.lower() in AUDIO_EXT:
                    out.append(p)
        except Exception:  # noqa
            pass
        return out

    def _vol_short(self, name: str) -> str:
        return clean_text(re.sub(r"\([^)]*\)|（[^）]*）|\[[^\]]*\]|【[^】]*】", "", name))

    def scan(self, force: bool = False) -> dict:
        with self._lock:
            if not force and self._cache and (time.time() - self._cache_at) < self.CACHE_TTL:
                return self._cache
            root = self.root()
            books: dict = {}
            if not root.exists():
                self._cache, self._cache_at = {}, time.time()
                return {}
            try:
                dirs = sorted([p for p in root.iterdir() if p.is_dir()], key=lambda p: _natkey(p.name))
            except Exception:  # noqa
                dirs = []
            for d in dirs:
                files = self._audio_of(d)
                if not files:
                    continue
                title, author, narrator = parse_book_name(d.name)
                bid = _stable_id(str(d.name))
                chs = []
                for f in sorted(files, key=lambda p: _natkey(str(p.relative_to(d)))):
                    rel = f.relative_to(d)
                    stem = clean_text(f.stem)
                    if len(rel.parts) > 1:
                        vol = _vol_short(rel.parts[-2])
                        # 只有文件名本身太简略（如 01.mp3）时才补卷名，避免和自带卷名的文件重复
                        if vol and len(stem) <= 8 and vol not in stem:
                            stem = f"{vol} · {stem}"
                    stem = chapter_title(stem, title)
                    chs.append({
                        "id": _stable_id(str(d.name) + "/" + str(rel)),
                        "title": stem,
                        "file": f,
                    })
                for i, c in enumerate(chs):
                    c["chapter_index"] = i + 1
                total = sum(1 for _ in chs)
                books[bid] = {
                    "id": bid,
                    "dir": d,
                    "dir_name": d.name,
                    "title": title,
                    "author": author,
                    "narrator": narrator,
                    "chapter_count": total,
                    "chapters": chs,
                    "cover": self._cover_of(d),
                }
            self._cache, self._cache_at = books, time.time()
            return books

    # ---------- 查询 ----------
    def list_books(self, q: str = "") -> list:
        books = list(self.scan().values())
        q = (q or "").strip().lower()
        if q:
            books = [b for b in books
                     if q in b["title"].lower() or q in (b["author"] or "").lower()
                     or q in (b["narrator"] or "").lower()]
        out = []
        for b in books:
            prog = self._prog.get(b["id"])
            out.append(self._brief(b, prog))
        out.sort(key=lambda x: (-(x.get("last_played") or 0), x["title"]))
        return out

    def _brief(self, b: dict, prog: Optional[dict] = None) -> dict:
        prog = prog if prog is not None else self._prog.get(b["id"])
        idx = prog.get("chapter_index") or 0
        pct = 0.0
        if b["chapter_count"] and idx:
            pct = min(1.0, idx / b["chapter_count"])
        return {
            "id": b["id"],
            "title": b["title"],
            "author": b["author"],
            "narrator": b["narrator"],
            "desc": f"{b['dir_name']}",
            "cover": f"/api/books/{b['id']}/cover",
            "chapters": b["chapter_count"],
            "duration": 0,
            "progress": pct,
            "last_chapter": prog.get("chapter_title") or "",
            "category": "",
            "last_played": prog.get("updated") or 0,
        }

    def search(self, kw: str) -> list:
        return self.list_books(kw)

    def get(self, bid) -> Optional[dict]:
        return self.scan().get(int(bid))

    def chapters_of(self, bid) -> list:
        b = self.get(bid)
        if not b:
            return []
        return [{"id": c["id"], "chapter_index": c["chapter_index"],
                 "title": c["title"], "duration": 0} for c in b["chapters"]]

    def chapter_by_id(self, bid, cid) -> Optional[dict]:
        b = self.get(bid)
        if not b:
            return None
        for c in b["chapters"]:
            if str(c["id"]) == str(cid):
                return c
        return None

    # ---------- 与小爱音箱 / 语音入口兼容的一层 ----------
    def base_url(self) -> str:
        u = ""
        try:
            u = (self._base_url_getter() or "").strip()
        except Exception:  # noqa
            u = ""
        u = u or f"http://{lan_ip()}:8899"
        return u.rstrip("/")

    def stream_url(self, bid, cid) -> str:
        return f"{self.base_url()}/api/books/{bid}/stream/{cid}"

    async def history(self) -> list:
        out = []
        for row in self._prog.recent(20):
            b = self.get(row["book_id"])
            if not b:
                continue
            out.append({
                "id": b["id"],
                "title": b["title"],
                "author": b["author"],
                "cover": f"/api/books/{b['id']}/cover",
                "book_id": b["id"],
                "book_title": b["title"],
                "book": {"id": b["id"], "title": b["title"],
                         "author": b["author"], "narrator": b["narrator"]},
                "chapter_id": row.get("chapter_id"),
                "chapter_title": row.get("chapter_title") or "",
                "last_position": row.get("last_position") or 0,
                "progress": row.get("chapter_index", 0) / max(1, b["chapter_count"]),
                "last_played_at": row.get("updated"),
            })
        return out

    async def search_books(self, name: str) -> list:
        return self.search(name)

    async def chapters(self, bid) -> list:
        return self.chapters_of(bid)

    async def progress_of(self, bid) -> dict:
        return self._prog.get(bid)

    async def progress(self, bid) -> dict:      # 与 Leelaa 客户端同名，语音入口不用改
        return self._prog.get(bid)

    async def report_progress(self, bid, chapter_id=None, position=None, **kw):
        ix = None
        ttl = ""
        c = self.chapter_by_id(bid, chapter_id) if chapter_id is not None else None
        if c:
            ix = c["chapter_index"]
            ttl = c["title"]
        self._prog.set(bid, chapter_id=chapter_id, position=position,
                          chapter_index=ix, chapter_title=ttl)
