"""
MediaHub —— 个人影音收藏/追更中心
后端：FastAPI + SQLite
对接：MoviePilot (订阅下载) / Emby (观看状态)
"""
import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import httpx
import logging
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from books_local import LocalBooks, ProgressStore, lan_ip

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
CONFIG_FILE = DATA_DIR / "config.json"
DB_FILE = DATA_DIR / "mediahub.db"
WEB_DIR = BASE_DIR.parent / "web"

DEFAULT_CONFIG = {
    "mp_url": "",            # 如 http://192.168.1.10:3000
    "mp_api_version": "auto",  # auto / v3 / v2 / v1
    "mp_api_version_detected": "",
    "mp_is_v3": False,
    "mp_username": "",
    "mp_password": "",
    "mp_token": "",          # 登录后缓存的 token
    "media_servers": [],     # [{id, type: emby/jellyfin/plex, name, url, api_key, user_id}]
    "pan_115_cookie": "",    # 115 网盘 cookie（webapi 搜索用）
    "year_goal": 0,          # 年度观影目标（0 = 未设置）
    "site_title": "MediaHub",
    # ---- 自动同步（后台定时把媒体服务器已看记录写入本地库）----
    "auto_sync": True,
    "sync_interval_min": 60,   # 同步间隔（分钟）
    "sync_last_at": 0,         # 上次同步完成时间戳
    "sync_last_result": "",    # 上次同步结果摘要
    "hook_key": "",            # Webhook 校验密钥（自动生成）
    "hook_last_at": 0,         # 最近一次收到 Webhook 的时间
    "hook_last_event": "",     # 最近一次事件摘要（便于排查）
    # ---- 本站访问密码（默认关闭，开启后未登录不能调任何 /api）----
    "auth_enabled": False,
    "auth_pass": "",         # pbkdf2_sha256$轮数$盐$哈希，不存明文
    "auth_secret": "",       # cookie 签名密钥，首次登录时生成
    "auth_days": 30,         # 免登录天数（记住我）
    # ---- 语音下载 / 小爱音箱播报 ----
    "xiaomi_user": "",       # 小米账号（用于小爱 TTS 播报）
    "xiaomi_pass": "",
    "xiaomi_did": "",        # 音箱设备 ID（空 = 自动取第一个）
    "leelaa_url": "",        # Leelaa Reader 服务地址（有声书，如 http://192.168.1.10:8686）
    "leelaa_user": "",       # Leelaa 账号
    "leelaa_pass": "",       # Leelaa 密码
    # ---- 有声书（本地目录模式，默认；不再依赖 Leelaa）----
    "books_mode": "local",   # local = 直接扫目录 / leelaa = 走 Leelaa 接口
    "books_dir": "/audiobooks",   # 容器内的有声书根目录（compose 里挂进来）
    "books_base_url": "",    # 给小爱音箱播直链用的地址，空 = 自动取 http://<局域网IP>:8899
    "tts_enabled": False,    # 开启后：下载开始/完成播报
}

# ---------------------------------------------------------------- 配置
def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        try:
            cfg.update(json.loads(CONFIG_FILE.read_text("utf-8")))
        except Exception:
            pass
    # 旧版单 Emby 配置 → 自动迁移为 media_servers 列表
    if not cfg.get("media_servers") and cfg.get("emby_url"):
        cfg["media_servers"] = [{
            "id": "emby1", "type": "emby", "name": "Emby",
            "url": cfg.get("emby_url", ""), "api_key": cfg.get("emby_api_key", ""),
            "user_id": cfg.get("emby_user_id", ""),
        }]
        CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")
    if not isinstance(cfg.get("media_servers"), list):
        cfg["media_servers"] = []
    return cfg


def save_config(new_cfg: dict) -> dict:
    cfg = load_config()
    for k, v in new_cfg.items():
        if k in DEFAULT_CONFIG and v is not None:
            cfg[k] = v
    # 敏感字段不回传给前端明文之外的部分照常存
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")
    return mask_config(cfg)


def mask_config(cfg: dict) -> dict:
    out = json.loads(json.dumps(cfg, ensure_ascii=False))  # deep copy
    for k in ("mp_password", "mp_token", "pan_115_cookie", "auth_pass", "auth_secret",
              "xiaomi_pass", "leelaa_pass"):
        if out.get(k):
            out[k] = "******"
    for s in out.get("media_servers") or []:
        if s.get("api_key"):
            s["api_key"] = "******"
        if s.get("password"):
            s["password"] = "******"
    return out

# ---------------------------------------------------------------- 本站登录
AUTH_COOKIE = "mh_token"
_PW_ITERS = 200_000
_fail_log: dict = {}          # ip -> [失败时间戳列表]，防暴力破解


def hash_pw(pw: str, salt: str = "") -> str:
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), _PW_ITERS)
    return f"pbkdf2_sha256${_PW_ITERS}${salt}${dk.hex()}"


def verify_pw(pw: str, stored: str) -> bool:
    try:
        _algo, iters, salt, hexd = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), int(iters))
        return hmac.compare_digest(dk.hex(), hexd)
    except Exception:
        return False


def auth_secret() -> str:
    cfg = load_config()
    s = cfg.get("auth_secret") or ""
    if not s:
        s = secrets.token_hex(32)
        save_config({"auth_secret": s})
    return s


def make_token(days: int = 30) -> str:
    exp = int(time.time()) + max(1, days) * 86400
    raw = f"{exp}|{_pw_fp()}"
    sig = hmac.new(auth_secret().encode(), raw.encode(), hashlib.sha256).hexdigest()[:40]
    return base64.urlsafe_b64encode(f"{raw}|{sig}".encode()).decode()


def check_token(tok: str) -> bool:
    """校验签名 + 过期；改过密码后旧 cookie 自动失效"""
    if not tok:
        return False
    try:
        raw = base64.urlsafe_b64decode(tok.encode()).decode()
        exp, fp, sig = raw.rsplit("|", 2)
        if int(exp) < time.time():
            return False
        if not hmac.compare_digest(fp, _pw_fp()):
            return False
        want = hmac.new(auth_secret().encode(), f"{exp}|{fp}".encode(),
                        hashlib.sha256).hexdigest()[:40]
        return hmac.compare_digest(sig, want)
    except Exception:
        return False


def auth_on() -> bool:
    cfg = load_config()
    return bool(cfg.get("auth_enabled") and cfg.get("auth_pass"))


def _pw_fp() -> str:
    """密码指纹：改密码后旧登录态自动失效"""
    return hashlib.sha256((load_config().get("auth_pass") or "").encode()).hexdigest()[:16]


def too_many_fails(ip: str) -> int:
    """返回还需等待的秒数（0 = 可以试）"""
    now = time.time()
    log = [t for t in _fail_log.get(ip, []) if now - t < 300]
    _fail_log[ip] = log
    if len(log) >= 8:
        return int(300 - (now - log[0])) + 1
    return 0


def note_fail(ip: str, ok: bool):
    if ok:
        _fail_log.pop(ip, None)
    else:
        _fail_log.setdefault(ip, []).append(time.time())


# ---------------------------------------------------------------- 数据库
def ukey_of(source: str, media_type: str, title: str, year: str,
            tmdb_id: Optional[int]) -> str:
    """去重键：来源|类型|标题|年份|tmdb"""
    return f"{source}|{media_type}|{title}|{year or ''}|{tmdb_id or 0}"


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE IF NOT EXISTS records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ukey TEXT NOT NULL UNIQUE,
            source TEXT NOT NULL,           -- mp / local / emby
            media_type TEXT NOT NULL,       -- 电影/电视剧/短剧/纪录片/图书/音乐
            title TEXT NOT NULL,
            year TEXT DEFAULT '',
            overview TEXT DEFAULT '',
            poster TEXT DEFAULT '',
            tmdb_id INTEGER,
            douban_id INTEGER,
            season INTEGER,
            status TEXT NOT NULL DEFAULT 'want',   -- want / watching / done
            rating INTEGER,                 -- 1-10
            comment TEXT DEFAULT '',
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            finished_at INTEGER             -- 完成时间（用于统计/热力图）
        )"""
    )
    # 旧库迁移：补 finished_at / tags / runtime 列
    cols = [r[1] for r in conn.execute("PRAGMA table_info(records)").fetchall()]
    if "finished_at" not in cols:
        conn.execute("ALTER TABLE records ADD COLUMN finished_at INTEGER")
    if "tags" not in cols:
        conn.execute("ALTER TABLE records ADD COLUMN tags TEXT DEFAULT ''")
    if "runtime" not in cols:
        conn.execute("ALTER TABLE records ADD COLUMN runtime INTEGER")  # 片长（分钟）
    if "rewatch" not in cols:
        conn.execute("ALTER TABLE records ADD COLUMN rewatch INTEGER DEFAULT 0")   # 重看次数
    if "last_done_at" not in cols:
        conn.execute("ALTER TABLE records ADD COLUMN last_done_at INTEGER")        # 最近一次看完
    # 片单
    conn.execute("""CREATE TABLE IF NOT EXISTS lists (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        created_at INTEGER NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS list_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        list_id INTEGER NOT NULL,
        ukey TEXT NOT NULL,
        added_at INTEGER NOT NULL,
        UNIQUE(list_id, ukey))""")
    # 旧库迁移：list_items 快照列（条目无记录时也能展示）
    li_cols = [r[1] for r in conn.execute("PRAGMA table_info(list_items)").fetchall()]
    for col, ddl in (("title", "TEXT DEFAULT ''"), ("media_type", "TEXT DEFAULT ''"),
                     ("year", "TEXT DEFAULT ''"), ("poster", "TEXT DEFAULT ''")):
        if col not in li_cols:
            conn.execute(f"ALTER TABLE list_items ADD COLUMN {col} {ddl}")
    # 媒体元信息缓存（豆瓣评分/主创/片长），mkey = 类型|标题|年份
    conn.execute("""CREATE TABLE IF NOT EXISTS media_meta (
        mkey TEXT PRIMARY KEY,
        douban_id INTEGER,
        douban_rating REAL,
        votes INTEGER,
        directors TEXT DEFAULT '',
        actors TEXT DEFAULT '',
        genres TEXT DEFAULT '',
        runtime INTEGER,
        douban_summary TEXT DEFAULT '',
        updated_at INTEGER NOT NULL)""")
    conn.commit()      # 建表 / 加列必须提交，否则连接关闭时回滚
    return conn


def meta_key(media_type: str, title: str, year: str) -> str:
    return f"{media_type}|{title}|{year or ''}"


def row_to_dict(r) -> dict:
    return {k: r[k] for k in r.keys()}

# ---------------------------------------------------------------- MoviePilot 客户端
class MoviePilotClient:
    """兼容 MoviePilot v3 / v2 / v1：
    - v3: /api/v3 或 /api/v1（V3 文档站显示为 v1 路径），响应统一 {success,message,data}，
          订阅用 media_source+media_id 身份模型，类型为 movie/tv/music
    - v2: /api/v2，订阅用 tmdbid/doubanid，类型为 电影/电视剧
    - v1: /api/v1（老版本）
    版本默认 auto，登录/探测成功后自动锁定并缓存。
    """

    CANDIDATES = ["v3", "v2", "v1"]

    def __init__(self):
        self._prefix: Optional[str] = None      # 完整前缀，如 http://host/api/v1
        self._resolved_ver: Optional[str] = None
        self._is_v3_product: Optional[bool] = None
        self._token_cache_time = 0

    def _candidates(self) -> list:
        cfg = load_config()
        base = cfg.get("mp_url", "").rstrip("/")
        if not base:
            return []
        ver = (cfg.get("mp_api_version") or "auto").lower()
        if ver in ("v1", "v2", "v3"):
            # 手动指定：优先尝试所选版本，失败仍回退其他版本
            others = [v for v in self.CANDIDATES if v != ver]
            return [f"{base}/api/{ver}"] + [f"{base}/api/{v}" for v in others]
        return [f"{base}/api/{v}" for v in self.CANDIDATES]

    def _save_resolve(self, prefix: str, token: Optional[str], is_v3: Optional[bool] = None):
        cfg = load_config()
        self._prefix = prefix
        self._resolved_ver = prefix.rsplit("/", 1)[-1]
        if is_v3 is not None:
            self._is_v3_product = is_v3
            cfg["mp_is_v3"] = is_v3
        cfg["mp_api_version_detected"] = self._resolved_ver
        if token is not None:
            cfg["mp_token"] = token
            self._token_cache_time = time.time()
        CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")

    @property
    def is_v3(self) -> bool:
        """V3 产品版（注意：V3 使用 /api/v1 路径，需单独探测）"""
        return bool(self._is_v3_product)

    async def _detect_product(self, cand: str, token: str) -> Optional[bool]:
        """探测是否 V3 产品版：/recommend/source 仅 V3 存在。
        返回 True/False；网络异常返回 None（由调用方回退到上次缓存值）"""
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.get(f"{cand}/recommend/source",
                                headers={"Authorization": f"Bearer {token}"})
            return r.status_code == 200
        except Exception:  # noqa
            return None

    async def _login_and_detect(self, force: bool = False) -> str:
        cfg = load_config()
        if not cfg.get("mp_url"):
            raise HTTPException(400, "尚未配置 MoviePilot 地址，请先到设置页填写")
        # 已缓存 token：直接探测正确版本
        if cfg.get("mp_token") and not force:
            for cand in self._candidates():
                try:
                    async with httpx.AsyncClient(timeout=10) as c:
                        resp = await c.get(f"{cand}/user/current",
                                           headers={"Authorization": f"Bearer {cfg['mp_token']}"})
                    if resp.status_code == 200:
                        is_v3 = await self._detect_product(cand, cfg["mp_token"])
                        if is_v3 is None:
                            is_v3 = bool(cfg.get("mp_is_v3", False))
                        self._save_resolve(cand, None, is_v3)
                        return cfg["mp_token"]
                except Exception:  # noqa
                    continue
        # 账密登录（同时探测版本）
        if cfg.get("mp_username") and cfg.get("mp_password"):
            errors = []
            for cand in self._candidates():
                try:
                    async with httpx.AsyncClient(timeout=15) as c:
                        resp = await c.post(
                            f"{cand}/login/access-token",
                            data={"username": cfg["mp_username"],
                                  "password": cfg["mp_password"]},
                        )
                    if resp.status_code == 200:
                        data = resp.json()
                        token = data.get("access_token") or ""
                        if token:
                            is_v3 = await self._detect_product(cand, token)
                            if is_v3 is None:
                                is_v3 = bool(cfg.get("mp_is_v3", False))
                            self._save_resolve(cand, token, is_v3)
                            return token
                except Exception as e:  # noqa
                    errors.append(str(e))
            raise HTTPException(502, "MoviePilot 登录失败(v3/v2/v1 均未成功)"
                                     + (f": {errors[0][:200]}" if errors else ""))
        if cfg.get("mp_token"):
            raise HTTPException(502, "Token 探测版本失败，请确认 MP 地址/Token 是否正确")
        raise HTTPException(400, "请在设置页填写 MoviePilot 用户名/密码或 API Token")

    async def _token(self, force: bool = False) -> str:
        cfg = load_config()
        if cfg.get("mp_token") and not force and \
                time.time() - self._token_cache_time < 3600 * 20 and self._prefix:
            return cfg["mp_token"]
        return await self._login_and_detect(force=force)

    @staticmethod
    def _unwrap(data: Any) -> Any:
        """v3 统一响应 {success, message, data} 解包"""
        if isinstance(data, dict) and "success" in data and "data" in data \
                and set(data.keys()) <= {"success", "message", "data"}:
            if data.get("success") is False:
                raise HTTPException(502, f"MoviePilot: {data.get('message') or '返回失败'}")
            return data.get("data")
        return data

    async def request(self, method: str, path: str, json_body: Any = None,
                      params: dict = None, retry: bool = True) -> Any:
        token = await self._token()
        headers = {"Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(timeout=30) as c:
            resp = await c.request(method, f"{self._prefix}{path}",
                                   json=json_body, params=params, headers=headers)
            if resp.status_code == 401 and retry:
                token = await self._login_and_detect(force=True)
                headers = {"Authorization": f"Bearer {token}"}
                resp = await c.request(method, f"{self._prefix}{path}",
                                       json=json_body, params=params, headers=headers)
            if resp.status_code >= 400:
                raise HTTPException(502, f"MoviePilot 接口错误 {resp.status_code}: {resp.text[:300]}")
            if resp.status_code == 204 or not resp.text:
                return None
            return self._unwrap(resp.json())

    # ---------------- 媒体映射 ----------------
    @staticmethod
    def _map_media(m: dict, fallback_type: str = "") -> dict:
        return {
            "source": "mp",
            "media_type": str(m.get("type") or fallback_type) or "电影",
            "title": m.get("title") or m.get("name") or "",
            "year": str(m.get("year") or ""),
            "overview": m.get("overview") or m.get("description") or "",
            "poster": m.get("poster_path") or m.get("poster") or "",
            "tmdb_id": m.get("tmdb_id") or m.get("tmdbid"),
            "douban_id": m.get("douban_id") or m.get("doubanid"),
            "media_source": m.get("media_source") or "",
            "media_id": str(m.get("media_id") or ""),
        }

    async def _try_paths(self, paths: list, params: dict = None) -> Any:
        """按顺序尝试多个接口路径，第一个成功的返回；404/422 视为版本不匹配继续回退"""
        last_err = None
        for p in paths:
            try:
                return await self.request("GET", p, params=params)
            except HTTPException as e:
                last_err = e
                if any(code in str(e.detail) for code in ("404", "422", "Not Found")):
                    continue
                raise
        if last_err:
            raise last_err
        return []

    async def search(self, title: str, mtype: str, page: int = 1) -> list:
        """通过 MP 搜索媒体（TMDB/豆瓣/MusicBrainz）"""
        data = await self._try_paths(["/media/search", "/media/search/"],
                                     params={"title": title, "page": page, "count": 24})
        results = data if isinstance(data, list) else (data or {}).get("list", [])
        tv_like = ("电视剧", "短剧", "纪录片")
        out = []
        for m in results:
            t = str(m.get("type") or "")
            if mtype and mtype != "全部":
                if mtype in tv_like and t == "电影":
                    continue
                if mtype == "电影" and t not in ("电影", "movie", ""):
                    continue
            out.append(self._map_media(m, mtype if mtype != "全部" else ""))
        return out

    async def popular(self, mtype: str, page: int = 1) -> list:
        """浏览热门：v3 用 /recommend/* 按数据源端点，v2/v1 用旧接口"""
        if self._prefix is None:   # 先完成版本/产品探测，再判断分支
            await self._token()
        if self.is_v3:
            # 主选 + 回退（TMDB 未配置时用豆瓣兜底）
            path_map = {
                "电影": ["/recommend/tmdb_movies", "/recommend/douban_movie_hot"],
                "电视剧": ["/recommend/tmdb_tvs", "/recommend/douban_tv_hot"],
                "短剧": ["/recommend/douban_tv_hot", "/recommend/tmdb_tvs"],
                "纪录片": ["/recommend/douban_tv_hot", "/recommend/tmdb_tvs"],
                "图书": [],   # MoviePilot 不支持图书，仅本地记录
                "音乐": ["/recommend/music_douban", "/recommend/music_weekly"],
            }
            for path in path_map.get(mtype, ["/recommend/tmdb_trending"]):
                try:
                    data = await self.request("GET", path, params={"page": page, "count": 24})
                except HTTPException:
                    continue
                items = data if isinstance(data, list) else (data or {}).get("list", [])
                out = [self._map_media(m, mtype) for m in items]
                if out:
                    return out
            return []
        # v2 / v1 旧接口
        data = await self._try_paths(
            ["/media/trending", "/recommend/tmdb/popular"],
            params={"page": page, "count": 24})
        items = data if isinstance(data, list) else (data or {}).get("list", [])
        tv_like = ("电视剧", "短剧", "纪录片")
        out = []
        for m in items:
            t = str(m.get("type") or "")
            if mtype in tv_like and t == "电影":
                continue
            if mtype == "电影" and t not in ("电影", "movie", ""):
                continue
            out.append(self._map_media(m, mtype))
        return out

    async def subscribe(self, media: dict, season: Optional[int] = None) -> Any:
        """添加 MP 订阅（v3 用 media_source/media_id，v2/v1 用 tmdbid/doubanid）"""
        if self._prefix is None:   # 先完成版本/产品探测，再选择订阅格式
            await self._token()
        mt = media.get("media_type", "")
        if self.is_v3:
            type_map = {"电影": "movie", "电视剧": "tv", "短剧": "tv",
                        "纪录片": "tv", "音乐": "music"}
            body = {
                "name": media.get("title"),
                "type": type_map.get(mt, "movie"),
                "year": str(media.get("year") or ""),
            }
            # 身份模型：tmdb 优先，其次 douban
            if media.get("tmdb_id"):
                body["media_source"], body["media_id"] = "themoviedb", str(media["tmdb_id"])
            elif media.get("douban_id"):
                body["media_source"], body["media_id"] = "douban", str(media["douban_id"])
            elif media.get("media_source") and media.get("media_id"):
                body["media_source"] = media["media_source"]
                body["media_id"] = media["media_id"]
            if season and mt != "音乐":
                body["season"] = season
        else:
            if mt == "音乐":
                raise HTTPException(400, "音乐订阅需要 MoviePilot V3")
            mtype = "电视剧" if mt in ("电视剧", "短剧", "纪录片") else "电影"
            body = {"name": media.get("title"), "type": mtype,
                    "year": str(media.get("year") or "")}
            if media.get("tmdb_id"):
                body["tmdbid"] = int(media["tmdb_id"])
            if media.get("douban_id"):
                body["doubanid"] = int(media["douban_id"])
            if season:
                body["season"] = season
        return await self.request("POST", "/subscribe/", json_body=body)

    async def list_subscriptions(self) -> list:
        data = await self.request("GET", "/subscribe/")
        return data if isinstance(data, list) else []

    async def download_list(self) -> list:
        """当前下载任务列表（含 progress/state/hash，注意路径必须带尾斜杠）"""
        data = await self.request("GET", "/download/")
        return data if isinstance(data, list) else []

    async def unsubscribe(self, sub_id) -> Any:
        """删除 MP 订阅"""
        return await self.request("DELETE", f"/subscribe/{sub_id}")


mp = MoviePilotClient()

# ---------------------------------------------------------------- 媒体服务器客户端（Emby / Jellyfin / Plex）
def _save_servers(servers: list):
    cfg = load_config()
    cfg["media_servers"] = servers
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")


def _plex_tmdb(guids: list) -> Optional[int]:
    for g in guids or []:
        gid = g.get("id") or ""
        if gid.startswith("tmdb://"):
            try:
                return int(gid.split("//", 1)[1])
            except ValueError:
                return None
    return None


DEFAULT_PORTS = {"emby": "8096", "jellyfin": "8096", "plex": "32400"}


def norm_url(url: str, port: str = "", server_type: str = "emby") -> str:
    """把「地址 + 端口」拼成规范 base URL。

    - 没写协议默认 http://
    - port 显式给了就覆盖地址里原有的端口
    - 都没给端口时：IP / 内网主机补默认端口（Emby 8096 / Jellyfin 8096 / Plex 32400），
      域名（多半是 https 反代）保持无端口，避免多打一个 8096 打不开
    - 保留子路径（如 http://host:8096/emby）
    """
    u = (url or "").strip()
    if not u:
        return ""
    if not re.match(r"^https?://", u, re.I):
        u = "http://" + u
    m = re.match(r"^(https?)://(\[[^\]]+\]|[^/:]+)(?::(\d+))?(/.*)?$", u, re.I)
    if not m:
        return u.rstrip("/")
    scheme, host = m.group(1).lower(), m.group(2)
    cur_port, path = m.group(3), (m.group(4) or "")
    p = str(port or "").strip()
    final_port = (p if p.isdigit() else "") or (cur_port or "")
    if not final_port:
        is_ip = bool(re.match(r"^\d{1,3}(\.\d{1,3}){3}$", host)) or host.startswith("[")
        if is_ip:
            final_port = DEFAULT_PORTS.get((server_type or "").lower(), "")
    return f"{scheme}://{host}{':' + final_port if final_port else ''}{path}".rstrip("/")


# 影视作品的元数据来源；只有 bilibili 之类的说明是搬运短视频，不进「继续观看/最近入库」
FILM_PROVIDERS = {"tmdb", "imdb", "tvdb", "tvrage", "tmdbcollection", "douban"}


def _looks_like_film(it: dict) -> bool:
    """判断条目是不是正经影视：有影视类 ProviderId → 是；只有 bilibili 等 → 不是"""
    pids = {str(k).lower() for k in (it.get("ProviderIds") or {})}
    if pids & FILM_PROVIDERS:
        return True
    if pids:            # 有 ID 但全是短视频平台的
        return False
    return True         # 无任何 ID：可能自制/冷门片，默认保留


class MediaServer:
    """单个媒体服务器驱动：emby / jellyfin（同一套 API），plex（独立 API）"""

    def __init__(self, srv: dict):
        self.srv = dict(srv)
        self.id = srv.get("id") or f"srv{int(time.time())}"
        self.type = (srv.get("type") or "emby").lower()
        self.name = srv.get("name") or self.type.capitalize()
        self.port = str(srv.get("port") or "").strip()
        self.base = norm_url(srv.get("url") or "", self.port, self.type)
        self.key = srv.get("api_key") or ""
        self.user = srv.get("username") or ""     # 别人的服只给账号密码时用
        self.pwd = srv.get("password") or ""
        self._uid_cache = srv.get("user_id") or ""

    @staticmethod
    def _client(timeout: float = 20, **kw) -> httpx.AsyncClient:
        """媒体服务器请求不走系统代理：内网 IP / 直连域名走代理反而会 502"""
        return httpx.AsyncClient(timeout=timeout, trust_env=False, **kw)

    async def auth(self, force: bool = False) -> str:
        """可用凭证：有 API Key 直接用；只填了账号密码则登录换 AccessToken（缓存到配置）"""
        if self.key and not force:
            return self.key
        if not self.user:
            raise HTTPException(400, f"{self.name} 未配置 API Key 或账号密码")
        hdr = ('MediaBrowser Client="MediaHub", Device="MediaHub", '
               'DeviceId="mediahub", Version="1.0.0"')
        path = ("/emby/Users/AuthenticateByName" if self.type == "emby"
                else "/Users/AuthenticateByName")
        async with self._client(timeout=15) as c:
            try:
                r = await c.post(f"{self.base}{path}",
                                 headers={"X-Emby-Authorization": hdr,
                                          "Content-Type": "application/json"},
                                 json={"Username": self.user, "Pw": self.pwd})
            except httpx.ConnectError:
                raise HTTPException(502, self._conn_hint())
            except httpx.TimeoutException:
                raise HTTPException(504, f"{self.name} 连接超时：{self.base}")
        if r.status_code == 401:
            raise HTTPException(401, f"{self.name} 账号或密码错误")
        if r.status_code == 403:
            raise HTTPException(403, f"{self.name} 登录被拒绝（403）：账号可能被限制，或反代不允许该路径")
        if r.status_code != 200:
            raise HTTPException(502, f"{self.name} 登录失败: {r.status_code}")
        d = r.json()
        tok = d.get("AccessToken") or ""
        if not tok:
            raise HTTPException(502, f"{self.name} 登录未返回 token")
        uid = ((d.get("User") or {}).get("Id")
               or (d.get("SessionInfo") or {}).get("UserId") or "")
        sid = d.get("ServerId") or ""
        self.key = tok
        if uid:
            self._uid_cache = uid
        self.srv["api_key"] = tok
        self.srv["token_type"] = "user"
        servers = load_config().get("media_servers") or []
        for s in servers:
            if s.get("id") == self.id:
                s["api_key"] = tok
                s["token_type"] = "user"
                if uid:
                    s["user_id"] = uid
                if sid:
                    s["server_id"] = sid
        _save_servers(servers)
        return tok

    def _conn_hint(self) -> str:
        return (f"{self.name} 连不上 {self.base}：请检查地址/端口是否正确、"
                f"服务器是否在线、要不要用 https")

    async def _jelly_get(self, url: str, params: Optional[dict] = None,
                         timeout: float = 30):
        """带自动续期的 GET：401 且是账号密码登录时重新登录一次再试"""
        tok = await self.auth()
        async with self._client(timeout=timeout) as c:
            try:
                r = await c.get(url, headers={"X-Emby-Token": tok}, params=params)
                if r.status_code == 401 and self.user:
                    tok = await self.auth(force=True)
                    r = await c.get(url, headers={"X-Emby-Token": tok}, params=params)
            except httpx.ConnectError:
                raise HTTPException(502, self._conn_hint())
            except httpx.TimeoutException:
                raise HTTPException(504, f"{self.name} 连接超时：{self.base}")
            return r

    # ---------------- Emby / Jellyfin ----------------
    def _persist_uid(self, uid: str):
        self._uid_cache = uid
        servers = load_config().get("media_servers") or []
        for s in servers:
            if s.get("id") == self.id:
                s["user_id"] = uid
        _save_servers(servers)

    async def _jelly_uid(self) -> str:
        """用户 ID：优先配置/登录结果；账号密码模式依次试 /Users/Me、/Sessions；
        API Key 模式列 /Users 取管理员。普通账号列用户会 403，需引导填 ID。"""
        if self._uid_cache:
            return self._uid_cache
        tried = []

        def _save(uid: str) -> str:
            self._persist_uid(uid)
            return uid

        # 1) 账号密码登录的普通用户：普通账号没有列用户的权限，只能问"我是谁"
        if self.user:
            me_paths = ("/Users/Me", "/emby/Users/Me")
            for p in me_paths:
                try:
                    r = await self._jelly_get(f"{self.base}{p}", timeout=15)
                except HTTPException as e:
                    tried.append(f"{p}={e.status_code}")
                    continue
                tried.append(f"{p}={r.status_code}")
                if r.status_code == 200:
                    d = r.json() if r.content else {}
                    uid = (d.get("Id") or "") if isinstance(d, dict) else ""
                    if uid:
                        return _save(uid)
            # 2) /Sessions 兜底：普通账号能读自己的会话，里面有 UserId
            for p in ("/Sessions", "/emby/Sessions"):
                try:
                    r = await self._jelly_get(f"{self.base}{p}", timeout=15)
                except HTTPException as e:
                    tried.append(f"{p}={e.status_code}")
                    continue
                tried.append(f"{p}={r.status_code}")
                if r.status_code != 200:
                    continue
                try:
                    sess = r.json()
                except Exception:  # noqa
                    continue
                if not isinstance(sess, list):
                    continue
                mine = [x for x in sess if (x.get("UserName") or "").lower() == self.user.lower()]
                for x in (mine or sess):
                    uid = x.get("UserId") or (x.get("User") or {}).get("Id") or ""
                    if uid:
                        return _save(uid)
        # 3) API Key（管理员）模式：列用户挑管理员
        path = "/emby/Users" if self.type == "emby" else "/Users"
        resp = await self._jelly_get(f"{self.base}{path}", timeout=15)
        if resp.status_code in (401, 403):
            detail = ("该账号无权读取用户列表，请在「用户 ID」里填上你的 UserId"
                      "（Emby 网页端 → 右上角头像 → 个人资料 → 网址里的 userId）"
                      if self.user else "API Key 无效或无权限")
            tail = f"（自动识别失败：{', '.join(tried)}）" if tried else ""
            raise HTTPException(403, f"{self.name} {detail}{tail}")
        if resp.status_code != 200:
            raise HTTPException(502, f"{self.name} 获取用户列表失败: {resp.status_code}")
        users = resp.json()
        if not users:
            raise HTTPException(502, f"{self.name} 没有用户，请检查 API Key")
        admin = next((u for u in users if u.get("Policy", {}).get("IsAdministrator")), users[0])
        uid = admin.get("Id", "")
        return _save(uid)

    async def _jelly_search(self, title: str) -> list:
        uid = await self._jelly_uid()
        r = await self._jelly_get(f"{self.base}/Users/{uid}/Items",
                                  params={"SearchTerm": title, "Recursive": "true",
                                          "IncludeItemTypes": "Movie,Series", "Limit": 8,
                                          "Fields": "ProductionYear,ProviderIds"},
                                  timeout=15)
        if r.status_code != 200:
            raise HTTPException(502, f"{self.name} 搜索失败: {r.status_code}")
        items = r.json().get("Items", [])
        return [{
            "media_type": "电影" if it.get("Type") == "Movie" else "电视剧",
            "title": it.get("Name", ""),
            "year": str(it.get("ProductionYear") or ""),
            "poster": f"/api/ms/img/{self.id}/{it.get('Id')}" if (it.get("ImageTags") or {}).get("Primary") else "",
            "tmdb_id": (it.get("ProviderIds") or {}).get("Tmdb"),
            "item_id": it.get("Id"),
        } for it in items]

    async def _jelly_watched(self) -> list:
        uid = await self._jelly_uid()
        out = []
        for itype, mt in (("Movie", "电影"), ("Series", "电视剧")):
            r = await self._jelly_get(f"{self.base}/Users/{uid}/Items",
                                      params={"Filters": "IsPlayed", "Recursive": "true",
                                              "IncludeItemTypes": itype,
                                              "Fields": "Overview,ProductionYear,ProviderIds,DatePlayed,RunTimeTicks",
                                              "Limit": 500},
                                      timeout=60)
            r.raise_for_status()
            for it in r.json().get("Items", []):
                rt = it.get("RunTimeTicks") or 0
                out.append({
                    "media_type": mt, "title": it.get("Name", ""),
                    "year": str(it.get("ProductionYear") or ""),
                    "overview": it.get("Overview") or "",
                    "poster": f"/api/ms/img/{self.id}/{it.get('Id')}" if (it.get("ImageTags") or {}).get("Primary") else "",
                    "tmdb_id": (it.get("ProviderIds") or {}).get("Tmdb"),
                    "date_played": it.get("DatePlayed") or "",
                    "runtime": int(rt / 600_000_000) if rt else None,  # ticks → 分钟
                })
        return out

    async def _jelly_resume(self, limit: int = 12) -> list:
        """继续观看：媒体服务器记住的播放位置（电影 / 剧集单集）"""
        uid = await self._jelly_uid()
        r = await self._jelly_get(
            f"{self.base}/Users/{uid}/Items/Resume",
            params={"Limit": max(limit * 3, 24), "MediaTypes": "Video",
                    "Fields": "SeriesName,SeriesId,ProductionYear,RunTimeTicks,UserData,"
                              "ParentIndexNumber,IndexNumber,ImageTags,ProviderIds"},
            timeout=20)
        if r.status_code != 200:
            return []
        data = r.json()
        items = data.get("Items") if isinstance(data, dict) else (data or [])
        out = []
        for it in items or []:
            ud = it.get("UserData") or {}
            total_ticks = it.get("RunTimeTicks") or 0
            pos_ticks = ud.get("PlaybackPositionTicks") or 0
            pct = float(ud.get("PlayedPercentage") or 0)
            if not pct and total_ticks:
                pct = pos_ticks * 100.0 / total_ticks
            pct = int(round(pct))
            if pct >= 100:      # 已看完的不放在「继续」里
                continue
            # 跳过短视频片段（搬运/花絮类）：优先看元数据来源，再兜底看时长
            if not _looks_like_film(it):
                continue
            if total_ticks and total_ticks < 8 * 600_000_000:
                continue
            is_ep = it.get("Type") == "Episode"
            sid = it.get("SeriesId") or ""
            se = int(it.get("ParentIndexNumber") or 0)
            ep = int(it.get("IndexNumber") or 0)
            sub = ""
            if is_ep and se and ep:
                sub = f"S{se:02d}E{ep:02d}"
                if it.get("Name"):
                    sub += f" · {it['Name']}"
            img_id = (it.get("Id") if (it.get("ImageTags") or {}).get("Primary")
                      else (sid or it.get("Id")))
            out.append({
                "srv_id": self.id, "srv_name": self.name,
                "item_id": it.get("Id"), "series_id": sid,
                "title": (it.get("SeriesName") if is_ep else it.get("Name")) or "",
                "sub": sub,
                "media_type": "电视剧" if is_ep else "电影",
                "year": str(it.get("ProductionYear") or ""),
                "pct": max(1, min(99, pct or 1)),
                "pos_min": round(pos_ticks / 600_000_000) if pos_ticks else 0,
                "total_min": round(total_ticks / 600_000_000) if total_ticks else 0,
                "poster": f"/api/ms/img/{self.id}/{img_id}" if img_id else "",
                "last_played": ud.get("LastPlayedDate") or "",
            })
        return out

    async def _jelly_latest(self, limit: int = 12) -> list:
        """最近入库：媒体库新增的电影 / 剧集"""
        uid = await self._jelly_uid()
        r = await self._jelly_get(
            f"{self.base}/Users/{uid}/Items/Latest",
            params={"Limit": max(limit * 3, 24), "IncludeItemTypes": "Movie,Series",
                    "Fields": "ProductionYear,DateCreated,RunTimeTicks,ImageTags,ProviderIds"},
            timeout=20)
        if r.status_code != 200:
            return []
        data = r.json()
        items = data.get("Items") if isinstance(data, dict) else (data or [])
        out = []
        for it in items or []:
            rt = it.get("RunTimeTicks") or 0
            if not _looks_like_film(it) or (rt and rt < 8 * 600_000_000):
                continue
            img_id = it.get("Id")
            out.append({
                "srv_id": self.id, "srv_name": self.name,
                "item_id": it.get("Id"),
                "title": it.get("Name") or "",
                "media_type": "电影" if it.get("Type") == "Movie" else "电视剧",
                "year": str(it.get("ProductionYear") or ""),
                "created": it.get("DateCreated") or "",
                "poster": f"/api/ms/img/{self.id}/{img_id}" if img_id else "",
            })
        return out

    async def _jelly_sizes(self, limit: int = 8000) -> list:
        """库内条目体积（电影按片、剧集按集），用于存储体检"""
        uid = await self._jelly_uid()
        r = await self._jelly_get(
            f"{self.base}/Users/{uid}/Items",
            params={"Recursive": "true", "IncludeItemTypes": "Movie,Episode",
                    "Fields": "Size,SeriesName,ProductionYear,RunTimeTicks,UserData,ProviderIds",
                    "SortBy": "Size", "SortOrder": "Descending", "Limit": limit},
            timeout=90)
        if r.status_code != 200:
            return []
        data = r.json()
        return data.get("Items") or []

    async def _jelly_test(self) -> dict:
        uid = await self._jelly_uid()
        r = await self._jelly_get(f"{self.base}/Users/{uid}", timeout=12)
        if r.status_code != 200:
            raise HTTPException(502, f"{self.name} 读取用户信息失败: {r.status_code}")
        u = r.json()
        return {"ok": True, "server": self.name, "user": u.get("Name") or uid, "uid": uid}

    # ---------------- Plex ----------------
    def _plex_headers(self) -> dict:
        return {"X-Plex-Token": self.key, "Accept": "application/json"}

    async def _plex_search(self, title: str) -> list:
        async with self._client(timeout=15) as c:
            r = await c.get(f"{self.base}/search", headers=self._plex_headers(),
                            params={"query": title})
            if r.status_code != 200:
                raise HTTPException(502, f"{self.name} 搜索失败: {r.status_code}")
            meta = r.json().get("MediaContainer", {}).get("Metadata", []) or []
        return [{
            "media_type": "电影" if it.get("type") == "movie" else "电视剧",
            "title": it.get("title", ""),
            "year": str(it.get("year") or ""),
            "poster": f"/api/ms/img/{self.id}/{it.get('ratingKey')}" if it.get("thumb") else "",
            "tmdb_id": _plex_tmdb(it.get("Guid") or []),
            "item_id": it.get("ratingKey"),
        } for it in meta if it.get("type") in ("movie", "show")][:8]

    async def _plex_watched(self) -> list:
        async with self._client(timeout=60) as c:
            r = await c.get(f"{self.base}/library/sections", headers=self._plex_headers())
            r.raise_for_status()
            sections = [s for s in (r.json().get("MediaContainer", {}).get("Directory") or [])
                        if s.get("type") in ("movie", "show")]
            out = []
            for sec in sections:
                rr = await c.get(f"{self.base}/library/sections/{sec['key']}/all",
                                 headers=self._plex_headers(),
                                 params={"viewCount": ">=1"})
                if rr.status_code != 200:
                    continue
                for it in (rr.json().get("MediaContainer", {}).get("Metadata") or []):
                    out.append({
                        "media_type": "电影" if sec.get("type") == "movie" else "电视剧",
                        "title": it.get("title", ""),
                        "year": str(it.get("year") or ""),
                        "overview": it.get("summary") or "",
                        "poster": f"/api/ms/img/{self.id}/{it.get('ratingKey')}" if it.get("thumb") else "",
                        "tmdb_id": _plex_tmdb(it.get("Guid") or []),
                        "date_played": (it.get("lastViewedAt") and
                                        datetime.fromtimestamp(it["lastViewedAt"]).isoformat()) or "",
                        "runtime": int(it["Duration"] / 60_000) if it.get("Duration") else None,
                    })
        return out

    async def _plex_test(self) -> dict:
        async with self._client(timeout=12) as c:
            r = await c.get(f"{self.base}/", headers=self._plex_headers())
            r.raise_for_status()
            info = r.json().get("MediaContainer", {})
        return {"ok": True, "server": self.name,
                "user": info.get("friendlyName") or self.name, "uid": "-"}

    # ---------------- 播放直达 ----------------
    async def server_id(self) -> str:
        """获取服务器 GUID（用于网页播放深链），并缓存到配置"""
        if self.srv.get("server_id"):
            return self.srv["server_id"]
        async with self._client(timeout=12) as c:
            if self.type == "plex":
                r = await c.get(f"{self.base}/", headers=self._plex_headers())
                r.raise_for_status()
                sid = (r.json().get("MediaContainer") or {}).get("machineIdentifier") or ""
            else:
                path = "/emby/System/Info" if self.type == "emby" else "/System/Info"
                r = await self._jelly_get(f"{self.base}{path}", timeout=12)
                r.raise_for_status()
                sid = r.json().get("Id") or ""
        if sid:
            self.srv["server_id"] = sid
            servers = load_config().get("media_servers") or []
            for s in servers:
                if s.get("id") == self.id:
                    s["server_id"] = sid
            _save_servers(servers)
        return sid

    async def play_url(self, item_id: str) -> Optional[str]:
        """网页端播放深链（打开媒体服务器网页并定位到该片）"""
        if not item_id:
            return None
        sid = await self.server_id()
        if not sid:
            return None
        if self.type == "plex":
            from urllib.parse import quote
            return (f"{self.base}/web/index.html#!/server/{sid}/details"
                    f"?key={quote('/library/metadata/' + str(item_id))}")
        if self.type == "jellyfin":
            return f"{self.base}/web/index.html#!/details?id={item_id}&serverId={sid}"
        return f"{self.base}/web/index.html#!/item?id={item_id}&serverId={sid}"

    # ---------------- 统一入口 ----------------
    async def search(self, title: str) -> list:
        if self.type == "plex":
            return await self._plex_search(title)
        return await self._jelly_search(title)

    async def watched(self) -> list:
        if self.type == "plex":
            return await self._plex_watched()
        return await self._jelly_watched()

    async def test(self) -> dict:
        if self.type == "plex":
            return await self._plex_test()
        return await self._jelly_test()


def media_servers() -> list:
    # 有 API Key，或只给了账号密码（朋友服）都算可用
    return [MediaServer(s) for s in (load_config().get("media_servers") or [])
            if s.get("url") and (s.get("api_key") or s.get("username"))]


# ---------------------------------------------------------------- 115 网盘（Cookie 直连）
class Pan115:
    UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) MediaHub/1.0"

    def _headers(self) -> dict:
        cookie = (load_config().get("pan_115_cookie") or "").strip()
        if not cookie:
            raise HTTPException(400, "尚未配置 115 Cookie，请到设置页粘贴")
        return {"Cookie": cookie, "User-Agent": self.UA, "Referer": "https://115.com/"}

    async def search(self, title: str) -> list:
        async with httpx.AsyncClient(timeout=15, headers=self._headers()) as c:
            r = await c.get("https://webapi.115.com/files/search",
                            params={"search_value": title, "limit": 20,
                                    "o": "user_ptime", "asc": "0"})
            data = r.json()
        if data.get("state") is False:
            raise HTTPException(502, f"115 返回错误: {str(data.get('error') or data)[:120]}（Cookie 可能已过期）")
        files = ((data.get("data") or {}).get("list")) or []
        return [{"name": f.get("name", ""), "fid": f.get("fid"), "cid": f.get("cid"),
                 "size": f.get("size"), "ico": f.get("ico") or ""} for f in files]

    async def test(self) -> dict:
        async with httpx.AsyncClient(timeout=15, headers=self._headers()) as c:
            r = await c.get("https://my.115.com/?ct=ajax&ac=walsh_user_info")
            data = r.json()
        if data.get("state"):
            info = data.get("data") or {}
            return {"ok": True, "user": info.get("user_name") or "115 用户"}
        raise HTTPException(502, f"115 Cookie 无效或过期: {str(data)[:120]}")


pan115 = Pan115()

# ---------------------------------------------------------------- FastAPI
app = FastAPI(title="MediaHub")
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])


@app.on_event("startup")
async def _startup_tasks():
    """启动后台定时同步（单进程 uvicorn，不会重复起多个）"""
    try:
        db().close()          # 触发建表 / 字段迁移，避免第一次请求时才跑
    except Exception:  # noqa
        pass
    asyncio.create_task(_auto_sync_loop())
    asyncio.create_task(_media_watch_loop())


async def _media_watch_loop():
    """每分钟看一眼 MP 下载列表：新任务出现 → 播报开始；任务消失 → 播报完成"""
    await asyncio.sleep(40)
    while True:
        try:
            await _watch_downloads_once()
        except Exception:  # noqa
            pass
        await asyncio.sleep(60)


_DL_SEEN: dict = {}          # hash -> {name, season}
_WATCH_INIT = False          # 首轮只记录不播报（避免重启后把存量任务当新任务）


def tts_on() -> bool:
    cfg = load_config()
    return bool(cfg.get("tts_enabled")) and bool(cfg.get("xiaomi_user"))


async def _watch_downloads_once():
    global _WATCH_INIT
    try:
        dl = await mp.download_list()
    except Exception:  # noqa
        return
    current = {}
    for d in dl:
        if not isinstance(d, dict):
            continue
        h = str(d.get("hash") or d.get("title") or "")
        if h:
            current[h] = d
    if not _WATCH_INIT:
        _WATCH_INIT = True
        for h, d in current.items():
            _DL_SEEN[h] = {"name": str(d.get("name") or d.get("title") or "")[:24],
                           "season": str(d.get("season_episode") or "")}
        return
    # 新任务 → 开始下载播报
    for h, d in current.items():
        if h not in _DL_SEEN:
            name = str(d.get("name") or d.get("title") or "")[:24]
            season = str(d.get("season_episode") or "")
            _DL_SEEN[h] = {"name": name, "season": season}
            if name:
                say_text = f"已开始下载{name}" + (f"第{season.lower().replace('s', '')}季" if season else "")
                asyncio.create_task(_say_safe(say_text))
    # 消失的任务 → 完成（或被移除），播报完成
    for h, info in list(_DL_SEEN.items()):
        if h not in current:
            _DL_SEEN.pop(h)
            if info.get("name"):
                asyncio.create_task(_say_safe(f"《{info['name']}》下载完成，可以看了"))


async def _say_safe(text: str):
    try:
        await say(text)
    except Exception:  # noqa
        pass


async def say(text: str) -> bool:
    """小爱音箱 TTS 播报。返回是否成功。支持两种凭据：小米密码，或已种入
    server/data/mi.token 的 passToken（免密码）。"""
    cfg = load_config()
    user = cfg.get("xiaomi_user") or ""
    if not cfg.get("tts_enabled") or not user:
        return False
    try:
        from miservice import MiAccount, MiNAService
        account = MiAccount(None, user, cfg.get("xiaomi_pass") or "",
                            str(DATA_DIR / "mi.token"))
        mina = MiNAService(account)
        did = str(cfg.get("xiaomi_did") or "")
        if not did:
            devices = await mina.device_list()
            lst = []
            if isinstance(devices, dict):
                lst = devices.get("deviceList") or []
            elif isinstance(devices, list):
                lst = devices
            if not lst:
                raise HTTPException(400, "小米账号下未找到小爱音箱设备")
            did = str(lst[0].get("deviceID") or lst[0].get("deviceId") or "")
        ok = await mina.text_to_speech(did, text)
        return bool(ok)
    except HTTPException:
        raise
    except Exception as e:  # noqa
        raise HTTPException(502, f"小爱播报失败：{e}")


# ---------------------------------------------------------------- 登录守卫
@app.middleware("http")
async def auth_guard(request: Request, call_next):
    """开启登录后，除静态资源与 /api/auth/* 外全部要求已登录"""
    path = request.url.path
    if (not path.startswith("/api/")
            or path.startswith("/api/auth/")
            # Webhook 由媒体服务器发起，用 key 校验，不走登录态
            or (path == "/api/hook/emby" and request.method == "POST")
            or (path == "/api/hook/mpdl" and request.method == "POST")
            or request.method == "OPTIONS"):
        return await call_next(request)
    if not auth_on():
        return await call_next(request)
    tok = request.cookies.get(AUTH_COOKIE) or ""
    if not tok:
        ah = request.headers.get("authorization") or ""
        if ah.lower().startswith("bearer "):
            tok = ah[7:].strip()
    if check_token(tok):
        return await call_next(request)
    return JSONResponse({"detail": "登录已过期，请重新登录", "need_login": True},
                        status_code=401)


class LoginModel(BaseModel):
    password: str = ""
    remember: bool = True


class AuthSetupModel(BaseModel):
    password: str = ""
    enabled: bool = True
    days: int = 30


def _set_login_cookie(resp, days: int):
    resp.set_cookie(AUTH_COOKIE, make_token(days), max_age=days * 86400,
                    httponly=True, samesite="lax", path="/")
    return resp


@app.get("/api/auth/status")
def auth_status(request: Request):
    """登录页据此决定是否弹密码框"""
    cfg = load_config()
    on = auth_on()
    tok = request.cookies.get(AUTH_COOKIE) or ""
    return {"enabled": on,
            "logged_in": (not on) or check_token(tok),
            "site_title": cfg.get("site_title") or "MediaHub",
            "days": int(cfg.get("auth_days") or 30)}


@app.post("/api/auth/login")
def auth_login(m: LoginModel, request: Request):
    cfg = load_config()
    if not auth_on():
        return {"ok": True, "enabled": False}
    ip = (request.client.host if request.client else "?") or "?"
    wait = too_many_fails(ip)
    if wait:
        raise HTTPException(429, f"失败次数过多，请 {wait} 秒后再试")
    ok = verify_pw(m.password, cfg.get("auth_pass") or "")
    note_fail(ip, ok)
    if not ok:
        raise HTTPException(401, "密码不对")
    days = int(cfg.get("auth_days") or 30) if m.remember else 1
    return _set_login_cookie(JSONResponse({"ok": True}), days)


@app.post("/api/auth/logout")
def auth_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(AUTH_COOKIE, path="/")
    return resp


@app.post("/api/auth/setup")
def auth_setup(m: AuthSetupModel):
    """开启 / 修改 / 关闭访问密码。
    已开启时必须已登录（中间件已拦）；未开启时允许首次设置。"""
    cfg = load_config()
    payload: dict = {"auth_enabled": bool(m.enabled)}
    if m.enabled:
        if m.password:
            if len(m.password) < 4:
                raise HTTPException(400, "密码至少 4 位")
            payload["auth_pass"] = hash_pw(m.password)
        elif not cfg.get("auth_pass"):
            raise HTTPException(400, "开启密码必须设置一个密码")
        if m.days:
            payload["auth_days"] = max(1, min(365, int(m.days)))
    save_config(payload)
    if not m.enabled:
        resp = JSONResponse({"ok": True, "enabled": False})
        resp.delete_cookie(AUTH_COOKIE, path="/")
        return resp
    days = int(load_config().get("auth_days") or 30)
    return _set_login_cookie(JSONResponse({"ok": True, "enabled": True}), days)


class SettingsModel(BaseModel):
    mp_url: Optional[str] = None
    mp_api_version: Optional[str] = None
    mp_username: Optional[str] = None
    mp_password: Optional[str] = None
    mp_token: Optional[str] = None
    media_servers: Optional[list] = None   # [{id, type, name, url, api_key, user_id}]
    pan_115_cookie: Optional[str] = None
    year_goal: Optional[int] = None
    site_title: Optional[str] = None
    xiaomi_user: Optional[str] = None
    xiaomi_pass: Optional[str] = None
    xiaomi_did: Optional[str] = None
    tts_enabled: Optional[bool] = None
    leelaa_url: Optional[str] = None
    leelaa_user: Optional[str] = None
    leelaa_pass: Optional[str] = None
    books_mode: Optional[str] = None
    books_dir: Optional[str] = None
    books_base_url: Optional[str] = None


class TestServerModel(BaseModel):
    id: Optional[str] = None
    type: str = "emby"
    name: str = ""
    url: str
    port: str = ""
    api_key: str = ""
    username: str = ""
    password: str = ""
    auth_mode: str = "key"
    user_id: Optional[str] = ""


class WantModel(BaseModel):
    source: str = "mp"
    media_type: str
    title: str
    year: str = ""
    overview: str = ""
    poster: str = ""
    tmdb_id: Optional[int] = None
    douban_id: Optional[int] = None
    media_source: str = ""   # v3 身份模型：themoviedb / douban / imdb ...
    media_id: str = ""
    season: Optional[int] = None
    subscribe_mp: bool = True   # 是否同时向 MP 发起订阅


class FinishModel(BaseModel):
    record_id: int
    rating: Optional[int] = None  # 1-10
    comment: str = ""
    tags: str = ""                # 快捷标签，如 "神作,烧脑"
    rewatch: bool = False         # 标记为「重看」：次数 +1，首看时间保留


@app.get("/api/settings")
def get_settings():
    return mask_config(load_config())


@app.post("/api/settings")
def update_settings(body: SettingsModel):
    data = body.model_dump(exclude_none=True)
    # 密码/ token 填 ****** 表示不修改
    for k in ("mp_password", "mp_token", "pan_115_cookie", "xiaomi_pass", "leelaa_pass"):
        if data.get(k) == "******":
            data.pop(k)
    # 媒体服务器列表：api_key / password 为 ****** 表示沿用旧值
    if "media_servers" in data:
        old = {s.get("id"): s for s in (load_config().get("media_servers") or [])}
        merged = []
        for i, s in enumerate(data["media_servers"]):
            if not (s.get("url") or "").strip():
                continue
            s = dict(s)
            s.setdefault("id", f"srv{i + 1}_{int(time.time()) % 100000}")
            s["type"] = (s.get("type") or "emby").lower()
            s["port"] = str(s.get("port") or "").strip()
            s["url"] = norm_url(s.get("url") or "", s["port"], s["type"])
            if not s["url"]:
                continue
            prev = old.get(s["id"], {})
            raw_pwd = s.get("password") or ""
            if s.get("api_key") in ("", "******"):
                s["api_key"] = prev.get("api_key", "")
            if raw_pwd in ("", "******"):
                s["password"] = prev.get("password", "")
            if not (s.get("user_id") or "").strip():
                s["user_id"] = prev.get("user_id", "")
            # 账号或密码变了 → 丢弃旧 token，下次自动用新账密登录
            pwd_changed = raw_pwd not in ("", "******") and raw_pwd != prev.get("password")
            if s.get("username") and (s.get("username") != prev.get("username") or pwd_changed):
                s["api_key"] = ""
                s.pop("token_type", None)
                s["user_id"] = ""
                s.pop("server_id", None)
            merged.append(s)
        data["media_servers"] = merged
    return save_config(data)


@app.post("/api/ms/test")
async def test_server(body: TestServerModel):
    """测试媒体服务器连通性（不落盘；账号密码模式支持沿用已保存的密码）"""
    try:
        d = body.model_dump()
        if d.get("password") in ("", "******") or d.get("api_key") in ("", "******"):
            prev = next((s for s in (load_config().get("media_servers") or [])
                         if s.get("id") == d.get("id")), {})
            if d.get("password") in ("", "******"):
                d["password"] = prev.get("password", "")
            if d.get("api_key") in ("", "******"):
                d["api_key"] = prev.get("api_key", "")
            d["user_id"] = d.get("user_id") or prev.get("user_id", "") or ""
        # 账号密码模式：有密码就直接登录验证，不用旧 token
        if d.get("auth_mode") == "login" and d.get("password"):
            d["api_key"] = ""
        srv = MediaServer(d)
        return await srv.test()
    except HTTPException as e:
        return {"ok": False, "error": e.detail}
    except Exception as e:  # noqa
        return {"ok": False, "error": str(e)[:200]}


@app.post("/api/pan115/test")
async def test_115():
    """测试 115 Cookie 有效性（用已保存的）"""
    try:
        return await pan115.test()
    except HTTPException as e:
        return {"ok": False, "error": e.detail}
    except Exception as e:  # noqa
        return {"ok": False, "error": str(e)[:200]}


@app.get("/api/media/search")
async def media_search(title: str = Query(..., min_length=1),
                       media_type: str = Query("全部"),
                       page: int = 1):
    """搜索媒体：走 MoviePilot（TMDB/豆瓣）"""
    return await mp.search(title, media_type, page)


@app.get("/api/media/popular")
async def media_popular(media_type: str = Query("电影"), page: int = 1):
    """浏览热门：走 MP 接口（自动兼容 v3/v2/v1，多路径回退）"""
    try:
        return await mp.popular(media_type, page)
    except HTTPException:
        raise
    except Exception:
        return []


@app.post("/api/want")
async def want(body: WantModel):
    """想看 → 本地记录 + 通知 MoviePilot 订阅下载"""
    now = int(time.time())
    conn = db()
    conn.execute(
        """INSERT INTO records (ukey, source, media_type, title, year, overview, poster,
             tmdb_id, douban_id, season, status, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,'want',?,?)
           ON CONFLICT(ukey) DO UPDATE SET status='want', updated_at=excluded.updated_at""",
        (ukey_of(body.source, body.media_type, body.title, body.year, body.tmdb_id),
         body.source, body.media_type, body.title, body.year, body.overview,
         body.poster, body.tmdb_id, body.douban_id, body.season, now, now))
    conn.commit()
    rid = conn.execute("SELECT id FROM records WHERE ukey=?",
                       (ukey_of(body.source, body.media_type, body.title, body.year, body.tmdb_id),)
                       ).fetchone()["id"]
    mp_result, mp_ok, mp_skip = None, True, False
    if body.subscribe_mp and body.media_type in ("电影", "电视剧", "短剧", "纪录片", "音乐"):
        if body.media_type == "音乐" and mp.is_v3 is False and \
                (load_config().get("mp_api_version") or "auto").lower() in ("v1", "v2"):
            mp_ok, mp_result, mp_skip = False, "音乐订阅需要 MoviePilot V3", True
        else:
            try:
                mp_result = await mp.subscribe(body.model_dump(), body.season)
            except HTTPException as e:
                mp_ok, mp_result = False, e.detail
            except Exception as e:  # noqa
                mp_ok, mp_result = False, str(e)
    conn.close()
    return {"ok": True, "record_id": rid, "mp_subscribed": mp_ok, "mp_result": mp_result}


@app.post("/api/finish")
def finish(body: FinishModel):
    """看完 → 标记完成 + 评分评论；已看过的再次标记可记为「重看」"""
    now = int(time.time())
    conn = db()
    cur = conn.execute(
        """UPDATE records SET status='done', rating=?, comment=?, tags=?, updated_at=?,
             finished_at=CASE WHEN finished_at IS NULL THEN ? ELSE finished_at END,
             last_done_at=?,
             rewatch=CASE WHEN ? = 1 AND status='done' THEN COALESCE(rewatch, 0) + 1
                          ELSE COALESCE(rewatch, 0) END
           WHERE id=?""",
        (body.rating, body.comment, body.tags, now, now, now,
         1 if body.rewatch else 0, body.record_id))
    conn.commit()
    if cur.rowcount == 0:
        conn.close()
        raise HTTPException(404, "记录不存在")
    row = conn.execute("SELECT * FROM records WHERE id=?", (body.record_id,)).fetchone()
    conn.close()
    return {"ok": True, "record": row_to_dict(row)}


@app.post("/api/drop")
def drop(record_id: int = Query(...)):
    """取消想看（不订阅了）"""
    conn = db()
    conn.execute("DELETE FROM records WHERE id=?", (record_id,))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.get("/api/records")
def records(status: str = Query("all"), media_type: str = Query("all")):
    """我的清单：want / done"""
    sql, args = "SELECT * FROM records", []
    conds = []
    if status in ("want", "done", "watching"):
        conds.append("status=?")
        args.append(status)
    if media_type != "all":
        conds.append("media_type=?")
        args.append(media_type)
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY updated_at DESC"
    conn = db()
    rows = [row_to_dict(r) for r in conn.execute(sql, args).fetchall()]
    conn.close()
    return rows


@app.get("/api/emby/img/{item_id}")
async def legacy_emby_img(item_id: str, w: int = 400):
    """旧记录里存的 /api/emby/img/ 链接 → 转发到第一个 emby/jellyfin 服务器"""
    servers = [s for s in media_servers() if s.type in ("emby", "jellyfin")]
    if not servers:
        raise HTTPException(404, "未配置媒体服务器")
    return await _proxy_img(servers[0], item_id, w)


@app.get("/api/ms/img/{sid}/{item_id}")
async def ms_img(sid: str, item_id: str, w: int = 400):
    """代理任意媒体服务器封面图（带认证，避免浏览器直连 401 / 暴露 Key）"""
    srv = next((s for s in media_servers() if s.id == sid), None)
    if not srv:
        raise HTTPException(404, "服务器不存在或未启用")
    return await _proxy_img(srv, item_id, w)


async def _proxy_img(srv: MediaServer, item_id: str, w: int) -> Response:
    if srv.type == "plex":
        url = f"{srv.base}/library/metadata/{item_id}/thumb"
        async with httpx.AsyncClient(timeout=20, trust_env=False) as c:
            r = await c.get(url, params={"X-Plex-Token": srv.key})
    else:
        tok = await srv.auth()
        url = f"{srv.base}/Items/{item_id}/Images/Primary"
        params = {"maxWidth": w, "quality": 80, "api_key": tok}
        async with httpx.AsyncClient(timeout=20, trust_env=False) as c:
            r = await c.get(url, params=params)
            if r.status_code == 401 and srv.user:
                tok = await srv.auth(force=True)
                params["api_key"] = tok
                r = await c.get(url, params=params)
    if r.status_code != 200:
        raise HTTPException(404, "图片不存在")
    return Response(content=r.content,
                    media_type=r.headers.get("content-type", "image/jpeg"),
                    headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/emby/watched")
async def emby_watched():
    """（兼容旧接口）从所有媒体服务器拉取已看列表预览"""
    out = []
    for s in media_servers():
        try:
            out.extend(await s.watched())
        except Exception as e:  # noqa
            pass
    return out


def _parse_emby_date(s: str) -> Optional[int]:
    """Emby DatePlayed ISO 字符串 → unix 秒；失败返回 None"""
    if not s:
        return None
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except Exception:  # noqa
        return None


@app.get("/api/home/rows")
async def home_rows(resume_limit: int = 12, latest_limit: int = 12):
    """首页两条横滑：继续观看（带播放进度）+ 最近入库"""
    servers = [s for s in media_servers() if s.type != "plex"]
    if not servers:
        return {"ok": True, "resume": [], "latest": []}

    async def _resume(s: MediaServer):
        try:
            return await s._jelly_resume(resume_limit)
        except Exception:  # noqa
            return []

    async def _latest(s: MediaServer):
        try:
            return await s._jelly_latest(latest_limit)
        except Exception:  # noqa
            return []

    r_res = await asyncio.gather(*[_resume(s) for s in servers])
    r_new = await asyncio.gather(*[_latest(s) for s in servers])

    def _dedup(rows: list, key) -> list:
        seen, out = set(), []
        for r in rows:
            k = key(r)
            if k in seen:
                continue
            seen.add(k)
            out.append(r)
        return out

    resume = _dedup([x for r in r_res for x in r],
                    lambda r: (r["title"], r.get("sub") or ""))
    latest = _dedup([x for r in r_new for x in r], lambda r: r["title"])
    # 继续观看按「最久没动」排在后面，最近看的靠前（同一剧只保留一条）
    resume = resume[:resume_limit]
    latest = latest[:latest_limit]

    s_by_id = {s.id: s for s in servers}

    async def _play(item: dict) -> dict:
        s = s_by_id.get(item.get("srv_id"))
        if s and item.get("item_id"):
            try:
                item["play_url"] = await s.play_url(item["item_id"])
            except Exception:  # noqa
                item["play_url"] = None
        return item

    resume = list(await asyncio.gather(*[_play(r) for r in resume]))
    latest = list(await asyncio.gather(*[_play(r) for r in latest]))

    now = datetime.now()
    for it in latest:
        d = _parse_emby_date(it.get("created") or "")
        it["days_ago"] = max(0, (now - datetime.fromtimestamp(d)).days) if d else None

    return {"ok": True, "resume": resume, "latest": latest}


SYNC_LOCK = asyncio.Lock()


async def do_sync() -> dict:
    """把所有媒体服务器已看条目导入本地（接口与后台定时任务共用）"""
    async with SYNC_LOCK:
        now = int(time.time())
        conn = db()
        total, per_server, imported = 0, [], 0
        servers = media_servers()
        for s in servers:
            cnt, err = 0, None
            try:
                items = await s.watched()
                total += len(items)
                for m in items:
                    f_at = _parse_emby_date(m.get("date_played") or "") or now
                    conn.execute(
                        """INSERT INTO records (ukey, source, media_type, title, year, overview, poster,
                             tmdb_id, status, created_at, updated_at, finished_at, runtime)
                           VALUES (?, 'emby', ?, ?, ?, ?, ?, ?, 'done', ?, ?, ?, ?)
                           ON CONFLICT(ukey) DO UPDATE SET status='done', poster=excluded.poster,
                             overview=excluded.overview, updated_at=excluded.updated_at,
                             finished_at=COALESCE(records.finished_at, excluded.finished_at),
                             runtime=COALESCE(records.runtime, excluded.runtime)""",
                        (ukey_of("emby", m["media_type"], m["title"], m["year"], m.get("tmdb_id")),
                         m["media_type"], m["title"], m["year"], m["overview"], m["poster"],
                         m.get("tmdb_id"), now, now, f_at, m.get("runtime")))
                    cnt += 1
                imported += cnt
            except Exception as e:  # noqa
                err = str(e)[:150]
            per_server.append({"server": s.name, "type": s.type, "count": cnt, "error": err})
        conn.commit()
        conn.close()
        summary = f"导入 {imported} 条 · {len(servers)} 台服务器" if servers else "未配置媒体服务器"
        try:
            save_config({"sync_last_at": now, "sync_last_result": summary})
        except Exception:  # noqa
            pass
        return {"ok": True, "imported": imported, "total": total,
                "servers": per_server, "at": now}


@app.post("/api/sync")
@app.post("/api/emby/sync")
async def sync_all():
    """手动触发同步（前端「同步已看」按钮）"""
    return await do_sync()


async def _auto_sync_loop():
    """后台定时同步：启动 30 秒后跑一次，之后按配置间隔循环"""
    await asyncio.sleep(30)
    while True:
        cfg = load_config()
        try:
            if cfg.get("auto_sync", True) and media_servers():
                await do_sync()
        except Exception:  # noqa
            pass
        interval = max(5, int(cfg.get("sync_interval_min") or 60))
        await asyncio.sleep(interval * 60)


# ---------------------------------------------------------------- Webhook（实时同步）
def hook_info() -> dict:
    """Webhook 校验密钥（首次访问时自动生成并存盘）"""
    cfg = load_config()
    key = cfg.get("hook_key") or ""
    if not key:
        key = secrets.token_urlsafe(12)
        save_config({"hook_key": key})
    return {"key": key,
            "last_at": int(cfg.get("hook_last_at") or 0),
            "last_event": cfg.get("hook_last_event") or ""}


@app.get("/api/hook/info")
def api_hook_info():
    """设置页用：返回 webhook 密钥与最近收到的事件"""
    return {"ok": True, **hook_info()}


async def _hook_sync_soon(delay: float = 8.0):
    """Webhook 落库后稍等片刻再同步一次，补全海报/简介/时长等元数据"""
    await asyncio.sleep(delay)
    try:
        await do_sync()
    except Exception:  # noqa
        pass


@app.post("/api/hook/emby")
async def emby_hook(request: Request, k: str = ""):
    """Emby / Jellyfin Webhook：播放结束时即时标记完成，不等定时同步"""
    info = hook_info()
    if info["key"] and k != info["key"]:
        raise HTTPException(403, "hook 密钥不正确")
    try:
        data = await request.json()
    except Exception:  # noqa
        data = {}
    if not isinstance(data, dict):
        data = {}
    ev = str(data.get("Event") or data.get("NotificationType") or "").lower()
    ev = ev.replace(".", "").replace("_", "").replace("-", "")
    item = data.get("Item") or {}
    name = item.get("Name") or item.get("SeriesName") or ""
    save_config({"hook_last_at": int(time.time()),
                 "hook_last_event": (f"{ev or '事件'} · {name}")[:100]})
    if ev in ("playbackstop", "playbackfinished") and item:
        total = int(item.get("RunTimeTicks") or 0)
        pbi = data.get("PlaybackInfo") or {}
        pos = int(pbi.get("PositionTicks") or data.get("PositionTicks") or 0)
        if total and pos and pos / total >= 0.85:      # 看了 85% 以上才算看完
            is_ep = item.get("Type") == "Episode"
            title = item.get("SeriesName") if is_ep else item.get("Name")
            if title:
                now = int(time.time())
                rt = int(total / 600_000_000) or None
                conn = db()
                conn.execute(
                    """INSERT INTO records (ukey, source, media_type, title, year, overview, poster,
                         tmdb_id, status, created_at, updated_at, finished_at, runtime)
                       VALUES (?, 'emby', ?, ?, ?, '', '', ?, 'done', ?, ?, ?, ?)
                       ON CONFLICT(ukey) DO UPDATE SET status='done', updated_at=excluded.updated_at,
                         finished_at=COALESCE(records.finished_at, excluded.finished_at),
                         runtime=COALESCE(records.runtime, excluded.runtime)""",
                    (ukey_of("emby", "电视剧" if is_ep else "电影", title,
                             str(item.get("SeriesProductionYear") or item.get("ProductionYear") or ""),
                             (item.get("ProviderIds") or {}).get("Tmdb")),
                     "电视剧" if is_ep else "电影", title,
                     str(item.get("SeriesProductionYear") or item.get("ProductionYear") or ""),
                     (item.get("ProviderIds") or {}).get("Tmdb"), now, now, now, rt))
                conn.commit()
                conn.close()
                asyncio.create_task(_hook_sync_soon())
                return {"ok": True, "recorded": title}
    return {"ok": True, "event": ev}


@app.get("/api/resource/check")
async def resource_check(title: str = Query(..., min_length=1)):
    """一键检查标题在所有自有资源库（Emby/Jellyfin/Plex/115）中是否存在"""
    import asyncio

    async def _one(s: MediaServer):
        try:
            items = await s.search(title)
            # 每个条目补播放深链（媒体服务器网页直达）
            for it in items:
                try:
                    it["play_url"] = await s.play_url(it.get("item_id"))
                except Exception:  # noqa
                    it["play_url"] = None
            return {"id": s.id, "name": s.name, "type": s.type,
                    "ok": True, "count": len(items), "items": items}
        except Exception as e:  # noqa
            return {"id": s.id, "name": s.name, "type": s.type,
                    "ok": False, "count": 0, "error": str(e)[:120], "items": []}

    async def _pan115():
        cfg = load_config()
        if not (cfg.get("pan_115_cookie") or "").strip():
            return None
        try:
            files = await pan115.search(title)
            return {"id": "pan115", "name": "115 网盘", "type": "115",
                    "ok": True, "count": len(files), "items": files[:8]}
        except Exception as e:  # noqa
            return {"id": "pan115", "name": "115 网盘", "type": "115",
                    "ok": False, "count": 0, "error": str(e)[:120], "items": []}

    tasks = [_one(s) for s in media_servers()] + [_pan115()]
    results = [r for r in await asyncio.gather(*tasks) if r]
    return {"ok": True, "results": results}


@app.get("/api/stats")
def stats():
    """统计汇总 + 日历热力图数据（最近一年）"""
    conn = db()
    rows = [row_to_dict(r) for r in conn.execute("SELECT * FROM records").fetchall()]
    conn.close()
    done = [r for r in rows if r["status"] == "done"]
    want = [r for r in rows if r["status"] == "want"]
    rated = [r for r in done if r["rating"]]
    by_type_done = {}
    for r in done:
        by_type_done[r["media_type"]] = by_type_done.get(r["media_type"], 0) + 1
    now = int(time.time())
    month_start = int(datetime.now().replace(day=1, hour=0, minute=0,
                                              second=0, microsecond=0).timestamp())
    # 热力图：按 finished_at 聚合到天（本地时区）
    activity = {}
    for r in done:
        f_at = r.get("finished_at") or r.get("updated_at")
        if not f_at:
            continue
        d = datetime.fromtimestamp(f_at).strftime("%Y-%m-%d")
        activity[d] = activity.get(d, 0) + 1
    top_rated = sorted(rated, key=lambda r: -r["rating"])[:8]
    recent = sorted(done, key=lambda r: -(r.get("finished_at") or r["updated_at"]))[:8]
    want_list = sorted(want, key=lambda r: -r["updated_at"])[:8]
    rating_dist = {}
    for r in rated:
        rating_dist[str(r["rating"])] = rating_dist.get(str(r["rating"]), 0) + 1
    # 观影时长 + 最爱导演/演员（media_meta 有则用真实值，无则按类型估算）
    conn2 = db()
    metas = {m["mkey"]: row_to_dict(m) for m in
             conn2.execute("SELECT * FROM media_meta").fetchall()} \
        if _table_exists(conn2, "media_meta") else {}
    conn2.close()
    total_minutes, dir_cnt, act_cnt = 0, {}, {}
    for r in done:
        meta = metas.get(meta_key(r["media_type"], r["title"], r.get("year") or ""))
        rt = (meta and meta.get("runtime")) or r.get("runtime") or \
             (110 if r["media_type"] == "电影" else 45 if r["media_type"] in
              ("电视剧", "短剧", "纪录片") else 60)
        total_minutes += rt or 0
        for field, cnt in (("directors", dir_cnt), ("actors", act_cnt)):
            for p in _split_persons(meta.get(field) if meta else ""):
                cnt[p] = cnt.get(p, 0) + 1
    this_year_done = sum(
        1 for r in done
        if (r.get("finished_at") or 0) >= int(datetime(datetime.now().year, 1, 1).timestamp()))
    goal_target = int(load_config().get("year_goal") or 0)
    return {
        "done_count": len(done),
        "want_count": len(want),
        "by_type_done": by_type_done,
        "avg_rating": round(sum(r["rating"] for r in rated) / len(rated), 1) if rated else None,
        "month_count": sum(1 for r in done
                           if (r.get("finished_at") or 0) >= month_start),
        "comment_count": sum(1 for r in done if r.get("comment")),
        "rating_dist": rating_dist,
        "activity": activity,
        "top_rated": [{"title": r["title"], "media_type": r["media_type"],
                       "rating": r["rating"], "poster": r["poster"]} for r in top_rated],
        "recent": [{"title": r["title"], "media_type": r["media_type"],
                    "finished_at": r.get("finished_at") or r["updated_at"],
                    "poster": r["poster"]} for r in recent],
        "want_list": [{"title": r["title"], "media_type": r["media_type"],
                       "year": r.get("year") or "",
                       "created_at": r["created_at"],
                       "mp_subscribed": r.get("mp_subscribed"),
                       "poster": r["poster"]} for r in want_list],
        "server_now": now,
        "total_hours": round(total_minutes / 60, 1),
        "top_directors": [{"name": k, "count": v} for k, v in
                          sorted(dir_cnt.items(), key=lambda x: -x[1])[:5]],
        "top_actors": [{"name": k, "count": v} for k, v in
                       sorted(act_cnt.items(), key=lambda x: -x[1])[:5]],
        "goal": {"target": goal_target, "done": this_year_done,
                 "pct": min(100, round(this_year_done / goal_target * 100)) if goal_target else None},
        "rewatch": {
            "times": sum(int(r.get("rewatch") or 0) for r in done),
            "titles": sum(1 for r in done if int(r.get("rewatch") or 0) > 0),
        },
    }


_STORAGE_CACHE = {"at": 0.0, "data": None}


@app.get("/api/storage")
async def storage(refresh: bool = False):
    """存储体检：媒体库体积、占用 Top、可清理清单（10 分钟缓存）"""
    if (not refresh and _STORAGE_CACHE["data"]
            and time.time() - _STORAGE_CACHE["at"] < 600):
        return _STORAGE_CACHE["data"]

    servers = [s for s in media_servers() if s.type != "plex"]
    if not servers:
        return {"ok": True, "total_gb": 0, "count": 0, "by_type": [], "top": [],
                "cleanable": {"count": 0, "gb": 0, "items": []}}

    async def _one(s: MediaServer):
        try:
            return await s._jelly_sizes()
        except Exception:  # noqa
            return []

    results = await asyncio.gather(*[_one(s) for s in servers])

    groups, total = {}, 0
    for items in results:
        for it in items:
            size = int(it.get("Size") or 0)
            if size <= 0:
                continue
            is_ep = it.get("Type") == "Episode"
            title = (it.get("SeriesName") if is_ep else it.get("Name")) or ""
            if not title:
                continue
            key = ("剧" if is_ep else "影") + "|" + title
            g = groups.setdefault(key, {
                "title": title, "type": "电视剧" if is_ep else "电影",
                "year": str(it.get("SeriesProductionYear") or it.get("ProductionYear") or ""),
                "size": 0, "eps": 0, "played": 0, "last": 0,
            })
            ud = it.get("UserData") or {}
            g["size"] += size
            total += size
            if is_ep:
                g["eps"] += 1
                if ud.get("Played"):
                    g["played"] += 1
            else:
                g["eps"] = 1
                if ud.get("Played"):
                    g["played"] = 1
            d = _parse_emby_date(ud.get("LastPlayedDate") or "") or 0
            if d > g["last"]:
                g["last"] = d

    # 本地评分（判断「值不值得留」）
    conn = db()
    recs = {r["title"]: r for r in conn.execute(
        "SELECT title, rating, status FROM records").fetchall()}
    metas = {}
    if _table_exists(conn, "media_meta"):
        for m in conn.execute("SELECT mkey, douban_rating FROM media_meta").fetchall():
            metas[m["mkey"]] = m["douban_rating"]
    conn.close()

    GB = 1024 ** 3
    now = time.time()
    by_type = {}
    for g in groups.values():
        by_type[g["type"]] = by_type.get(g["type"], 0) + g["size"]
    top = sorted(groups.values(), key=lambda g: -g["size"])[:10]

    cleanable = []
    for g in groups.values():
        if g["eps"] and g["played"] < g["eps"]:
            continue                                  # 没看完的先不建议删
        rec = recs.get(g["title"])
        my = rec["rating"] if rec else None
        db_score = metas.get(meta_key(g["type"], g["title"], g["year"]))
        dull = ((my is not None and my <= 6)
                or (db_score is not None and float(db_score) <= 6.0))
        idle = bool(g["last"]) and (now - g["last"]) > 365 * 86400
        if dull or idle:
            cleanable.append({**g, "my": my,
                              "douban": round(float(db_score), 1) if db_score else None,
                              "reason": "评分一般" if dull else "一年以上没看"})
    cleanable.sort(key=lambda g: -g["size"])

    data = {
        "ok": True,
        "total_gb": round(total / GB, 1),
        "count": len(groups),
        "by_type": [{"name": k, "gb": round(v / GB, 1)} for k, v in
                    sorted(by_type.items(), key=lambda x: -x[1])],
        "top": [{"title": g["title"], "type": g["type"], "year": g["year"],
                 "gb": round(g["size"] / GB, 2), "eps": g["eps"]} for g in top],
        "cleanable": {
            "count": len(cleanable),
            "gb": round(sum(g["size"] for g in cleanable) / GB, 1),
            "items": [{"title": g["title"], "type": g["type"],
                       "gb": round(g["size"] / GB, 2), "reason": g["reason"],
                       "my": g["my"], "douban": g["douban"]} for g in cleanable[:10]],
        },
    }
    _STORAGE_CACHE.update({"at": time.time(), "data": data})
    return data


@app.get("/api/taste")
def taste():
    """口味画像：评分偏好 / 类型 / 年代 / 题材 / 导演演员"""
    conn = db()
    rows = [row_to_dict(r) for r in conn.execute(
        "SELECT * FROM records WHERE status='done'").fetchall()]
    metas = {}
    if _table_exists(conn, "media_meta"):
        metas = {m["mkey"]: row_to_dict(m) for m in
                 conn.execute("SELECT * FROM media_meta").fetchall()}
    conn.close()

    rated = [r for r in rows if r.get("rating")]
    if not rated:
        return {"ok": True, "n": len(rows), "rated": 0, "enough": False,
                "by_type": [], "by_decade": [], "genres": [],
                "directors": [], "actors": [], "cover": 0}

    def meta_of(r):
        return metas.get(meta_key(r["media_type"], r["title"], r.get("year") or "")) or {}

    def bucket(items):
        """{名称: [次数, 评分和, 评分次数]}"""
        out = {}
        for r in items:
            key = r["_k"]
            if not key:
                continue
            c = out.setdefault(key, [0, 0, 0])
            c[0] += 1
            if r.get("rating"):
                c[1] += r["rating"]
                c[2] += 1
        return [{"name": k, "count": v[0],
                 "avg": round(v[1] / v[2], 1) if v[2] else None}
                for k, v in sorted(out.items(), key=lambda x: -x[1][0])]

    by_type = bucket([{**r, "_k": r["media_type"]} for r in rows])

    def decade(y):
        try:
            n = int(str(y)[:4])
        except Exception:  # noqa
            return ""
        if n < 1950:
            return "更早"
        return f"{n // 10 * 10}s"

    by_decade = bucket([{**r, "_k": decade(r.get("year") or "")} for r in rows])
    by_decade = sorted([d for d in by_decade if d["name"]],
                       key=lambda d: d["name"], reverse=True)

    genre_rows, dir_rows, act_rows = [], [], []
    for r in rows:
        m = meta_of(r)
        for g in _split_persons(m.get("genres") or ""):
            genre_rows.append({**r, "_k": g})
        for p in _split_persons(m.get("directors") or ""):
            dir_rows.append({**r, "_k": p})
        for p in _split_persons(m.get("actors") or ""):
            act_rows.append({**r, "_k": p})

    cover = sum(1 for r in rows if meta_of(r).get("genres"))
    return {
        "ok": True,
        "n": len(rows), "rated": len(rated), "enough": len(rated) >= 5,
        "avg": round(sum(r["rating"] for r in rated) / len(rated), 1),
        "by_type": [t for t in by_type if t["name"]][:6],
        "by_decade": by_decade[:8],
        "genres": [g for g in bucket(genre_rows) if g["name"]][:10],
        "directors": [d for d in bucket(dir_rows) if d["name"]][:8],
        "actors": [a for a in bucket(act_rows) if a["name"]][:8],
        "cover": round(cover / len(rows) * 100) if rows else 0,
    }


@app.get("/api/rating-gap")
def rating_gap():
    """我和豆瓣的评分偏差"""
    conn = db()
    done = [row_to_dict(r) for r in conn.execute(
        "SELECT * FROM records WHERE status='done' AND rating IS NOT NULL").fetchall()]
    metas = {}
    if _table_exists(conn, "media_meta"):
        metas = {m["mkey"]: row_to_dict(m) for m in
                 conn.execute("SELECT * FROM media_meta").fetchall()}
    conn.close()
    pairs = []
    for r in done:
        m = metas.get(meta_key(r["media_type"], r["title"], r.get("year") or "")) or {}
        db_score = m.get("douban_rating")
        if not db_score:
            continue
        pairs.append({
            "title": r["title"], "media_type": r["media_type"],
            "year": r.get("year") or "", "poster": r.get("poster") or "",
            "my": r["rating"], "douban": round(float(db_score), 1),
            "gap": round(r["rating"] - float(db_score), 1),
        })
    if not pairs:
        return {"ok": True, "n": 0, "my_avg": None, "douban_avg": None,
                "delta": None, "over": [], "under": [], "same": 0}
    n = len(pairs)
    my_avg = sum(p["my"] for p in pairs) / n
    db_avg = sum(p["douban"] for p in pairs) / n
    over = sorted(pairs, key=lambda p: -p["gap"])[:5]
    under = sorted(pairs, key=lambda p: p["gap"])[:5]
    return {
        "ok": True, "n": n,
        "my_avg": round(my_avg, 1), "douban_avg": round(db_avg, 1),
        "delta": round(my_avg - db_avg, 1),
        "over": [p for p in over if p["gap"] > 0],
        "under": [p for p in under if p["gap"] < 0],
        "same": sum(1 for p in pairs if abs(p["gap"]) < 0.6),
    }


def _table_exists(conn, name: str) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def _split_persons(s: str) -> list:
    if not s:
        return []
    return [p.strip() for p in str(s).split(",") if p.strip() and p.strip() != "None"]


@app.get("/api/report")
def report(year: Optional[int] = None):
    """年度观影报告（Wrapped 风格数据）"""
    conn = db()
    rows = [row_to_dict(r) for r in conn.execute(
        "SELECT * FROM records WHERE status='done'").fetchall()]
    conn.close()
    this_year = datetime.now().year
    years = sorted({datetime.fromtimestamp(r.get("finished_at") or r["updated_at"]).year
                    for r in rows} | {this_year}, reverse=True)
    # year=0 表示「全部时间」
    all_time = (year == 0)
    year = this_year if year is None else year
    if all_time:
        items = rows
    else:
        y_start = int(datetime(year, 1, 1).timestamp())
        y_end = int(datetime(year + 1, 1, 1).timestamp())
        items = [r for r in rows
                 if y_start <= (r.get("finished_at") or r["updated_at"]) < y_end]
    rated = [r for r in items if r["rating"]]
    by_type = {}
    for r in items:
        by_type[r["media_type"]] = by_type.get(r["media_type"], 0) + 1
    monthly = [0] * 12
    for r in items:
        monthly[datetime.fromtimestamp(r.get("finished_at") or r["updated_at"]).month - 1] += 1
    rating_dist = {str(i): 0 for i in range(1, 11)}
    for r in rated:
        rating_dist[str(r["rating"])] += 1
    # 连续观看天数（当天有多部也算 1 天）
    days = sorted({datetime.fromtimestamp(r.get("finished_at") or r["updated_at"]).date()
                   for r in items})
    streak = best_streak = 0
    prev = None
    for d in days:
        streak = streak + 1 if (prev and (d - prev).days == 1) else 1
        best_streak = max(best_streak, streak)
        prev = d
    best_month_idx = monthly.index(max(monthly)) if items else 0
    top10 = sorted(rated, key=lambda r: (-r["rating"], -(r.get("finished_at") or 0)))[:10]
    comments = [r for r in items if r.get("comment")]
    comments.sort(key=lambda r: -(r.get("finished_at") or 0))
    total_minutes = 0
    for r in items:
        rt = r.get("runtime") or (110 if r["media_type"] == "电影" else
                                  45 if r["media_type"] in ("电视剧", "短剧", "纪录片") else 60)
        total_minutes += rt or 0
    return {
        "year": 0 if all_time else year, "years": years,
        "all_time": all_time,
        "min_year": min(years) if years else this_year,
        "total": len(items),
        "total_hours": round(total_minutes / 60, 1),
        "by_type": by_type,
        "monthly": monthly,
        "rating_dist": rating_dist,
        "avg_rating": round(sum(r["rating"] for r in rated) / len(rated), 1) if rated else None,
        "rated_count": len(rated),
        "comment_count": len(comments),
        "active_days": len(days),
        "best_streak": best_streak,
        "span": [datetime.fromtimestamp(
                    min(r.get("finished_at") or r["updated_at"] for r in items)).year,
                 datetime.fromtimestamp(
                    max(r.get("finished_at") or r["updated_at"] for r in items)).year] if items else None,
        "best_month": {"month": best_month_idx + 1, "count": max(monthly)} if items else None,
        "top10": [{"title": r["title"], "media_type": r["media_type"], "year": r.get("year") or "",
                   "rating": r["rating"], "poster": r["poster"]} for r in top10],
        "comments": [{"title": r["title"], "rating": r["rating"], "comment": r["comment"],
                      "tags": r.get("tags") or "",
                      "finished_at": r.get("finished_at") or r["updated_at"],
                      "poster": r["poster"]} for r in comments[:12]],
    }


@app.get("/api/subscriptions")
async def subscriptions():
    """MP 当前订阅状态（追更进度）"""
    try:
        subs = await mp.list_subscriptions()
        return {"ok": True, "list": subs}
    except HTTPException as e:
        return {"ok": False, "detail": e.detail, "list": []}


# ================================================================ 以下为 8 大新功能端点

def _person_str(v: Any) -> str:
    """主创字段归一化：str / list[str] / list[dict] → 'a,b,c'"""
    if not v:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        names = []
        for x in v:
            if isinstance(x, dict):
                n = x.get("name") or x.get("Name") or ""
            else:
                n = str(x)
            if n:
                names.append(n)
        return ",".join(names[:8])
    return str(v)


def _norm_rating(m: dict) -> Optional[float]:
    for k in ("rating", "douban_rating", "vote_average"):
        v = m.get(k)
        try:
            v = float(v)
        except (TypeError, ValueError):
            continue
        if 0 < v <= 10:
            return round(v, 1)
    return None


def _norm_runtime(m: dict) -> Optional[int]:
    v = m.get("runtime") or m.get("douban_runtime")
    try:
        v = int(v)
        if 1 < v < 1500:
            return v
    except (TypeError, ValueError):
        pass
    return None


async def _douban_direct_lookup(title: str, year: str, media_type: str) -> Optional[dict]:
    """MP 豆瓣源不可用时的兜底：豆瓣公开建议接口 + 移动端 rexxar API"""
    suggest_h = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                               "Chrome/124.0 Safari/537.36",
                 "Referer": "https://movie.douban.com/"}
    mobile_h = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
                              "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1",
                "Referer": "https://m.douban.com/movie/"}
    async with httpx.AsyncClient(timeout=15, headers=suggest_h, follow_redirects=True) as c:
        r = await c.get("https://movie.douban.com/j/subject_suggest", params={"q": title})
        if r.status_code != 200:
            return None
        try:
            sugg = r.json()
        except Exception:  # noqa
            return None
    if not isinstance(sugg, list) or not sugg:
        return None

    def _score(s: dict) -> int:
        sc = 0
        st = (s.get("title") or "").lower()
        if st == title.lower():
            sc += 10
        elif title.lower() in st or st in title.lower():
            sc += 5
        if year and str(s.get("year") or "") == str(year):
            sc += 6
        return sc

    best = max(sugg, key=_score)
    if _score(best) <= 0:
        return None
    sid = best.get("id") or ""
    if not sid:
        return None
    is_tv = (best.get("type") == "tv") or media_type in ("电视剧", "短剧", "纪录片")
    # rexxar 详情（movie/tv 两种路径都试）
    detail = None
    async with httpx.AsyncClient(timeout=15, headers=mobile_h, follow_redirects=True) as c:
        for sub in (("tv", "movie") if is_tv else ("movie", "tv")):
            try:
                r2 = await c.get(f"https://m.douban.com/rexxar/api/v2/{sub}/{sid}")
            except Exception:  # noqa
                continue
            if r2.status_code == 200:
                try:
                    detail = r2.json()
                    break
                except Exception:  # noqa
                    detail = None
    info = {"douban_id": int(sid) if str(sid).isdigit() else None,
            "title": best.get("title") or title,
            "year": str(best.get("year") or year or ""),
            "rating": None, "votes": None, "overview": "", "poster": "",
            "directors": "", "actors": "", "genres": "", "runtime": None}
    if not isinstance(detail, dict) or not detail.get("id"):
        return info
    rating = detail.get("rating") or {}
    info["rating"] = round(float(rating["value"]), 1) if rating.get("value") else None
    info["votes"] = rating.get("count")
    persons = lambda lst: ",".join(  # noqa: E731
        (p.get("name") or "") for p in (lst or [])[:8] if p.get("name"))
    info["directors"] = persons(detail.get("directors"))
    info["actors"] = persons(detail.get("actors"))
    info["genres"] = ",".join(detail.get("genres") or [])
    info["overview"] = str(detail.get("intro") or "")[:1500]
    info["poster"] = detail.get("cover_url") or (detail.get("cover") or {}).get("url") or ""
    for dur in detail.get("durations") or []:
        import re as _re
        m = _re.search(r"(\d+)", str(dur))
        if m:
            info["runtime"] = int(m.group(1))
            break
    if detail.get("year"):
        info["year"] = str(detail["year"])
    return info


async def _mp_douban_lookup(title: str, year: str, media_type: str) -> Optional[dict]:
    """通过 MP 的豆瓣数据源查找条目：先豆瓣源搜索，再豆瓣详情补全主创/片长"""
    params_list = [
        {"title": title, "page": 1, "count": 10, "source": "douban"},
        {"title": title, "page": 1, "count": 10},
    ]
    hits = []
    for params in params_list:
        try:
            data = await mp._try_paths(["/media/search"], params=params)
            hits = data if isinstance(data, list) else (data or {}).get("list", [])
        except Exception:  # noqa
            hits = []
        if hits and any(m.get("douban_id") or m.get("doubanid") for m in hits):
            break
        hits = []
    if not hits:
        return None
    t_low = title.lower()

    def _score(m: dict) -> int:
        s = 0
        name = (m.get("title") or m.get("name") or "").lower()
        if name == t_low:
            s += 10
        elif t_low in name or name in t_low:
            s += 5
        my = str(m.get("year") or "")
        if year and my == str(year):
            s += 6
        if m.get("douban_id") or m.get("doubanid"):
            s += 3
        if media_type in ("电视剧", "短剧", "纪录片") and str(m.get("type")) == "电影":
            s -= 4
        if media_type == "电影" and str(m.get("type")) not in ("电影", "movie", ""):
            s -= 4
        return s

    best = max(hits, key=_score)
    if _score(best) <= 0:
        return None
    info = {
        "douban_id": best.get("douban_id") or best.get("doubanid"),
        "rating": _norm_rating(best),
        "overview": best.get("overview") or best.get("description") or "",
        "poster": best.get("poster_path") or best.get("poster") or "",
        "directors": _person_str(best.get("directors") or best.get("director")),
        "actors": _person_str(best.get("actors") or best.get("actor")),
        "genres": _person_str(best.get("genres")),
        "runtime": _norm_runtime(best),
        "votes": best.get("votes") or best.get("vote_count"),
        "title": best.get("title") or title,
        "year": str(best.get("year") or year or ""),
    }
    # 豆瓣详情补全（评分/主创/片长/简介）
    if info["douban_id"]:
        for detail_params in ({"mediaid": f"douban:{info['douban_id']}"},
                              {"doubanid": info["douban_id"]}):
            try:
                d = await mp._try_paths(["/media/detail", "/douban/detail"],
                                        params=detail_params)
            except Exception:  # noqa
                d = None
            if isinstance(d, dict) and d:
                info["rating"] = _norm_rating(d) or info["rating"]
                info["runtime"] = _norm_runtime(d) or info["runtime"]
                info["directors"] = _person_str(d.get("directors") or d.get("director")) \
                    or info["directors"]
                info["actors"] = _person_str(d.get("actors") or d.get("actor")) or info["actors"]
                info["genres"] = _person_str(d.get("genres")) or info["genres"]
                info["overview"] = d.get("overview") or d.get("description") or info["overview"]
                info["poster"] = info["poster"] or d.get("poster_path") or d.get("poster") or ""
                info["votes"] = info["votes"] or d.get("votes") or d.get("vote_count")
                break
    return info


def _cache_meta(media_type: str, title: str, year: str, info: dict):
    conn = db()
    conn.execute(
        """INSERT INTO media_meta (mkey, douban_id, douban_rating, votes, directors,
             actors, genres, runtime, douban_summary, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(mkey) DO UPDATE SET douban_id=excluded.douban_id,
             douban_rating=excluded.douban_rating, votes=excluded.votes,
             directors=excluded.directors, actors=excluded.actors, genres=excluded.genres,
             runtime=excluded.runtime, douban_summary=excluded.douban_summary,
             updated_at=excluded.updated_at""",
        (meta_key(media_type, title, year), info.get("douban_id"), info.get("rating"),
         info.get("votes"), info.get("directors"), info.get("actors"), info.get("genres"),
         info.get("runtime"), info.get("overview") or "", int(time.time())))
    # 顺手把片长写回记录（时长统计用）
    if info.get("runtime"):
        conn.execute("UPDATE records SET runtime=? WHERE title=? AND media_type=?",
                     (info["runtime"], title, media_type))
    conn.commit()
    conn.close()


@app.get("/api/douban/info")
async def douban_info(title: str = Query(..., min_length=1),
                      year: str = "", media_type: str = "电影"):
    """豆瓣评分/简介/主创聚合（走 MoviePilot 豆瓣数据源，结果缓存）"""
    mkey = meta_key(media_type, title, year)
    conn = db()
    cached = None
    if _table_exists(conn, "media_meta"):
        row = conn.execute("SELECT * FROM media_meta WHERE mkey=?", (mkey,)).fetchone()
        cached = row_to_dict(row) if row else None
    conn.close()
    # 缓存 7 天（仅当命中过有效数据）
    if cached and time.time() - cached["updated_at"] < 7 * 86400 and \
            (cached.get("douban_rating") or cached.get("directors") or cached.get("runtime")):
        return {"ok": True, "cached": True, **cached}
    try:
        info = await _mp_douban_lookup(title, year, media_type)
        if not info or not (info.get("douban_id") or info.get("rating")):
            info = await _douban_direct_lookup(title, year, media_type)  # MP 豆瓣源不可用 → 直连兜底
    except Exception as e:  # noqa
        info = None
        err = str(e)[:150]
    else:
        err = None
    if not info or not (info.get("douban_id") or info.get("rating")):
        return {"ok": False, "error": err or "未在豆瓣数据源中匹配到该片"}
    # 统一字段名：与 media_meta 列一致，缓存与否前端无感
    norm = {
        "douban_id": info.get("douban_id"),
        "douban_rating": info.get("rating"),
        "votes": info.get("votes"),
        "directors": info.get("directors") or "",
        "actors": info.get("actors") or "",
        "genres": info.get("genres") or "",
        "runtime": info.get("runtime"),
        "douban_summary": info.get("overview") or "",
        "poster": info.get("poster") or "",
        "title": info.get("title") or title,
        "year": info.get("year") or year or "",
    }
    if norm.get("douban_rating") or norm.get("directors") or norm.get("runtime"):
        _cache_meta(media_type, title, year, info)
        return {"ok": True, "cached": False, "mkey": mkey, **norm}
    return {"ok": True, "cached": False, **norm}


@app.get("/api/memories")
def memories():
    """回忆杀：往年今天看完的作品"""
    today = datetime.now()
    conn = db()
    rows = [row_to_dict(r) for r in conn.execute(
        "SELECT * FROM records WHERE status='done'").fetchall()]
    conn.close()
    out = []
    for r in rows:
        ts = r.get("finished_at") or r.get("updated_at")
        if not ts:
            continue
        dt = datetime.fromtimestamp(ts)
        if dt.year < today.year and (dt.month, dt.day) == (today.month, today.day):
            out.append({"title": r["title"], "media_type": r["media_type"],
                        "year": r.get("year") or "", "poster": r["poster"],
                        "rating": r["rating"], "comment": r.get("comment") or "",
                        "watched_at": ts, "years_ago": today.year - dt.year})
    out.sort(key=lambda x: -x["watched_at"])
    if out:
        return {"ok": True, "list": out[:6], "date": today.strftime("%m-%d"), "mode": "anniversary"}
    # 兜底：还没有跨年数据时，显示最近看完的作品（否则横幅永远不出现，功能像不存在）
    recent = sorted(rows, key=lambda r: -(r.get("finished_at") or r.get("updated_at") or 0))
    for r in recent[:3]:
        ts = r.get("finished_at") or r.get("updated_at")
        if not ts:
            continue
        days = max(0, (today - datetime.fromtimestamp(ts)).days)
        out.append({"title": r["title"], "media_type": r["media_type"],
                    "year": r.get("year") or "", "poster": r["poster"],
                    "rating": r["rating"], "comment": r.get("comment") or "",
                    "watched_at": ts, "days_ago": days})
    return {"ok": True, "list": out, "date": today.strftime("%m-%d"), "mode": "recent"}


class ListModel(BaseModel):
    name: str


class ListItemModel(BaseModel):
    title: str
    media_type: str
    year: str = ""
    tmdb_id: Optional[int] = None
    source: str = "mp"
    poster: str = ""    # 无记录时的快照封面
    ukey: str = ""      # 已有记录可直接传 ukey


@app.get("/api/lists")
def lists_index():
    conn = db()
    out = []
    for l in conn.execute("SELECT * FROM lists ORDER BY created_at DESC").fetchall():
        lid = l["id"]
        cnt = conn.execute("SELECT COUNT(*) c FROM list_items WHERE list_id=?",
                           (lid,)).fetchone()["c"]
        posters = [r["poster"] for r in conn.execute(
            """SELECT COALESCE(NULLIF(r.poster,''), li.poster) AS poster
               FROM list_items li LEFT JOIN records r ON r.ukey=li.ukey
               WHERE li.list_id=? AND COALESCE(NULLIF(r.poster,''), li.poster) != ''
               LIMIT 4""", (lid,)).fetchall()]
        out.append({"id": lid, "name": l["name"], "count": cnt, "posters": posters})
    conn.close()
    return out


@app.post("/api/lists")
def list_create(body: ListModel):
    conn = db()
    cur = conn.execute("INSERT INTO lists (name, created_at) VALUES (?,?)",
                       (body.name.strip(), int(time.time())))
    conn.commit()
    lid = cur.lastrowid
    conn.close()
    return {"ok": True, "id": lid}


@app.delete("/api/lists/{lid}")
def list_delete(lid: int):
    conn = db()
    conn.execute("DELETE FROM lists WHERE id=?", (lid,))
    conn.execute("DELETE FROM list_items WHERE list_id=?", (lid,))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.get("/api/lists/{lid}")
def list_detail(lid: int):
    """片单详情：有本地记录的返回完整信息，否则返回加入时的快照"""
    conn = db()
    l = conn.execute("SELECT * FROM lists WHERE id=?", (lid,)).fetchone()
    if not l:
        conn.close()
        raise HTTPException(404, "片单不存在")
    items = []
    for li in conn.execute(
            """SELECT li.*, r.id AS rec_id, r.overview AS rec_overview, r.status AS rec_status,
                 r.rating AS rec_rating, r.comment AS rec_comment, r.tags AS rec_tags,
                 r.tmdb_id AS rec_tmdb, r.douban_id AS rec_douban
               FROM list_items li LEFT JOIN records r ON r.ukey=li.ukey
               WHERE li.list_id=? ORDER BY li.added_at DESC""", (lid,)).fetchall():
        items.append({
            "id": li["rec_id"],
            "ukey": li["ukey"],
            "source": "mp" if li["rec_id"] else "local",
            "media_type": li["media_type"] or "电影",
            "title": li["title"],
            "year": li["year"] or "",
            "overview": li["rec_overview"] or "",
            "poster": li["poster"] or "",
            "tmdb_id": li["rec_tmdb"],
            "douban_id": li["rec_douban"],
            "status": li["rec_status"] or "none",
            "rating": li["rec_rating"],
            "comment": li["rec_comment"] or "",
            "tags": li["rec_tags"] or "",
        })
    conn.close()
    return {"id": lid, "name": l["name"], "items": items}


@app.post("/api/lists/{lid}/items")
def list_add_item(lid: int, body: ListItemModel):
    ukey = body.ukey or ukey_of(body.source, body.media_type, body.title, body.year,
                                body.tmdb_id)
    conn = db()
    if not conn.execute("SELECT 1 FROM lists WHERE id=?", (lid,)).fetchone():
        conn.close()
        raise HTTPException(404, "片单不存在")
    rec = conn.execute("SELECT * FROM records WHERE ukey=?", (ukey,)).fetchone()
    title = (rec["title"] if rec else body.title) or body.title
    media_type = (rec["media_type"] if rec else body.media_type) or body.media_type
    year = (rec["year"] if rec else body.year) or body.year
    poster = (rec["poster"] if rec else "") or body.poster
    conn.execute(
        """INSERT OR IGNORE INTO list_items (list_id, ukey, added_at, title, media_type, year, poster)
           VALUES (?,?,?,?,?,?,?)""",
        (lid, ukey, int(time.time()), title, media_type, year, poster))
    conn.commit()
    conn.close()
    return {"ok": True, "ukey": ukey}


@app.delete("/api/lists/{lid}/items")
def list_remove_item(lid: int, ukey: str = Query(...)):
    conn = db()
    conn.execute("DELETE FROM list_items WHERE list_id=? AND ukey=?", (lid, ukey))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.get("/api/series/progress")
async def series_progress():
    """追剧进度：Emby 看到第几季第几集 + MP 订阅状态合并"""
    import asyncio
    import re

    def norm_name(s: str) -> str:
        return re.sub(r"[\s:：·season第季 ]+", "", str(s or "").lower())

    async def watched_series(s: MediaServer):
        """该服务器上已看剧集 → 按剧聚合（含海报/库内总集数/播放定位）"""
        if s.type == "plex":
            return []
        uid = await s._jelly_uid()
        r = await s._jelly_get(f"{s.base}/Users/{uid}/Items",
                               params={"Filters": "IsPlayed", "Recursive": "true",
                                       "IncludeItemTypes": "Episode", "Limit": 3000,
                                       "Fields": "SeriesName,SeriesProductionYear,ParentIndexNumber,"
                                                 "IndexNumber,DatePlayed,SeriesId"},
                               timeout=60)
        if r.status_code != 200:
            return []
        eps = r.json().get("Items", [])
        groups = {}
        for e in eps:
            name = e.get("SeriesName")
            if not name:
                continue
            g = groups.setdefault(name, {
                "title": name, "year": str(e.get("SeriesProductionYear") or ""),
                "eps": 0, "last_season": 0, "last_episode": 0, "last_date": 0,
                "series_id": "", "srv_id": s.id,
            })
            g["eps"] += 1
            se = int(e.get("ParentIndexNumber") or 0)
            ep = int(e.get("IndexNumber") or 0)
            if (se, ep) > (g["last_season"], g["last_episode"]):
                g["last_season"], g["last_episode"] = se, ep
            if e.get("SeriesId") and not g["series_id"]:
                g["series_id"] = e["SeriesId"]
            d = _parse_emby_date(e.get("DatePlayed") or "")
            if d and d > g["last_date"]:
                g["last_date"] = d
        # 库内总集数 + 更新到第几集（该下载了多少集）
        for g in groups.values():
            if not g["series_id"]:
                continue
            try:
                rr = await s._jelly_get(f"{s.base}/Users/{uid}/Items",
                                        params={"ParentId": g["series_id"], "Recursive": "true",
                                                "IncludeItemTypes": "Episode", "Limit": 0},
                                        timeout=60)
                if rr.status_code == 200:
                    t = rr.json().get("TotalRecordCount") or 0
                    g["lib_total"] = t or None
                # 最新一集（按季/集倒序取第一条）
                rr2 = await s._jelly_get(f"{s.base}/Users/{uid}/Items",
                                         params={"ParentId": g["series_id"], "Recursive": "true",
                                                 "IncludeItemTypes": "Episode", "Limit": 1,
                                                 "SortBy": "ParentIndexNumber,IndexNumber",
                                                 "SortOrder": "Descending",
                                                 "Fields": "ParentIndexNumber,IndexNumber"},
                                         timeout=60)
                if rr2.status_code == 200:
                    it = (rr2.json().get("Items") or [None])[0]
                    if it:
                        g["lib_latest_season"] = int(it.get("ParentIndexNumber") or 0)
                        g["lib_latest_episode"] = int(it.get("IndexNumber") or 0)
            except Exception:  # noqa
                pass
        return list(groups.values())

    servers = media_servers()
    results = await asyncio.gather(*[watched_series(s) for s in servers],
                                   return_exceptions=True)
    series = {}
    for r in results:
        if isinstance(r, list):
            for g in r:
                cur = series.get(g["title"])
                if not cur or g["eps"] > cur["eps"]:
                    series[g["title"]] = {**cur, **g} if cur else dict(g)
    # MP 订阅合并
    subs = {}
    try:
        for sub in await mp.list_subscriptions():
            subs.setdefault(norm_name(sub.get("name")), []).append(sub)
    except Exception:  # noqa
        pass
    out = []
    for g in series.values():
        match = subs.get(norm_name(g["title"]))
        sub_info = None
        if match:
            sub = match[0]
            sub_info = {"season": sub.get("season"), "state": sub.get("state"),
                        "total_episode": sub.get("total_episode"),
                        "start_episode": sub.get("start_episode"),
                        "poster": sub.get("poster") or ""}
        # 海报：优先媒体服务器剧海报（走代理），回退 MP 订阅海报
        poster = f"/api/ms/img/{g['srv_id']}/{g['series_id']}" if g.get("series_id") else ""
        if sub_info and sub_info.get("poster") and sub_info["poster"].startswith("http"):
            poster = poster or sub_info["poster"]
        g["poster"] = poster
        g["sub"] = sub_info
        g["days_ago"] = max(0, (datetime.now() - datetime.fromtimestamp(
            g["last_date"])).days) if g.get("last_date") else None
        out.append(g)
    # 服务器没记播放日期时，用本地记录的看完时间兜底
    conn = db()
    recs = {r["title"]: r for r in conn.execute(
        "SELECT title, finished_at, updated_at FROM records WHERE status='done'").fetchall()}
    conn.close()
    for g in out:
        if not g.get("days_ago") and g["days_ago"] != 0:
            rec = recs.get(g["title"])
            if rec:
                ts = rec["finished_at"] or rec["updated_at"]
                if ts:
                    g["days_ago"] = max(0, (datetime.now() - datetime.fromtimestamp(ts)).days)
    out.sort(key=lambda x: -x["last_date"])
    # 播放深链（每部剧一个）
    s_by_id = {s.id: s for s in servers}

    async def _with_play(g: dict) -> dict:
        s = s_by_id.get(g.get("srv_id"))
        if s and g.get("series_id"):
            g["play_url"] = await s.play_url(g["series_id"]) or None
        return g

    out = await asyncio.gather(*[_with_play(g) for g in out[:30]])
    return {"ok": True, "series": list(out)}


_IMG_ALLOW = ("doubanio.com", "douban.com", "themoviedb.org", "media-amazon.com",
              "sl.lostmedia.pro", "gtimg.cn")


@app.get("/api/img/proxy")
async def img_proxy(url: str):
    """外链图片代理（年报长图 Canvas 需要同源图片）"""
    from urllib.parse import urlparse
    host = urlparse(url).hostname or ""
    if not any(host == d or host.endswith("." + d) for d in _IMG_ALLOW):
        raise HTTPException(400, "不支持的图片域名")
    async with httpx.AsyncClient(timeout=20, follow_redirects=True,
                                 headers={"User-Agent": "Mozilla/5.0 MediaHub/1.0",
                                          "Referer": "https://movie.douban.com/"}) as c:
        r = await c.get(url)
        if r.status_code != 200:
            raise HTTPException(404, "图片获取失败")
        return Response(content=r.content,
                        media_type=r.headers.get("content-type", "image/jpeg"),
                        headers={"Cache-Control": "public, max-age=604800"})


# ---------------------------------------------------------------- 有声书（Leelaa Reader）
class Leelaa:
    """Leelaa Reader 第三方 API 客户端（token 缓存 1 小时，401 自动重登）"""

    def __init__(self):
        self._tok = ""
        self._tok_at = 0.0
        self._server_id = ""
        self._user_id = ""

    def _base(self) -> str:
        url = (load_config().get("leelaa_url") or "").strip().rstrip("/")
        if not url:
            raise HTTPException(400, "请先在设置里配置 Leelaa 服务地址")
        return url

    @staticmethod
    def _client() -> httpx.AsyncClient:
        return httpx.AsyncClient(trust_env=False, timeout=20)

    @staticmethod
    def _unwrap(j):
        """响应统一是 {code, data}；有 data 就取 data，否则原样返回"""
        if isinstance(j, dict) and isinstance(j.get("data"), (dict, list)):
            return j["data"]
        return j if isinstance(j, (dict, list)) else {}

    async def _login(self) -> str:
        cfg = load_config()
        base = self._base()
        user = cfg.get("leelaa_user") or "admin"
        pwd = cfg.get("leelaa_pass") or ""
        if not pwd:
            raise HTTPException(400, "请先在设置里配置 Leelaa 账号密码")
        async with self._client() as c:
            r = await c.post(f"{base}/api/v1/auth/login",
                             json={"username": user, "password": pwd})
        if r.status_code != 200:
            raise HTTPException(502, f"Leelaa 登录失败（{r.status_code}）：{r.text[:100]}")
        d = self._unwrap(r.json())
        tok = d.get("token") or d.get("authToken") or ""
        if not tok:
            raise HTTPException(502, "Leelaa 登录失败：未返回 token（账号密码可能不对）")
        self._tok = tok
        self._tok_at = time.time()
        self._server_id = str(d.get("serverId") or "")
        self._user_id = str((d.get("user") or {}).get("id") or "")
        return tok

    async def _auth(self) -> str:
        if self._tok and time.time() - self._tok_at < 3600:
            return self._tok
        return await self._login()

    async def req(self, method: str, path: str, *, json_body=None,
                  params: dict = None) -> dict:
        """带自动重登的请求；返回 data 部分。401 自动重试一次。"""
        base = self._base()
        for attempt in (1, 2):
            tok = await self._auth()
            async with self._client() as c:
                r = await c.request(method, f"{base}/api/v1{path}",
                                    headers={"Authorization": f"Bearer {tok}"},
                                    json=json_body, params=params)
            if r.status_code == 401 and attempt == 1:
                self._tok = ""
                continue
            if r.status_code == 403:
                raise HTTPException(502, "Leelaa 授权失效（license_required），去激活一下")
            if r.status_code >= 400:
                raise HTTPException(502, f"Leelaa 接口错误（{r.status_code}）：{r.text[:100]}")
            return self._unwrap(r.json())
        raise HTTPException(502, "Leelaa 登录态失效")

    # ---- 业务封装 ----
    async def search_books(self, kw: str) -> list:
        d = await self.req("POST", "/books/search",
                           json_body={"search": kw, "page": 1, "limit": 20})
        items = d.get("items") if isinstance(d, dict) else d
        if items is None and isinstance(d, dict):
            items = d.get("books")
        return items or []

    async def chapters(self, book_id) -> list:
        d = await self.req("GET", f"/books/{book_id}/chapters")
        if isinstance(d, list):
            return d
        return d.get("chapters") or []

    async def progress(self, book_id) -> dict:
        d = await self.req("GET", f"/auth/reading-progress/{book_id}")
        return d if isinstance(d, dict) else {}

    async def history(self) -> list:
        d = await self.req("GET", "/auth/reading-history")
        items = d.get("items") if isinstance(d, dict) else d
        return items or []

    async def report_progress(self, book_id, chapter_id, last_position: float,
                              duration: float = 0.0) -> bool:
        """把音箱的播放位置写回 Leelaa（best-effort）。"""
        try:
            prog = (last_position / duration) if duration else 0.0
            headers = {"X-Leelaa-Expected-Server-Id": self._server_id,
                       "X-Leelaa-Expected-User-Id": self._user_id}
            base = self._base()
            tok = await self._auth()
            async with self._client() as c:
                r = await c.post(f"{base}/api/v1/auth/reading-progress/{book_id}",
                                 headers={**headers, "Authorization": f"Bearer {tok}"},
                                 json={"chapterId": int(chapter_id),
                                       "progress": round(prog, 4),
                                       "lastPosition": round(last_position, 1),
                                       "playedDuration": round(last_position, 1)})
            return r.status_code == 200
        except Exception:  # noqa
            return False

    def play_url(self, chapter_id, tok: str = "") -> str:
        return (f"{self._base()}/api/v1/chapters/{chapter_id}/play"
                f"?raw=1&access_token={tok or self._tok}")


leelaa = Leelaa()

# 本地有声书库（默认模式）：直接扫 NAS 目录，不经过 Leelaa
BOOK_PROGRESS = ProgressStore(DATA_DIR / "book_progress.json")
local_books = LocalBooks(
    lambda: Path(load_config().get("books_dir") or "/audiobooks"),
    BOOK_PROGRESS,
    lambda: load_config().get("books_base_url") or "",
)


def books_local_mode() -> bool:
    """有声书数据源：local（默认）或 leelaa"""
    return (load_config().get("books_mode") or "local").strip() != "leelaa"


def book_src():
    return local_books if books_local_mode() else leelaa

_BOOK_HINT = ("有声书", "听书", "评书", "广播剧", "小说")
_BOOK_STOP = ("不听了", "别听了", "停止播放", "停止", "暂停", "别放了", "不播了")


def is_book_text(text: str) -> bool:
    t = str(text or "")
    return any(k in t for k in _BOOK_HINT) or t.strip().startswith(
        ("继续听", "继续播", "听一下", "我想听", "我要听"))


def is_book_stop(text: str) -> bool:
    return any(k in str(text or "") for k in _BOOK_STOP)


def parse_book_name(text: str) -> str:
    t = re.sub(r"[\s，。！!？?\.,!?]", "", str(text or ""))
    for kw in ("帮我播放", "帮我听一下", "帮我听", "帮我继续听", "继续播放",
               "继续听", "播放一下", "播放", "听一下", "我想听", "我要听",
               "我想听一下", "听", "帮我找", "帮我", "来一段", "来一集"):
        if t.startswith(kw):
            t = t[len(kw):]
            break
    for kw in ("有声书", "广播剧", "评书", "听书版", "听书", "小说"):
        t = t.replace(kw, "")
    for suffix in ("呗", "吧", "哦", "了", "行吗", "可以吗", "好不好"):
        if t.endswith(suffix):
            t = t[:-len(suffix)]
    return t.strip()


def _chapter_no_of(text: str) -> Optional[int]:
    """「三体第12章」→ 12；「第三集」→ 3；「第一百二十回」→ 120"""
    m = re.search(r"第([一二三四五六七八九十百零两\d]+)[章集回讲]", str(text or ""))
    if not m:
        return None
    v = m.group(1)
    if v.isdigit():
        return int(v)
    cn = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
          "六": 6, "七": 7, "八": 8, "九": 9}
    total, num = 0, 0
    for ch in v:
        if ch in cn:
            num = cn[ch]
        elif ch == "十":
            total += (num or 1) * 10
            num = 0
        elif ch == "百":
            total += (num or 1) * 100
            num = 0
    return total + num


def _pick_book(results: list, name: str) -> Optional[dict]:
    n = _norm_cmp(name)
    if not n or not results:
        return results[0] if results else None
    hits = [b for b in results if n in _norm_cmp(b.get("title") or "")]
    if not hits:
        hits = [b for b in results
                if any(w and w in _norm_cmp(b.get("title") or "")
                       for w in n.split())]
    if not hits:
        return None
    hits.sort(key=lambda b: len(_norm_cmp(b.get("title") or "")))
    return hits[0]


def _speaker_did(cfg: dict) -> str:
    return str(cfg.get("xiaomi_did") or "")


async def _mina():
    from miservice import MiAccount, MiNAService
    cfg = load_config()
    user = cfg.get("xiaomi_user") or ""
    if not user:
        raise HTTPException(400, "小爱账号还没配置")
    account = MiAccount(None, user, cfg.get("xiaomi_pass") or "",
                        str(DATA_DIR / "mi.token"))
    mina = MiNAService(account)
    did = _speaker_did(cfg)
    if not did:
        devices = await mina.device_list()
        lst = (devices.get("deviceList") if isinstance(devices, dict) else devices) or []
        if not lst:
            raise HTTPException(400, "小米账号下未找到音箱设备")
        did = str(lst[0].get("deviceID") or lst[0].get("deviceId") or "")
    return mina, did


def _offset_of(status: dict) -> Optional[float]:
    """从小爱播放状态里猜当前播放位置（秒）；解析不了返回 None"""
    try:
        d = status.get("data") or {}
        s = d.get("status") if isinstance(d.get("status"), dict) else d
        detail = s.get("play_song_detail") or s.get("playSongDetail") or {}
        for key in ("offset", "play_offset", "position"):
            v = detail.get(key)
            if v is None and isinstance(s, dict):
                v = s.get(key)
            if isinstance(v, (int, float)) and v >= 0:
                return float(v) / 1000.0 if float(v) > 10000 else float(v)
    except Exception:  # noqa
        pass
    return None


async def _book_progress_reporter(book, chapter, did: str, url: str):
    """播放期间每 30 秒读一次音箱状态，把位置写回 Leelaa（最多 3 小时）。"""
    book_id = book.get("id")
    ch_id = chapter.get("id")
    duration = float(chapter.get("duration") or 0)
    for _ in range(360):  # 30s × 360 = 3h
        await asyncio.sleep(30)
        try:
            mina, d2 = await _mina()
            st = await mina.player_get_status(d2)
            off = _offset_of(st if isinstance(st, dict) else {})
            if off is None:
                return
            await book_src().report_progress(book_id, ch_id, off, duration)
        except Exception:  # noqa
            return


async def _delayed_play(url: str, delay: float):
    """等 migpt 把确认语念完再推音频，避免打断。"""
    await asyncio.sleep(delay)
    try:
        mina, did = await _mina()
        await mina.play_by_url(did, url)
        cfg = load_config()
        book = _CURRENT_PLAY.get("book") or {}
        chapter = _CURRENT_PLAY.get("chapter") or {}
        if book and chapter:
            asyncio.create_task(_book_progress_reporter(book, chapter, did, url))
    except Exception as e:  # noqa
        logging.warning("有声书播放推送失败: %s", e)


_CURRENT_PLAY: dict = {}

# ---------------------------------------------------------------- 语音订阅（小爱 → migpt → 这里）
_VOICE_STRIP = ["帮我订阅", "帮我下载", "帮我下", "帮我找", "帮我搜", "帮我加",
                "帮我取消订阅", "帮我取消下载", "取消订阅", "取消下载", "退订",
                "帮我取消", "取消", "订阅一下", "下载一下", "我想看", "我要看",
                "找一下", "搜一下", "订阅", "下载", "看一下", "播放"]


def is_cancel_text(text: str) -> bool:
    return any(k in str(text or "") for k in ("取消", "退订", "不要了", "不订了"))


def parse_voice_name(text: str) -> str:
    """「帮我订阅沙丘2」→「沙丘2」"""
    t = re.sub(r"[\s，。！!？?\.,!?]", "", str(text or ""))
    for kw in _VOICE_STRIP:
        if t.startswith(kw):
            t = t[len(kw):]
            break
    for kw in _VOICE_STRIP:                       # 兜底：关键词在句中
        if kw in t:
            t = t.replace(kw, "", 1)
            break
    for suffix in ("呗", "吧", "哦", "了", "行吗", "可以吗", "好不好"):
        if t.endswith(suffix):
            t = t[:-len(suffix)]
    return t.strip()


def _season_of(text: str) -> Optional[int]:
    m = re.search(r"第([一二三四五六七八九十\d]+)季", str(text or ""))
    if not m:
        m = re.search(r"[Ss](\d{1,2})\b", str(text or ""))
    if not m:
        return None
    v = m.group(1)
    cn = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
          "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
    return int(v) if v.isdigit() else cn.get(v)


def _norm_cmp(s: str) -> str:
    return re.sub(r"[\s\-_:：·.。,，()（）\[\]【】']", "", str(s or "")).lower()


def _pick_media(results: list, name: str) -> Optional[dict]:
    """从 MP 搜索结果里挑最像的一个：标题包含片名优先，标题更短（更精确）优先"""
    n = _norm_cmp(name)
    if not n or not results:
        return results[0] if results else None
    hits = [m for m in results if n in _norm_cmp(m.get("title") or "")]
    if not hits:
        return None
    hits.sort(key=lambda m: len(_norm_cmp(m.get("title") or "")))
    return hits[0]


@app.post("/api/hook/mpdl")
async def mpdl_hook(request: Request, k: str = ""):
    """语音/快捷指令入口：{"text": "帮我订阅沙丘2"} → MoviePilot 订阅 → 小爱确认"""
    info = hook_info()
    if info["key"] and k != info["key"]:
        raise HTTPException(403, "hook 密钥不正确")
    try:
        data = await request.json()
    except Exception:  # noqa
        data = {}
    if not isinstance(data, dict):
        data = {}
    text = str(data.get("text") or data.get("name") or "").strip()
    # ================= 有声书分支 =================
    if is_book_text(text) or is_book_stop(text):
        return await _book_voice(text)
    name = parse_voice_name(text)
    if not name:
        return {"ok": False, "speak": "没听清片名，再说一次吧", "name": ""}
    cancel = is_cancel_text(text)
    # ---------------- 取消订阅 ----------------
    if cancel:
        try:
            subs = await mp.list_subscriptions()
        except Exception as e:  # noqa
            return {"ok": False, "speak": "MoviePilot 连接失败，检查一下服务吧", "error": str(e)[:100]}
        n = _norm_cmp(name)
        hit = None
        for s in subs:
            if n and (n in _norm_cmp(s.get("name") or "") or _norm_cmp(s.get("name") or "") in n):
                hit = s
                break
        if not hit:
            return {"ok": False, "speak": f"《{name}》本来就没有订阅哦", "name": name}
        try:
            await mp.unsubscribe(hit.get("id"))
        except Exception as e:  # noqa
            return {"ok": False, "speak": f"取消《{hit.get('name')}》订阅失败了", "error": str(e)[:100]}
        speak = f"好的，已取消订阅《{hit.get('name')}》"
        asyncio.create_task(_say_safe(speak))
        return {"ok": True, "action": "unsubscribe", "name": hit.get("name"),
                "speak": speak}
    # ---------------- 新增订阅 ----------------
    try:
        results = await mp.search(name, "全部")
    except Exception as e:  # noqa
        return {"ok": False, "speak": "MoviePilot 连接失败，检查一下服务吧", "error": str(e)[:100]}
    pick = _pick_media(results, name)
    if not pick:
        return {"ok": False, "speak": f"没有找到《{name}》，换个名字试试", "name": name}
    season = _season_of(text) or _season_of(pick.get("title"))
    try:
        await mp.subscribe({"title": pick.get("title"), "media_type": pick.get("media_type"),
                            "year": pick.get("year"), "tmdb_id": pick.get("tmdb_id"),
                            "douban_id": pick.get("douban_id"),
                            "media_source": pick.get("media_source") or "",
                            "media_id": pick.get("media_id") or ""}, season)
    except Exception as e:  # noqa
        return {"ok": False, "speak": f"订阅《{pick.get('title')}》失败了：{str(e)[:60]}",
                "name": pick.get("title")}
    speak = f"好的，已为你订阅《{pick.get('title')}》，下载完成后我会告诉你"
    asyncio.create_task(_say_safe("好的，已为你订阅" + str(pick.get("title"))))
    return {"ok": True, "name": pick.get("title"), "media_type": pick.get("media_type"),
            "year": pick.get("year"), "season": season, "speak": speak,
            "candidates": [{"title": m.get("title"), "media_type": m.get("media_type"),
                            "year": m.get("year")} for m in results[:5]]}


async def _book_voice(text: str) -> dict:
    """「播放XX有声书 / 继续听 / 不听了」→ 推 Leelaa 音频到小爱音箱"""
    # ---- 停止 ----
    if is_book_stop(text):
        try:
            mina, did = await _mina()
            try:
                await mina.player_stop(did)
            except Exception as e:  # noqa
                msg = str(e)
                if "101" in msg or "not exist" in msg.lower():
                    _CURRENT_PLAY.clear()
                    return {"ok": True, "action": "stop",
                            "speak": "现在没有在播放哦"}
                raise
        except HTTPException as e:
            return {"ok": False, "speak": str(e.detail)[:80]}
        except Exception as e:  # noqa
            return {"ok": False, "speak": "音箱没有响应", "error": str(e)[:80]}
        _CURRENT_PLAY.clear()
        speak = "好的，已停止播放"
        return {"ok": True, "action": "stop", "speak": speak}
    # ---- 解析书名 / 章节 ----
    name = parse_book_name(text)
    ch_no = _chapter_no_of(text)
    if not name:
        # 「继续听」→ 取最近在听的书
        try:
            hist = await book_src().history()
        except HTTPException as e:
            return {"ok": False, "speak": str(e.detail)[:80]}
        except Exception as e:  # noqa
            return {"ok": False, "speak": "有声书库读取失败，检查一下设置吧", "error": str(e)[:80]}
        if not hist:
            return {"ok": False, "speak": "你还没听过书，告诉我想听哪本吧"}
        h0 = hist[0] or {}
        b = h0.get("book") or h0.get("book_info") or {}
        name = str(b.get("title") or h0.get("book_title") or "")
        if not name:
            return {"ok": False, "speak": "没找到最近在听的书，说个书名吧"}
    # ---- 搜书 ----
    try:
        books = await book_src().search_books(name)
    except HTTPException as e:
        return {"ok": False, "speak": str(e.detail)[:80]}
    except Exception as e:  # noqa
        return {"ok": False, "speak": "有声书库读取失败，检查一下设置吧", "error": str(e)[:80]}
    book = _pick_book(books, name)
    if not book:
        return {"ok": False, "speak": f"书库里没找到《{name}》，换个名字试试", "name": name}
    book_id = book.get("id")
    # ---- 章节 + 进度 ----
    try:
        chs = await book_src().chapters(book_id)
        prog = await book_src().progress(book_id)
    except HTTPException as e:
        return {"ok": False, "speak": str(e.detail)[:80]}
    except Exception as e:  # noqa
        return {"ok": False, "speak": "章节读取失败", "error": str(e)[:80]}
    if not chs:
        return {"ok": False, "speak": f"《{book.get('title')}》还没有章节，先扫描入库吧"}
    chs.sort(key=lambda c: int(c.get("chapter_index") or 0))
    chapter = None
    resumed = False
    if ch_no:  # 用户点名了第 N 章
        for c in chs:
            if int(c.get("chapter_index") or 0) == ch_no:
                chapter = c
                break
        if chapter is None:
            return {"ok": False,
                    "speak": f"《{book.get('title')}》没有第{ch_no}章，一共{len(chs)}章"}
    else:  # 续播：Leelaa 进度优先
        pc = prog.get("chapter_id")
        if pc:
            for c in chs:
                if str(c.get("id")) == str(pc):
                    chapter = c
                    resumed = True
                    break
        if chapter is None:
            chapter = chs[0]
    if books_local_mode():
        url = local_books.stream_url(book_id, chapter.get("id"))
    else:
        tok = await leelaa._auth()
        url = leelaa.play_url(chapter.get("id"), tok)
    if books_local_mode():
        BOOK_PROGRESS.set(book_id, chapter_id=chapter.get("id"),
                          chapter_index=chapter.get("chapter_index"),
                          chapter_title=chapter.get("title") or "",
                          position=(prog.get("last_position") or 0) if resumed else 0)
    title = book.get("title") or name
    ch_title = chapter.get("title") or f"第{chapter.get('chapter_index') or 1}章"
    pos = ""
    if resumed and prog.get("last_position"):
        try:
            m = int(float(prog["last_position"]) // 60)
            pos = f"，从{m}分钟处接着讲"
        except Exception:  # noqa
            pos = ""
    speak = f"好的，为你播放《{title}》{ch_title}{pos}"
    _CURRENT_PLAY.clear()
    _CURRENT_PLAY.update({"book": book, "chapter": chapter})
    asyncio.create_task(_delayed_play(url, 9))   # 等 migpt 念完确认语
    return {"ok": True, "action": "play", "book": title, "chapter": ch_title,
            "chapter_index": chapter.get("chapter_index"), "speak": speak,
            "candidates": [{"id": b.get("id"), "title": b.get("title")}
                           for b in books[:5]]}


@app.get("/api/leelaa/status")
async def leelaa_status():
    """有声书连接状态（设置页测试用）"""
    cfg = load_config()
    if not cfg.get("leelaa_url"):
        return {"ok": False, "msg": "未配置 Leelaa 服务地址"}
    try:
        d = await leelaa.req("GET", "/books", params={"page": 1, "limit": 1})
        total = 0
        if isinstance(d, dict):
            total = d.get("total") or len(d.get("items") or [])
        elif isinstance(d, list):
            total = len(d)
        return {"ok": True, "books": total,
                "server": cfg.get("leelaa_url"),
                "user": cfg.get("leelaa_user") or "admin"}
    except HTTPException as e:
        return {"ok": False, "msg": str(e.detail)[:120]}
    except Exception as e:  # noqa
        return {"ok": False, "msg": str(e)[:120]}


def _book_brief(b: dict) -> dict:
    """把 Leelaa 书籍对象整理成前端用的精简结构"""
    bid = b.get("id")
    return {
        "id": bid,
        "title": b.get("title") or b.get("name") or "未命名",
        "author": b.get("author") or "",
        "narrator": b.get("narrator") or b.get("artist") or "",
        "desc": b.get("description") or b.get("intro") or b.get("summary") or "",
        "cover": f"/api/books/{bid}/cover" if bid else "",
        "chapters": b.get("chapter_count") or b.get("chapters_count") or b.get("total_chapters") or 0,
        "duration": b.get("duration") or b.get("total_duration") or 0,
        "progress": b.get("progress") or b.get("chapter_progress") or 0,
        "last_chapter": b.get("last_chapter_title") or b.get("last_chapter") or "",
        "category": b.get("category") or b.get("category_name") or "",
    }


def _need_leelaa():
    if not (load_config().get("leelaa_url") or "").strip():
        raise HTTPException(400, "请先在设置 → 有声书 里填 Leelaa 服务地址")


@app.get("/api/books/recent")
async def books_recent():
    """继续听：最近在听的书"""
    if books_local_mode():
        return {"items": await local_books.history()}
    _need_leelaa()
    try:
        hist = await leelaa.history()
    except HTTPException as e:
        raise HTTPException(502, str(e.detail))
    out = []
    for h in hist[:20]:
        b = h.get("book") or h.get("book_info") or {}
        bid = h.get("book_id") or b.get("id")
        out.append({
            "id": bid,
            "title": h.get("book_title") or b.get("title") or "未命名",
            "author": b.get("author") or h.get("book_author") or "",
            "cover": f"/api/books/{bid}/cover" if bid else "",
            "chapter_id": h.get("chapter_id"),
            "chapter_title": h.get("chapter_title") or "",
            "last_position": h.get("last_position") or 0,
            "progress": h.get("progress") or 0,
            "last_played_at": h.get("last_played_at") or h.get("read_at") or "",
        })
    return {"items": out}


@app.get("/api/books")
async def books_list(q: str = "", page: int = 1, limit: int = 60):
    """书库列表 / 搜索"""
    if books_local_mode():
        items = local_books.list_books(q)
        start = max(0, (page - 1) * limit)
        return {"items": items[start:start + limit], "total": len(items), "page": page}
    _need_leelaa()
    try:
        if q.strip():
            items = await leelaa.search_books(q.strip())
            total = len(items)
        else:
            d = await leelaa.req("GET", "/books", params={"page": page, "limit": limit})
            if isinstance(d, list):
                items, total = d, len(d)
            else:
                items = d.get("items") or d.get("books") or []
                total = int(d.get("total") or len(items))
    except HTTPException as e:
        raise HTTPException(502, str(e.detail))
    return {"items": [_book_brief(b) for b in items], "total": total, "page": page}


@app.get("/api/books/status")
async def books_status():
    """有声书库状态（设置页测试用）"""
    cfg = load_config()
    root = Path(cfg.get("books_dir") or "/audiobooks")
    try:
        items = local_books.list_books("")
        n_book = len(items)
        n_ch = sum(int(b.get("chapters") or 0) for b in items)
    except Exception as e:  # noqa
        return {"ok": False, "msg": f"目录读取失败：{str(e)[:120]}", "root": str(root)}
    return {"ok": True,
            "mode": "local" if books_local_mode() else "leelaa",
            "root": str(root), "exists": root.exists(),
            "books": n_book, "chapters": n_ch,
            "base_url": local_books.base_url(),
            "titles": [b.get("title") for b in items[:8]]}


@app.get("/api/books/{bid}/stream/{cid}")
async def book_stream(bid: str, cid: str):
    """把本地音频以可 Range 的方式吐出去，小爱音箱和浏览器都能直接播"""
    if not books_local_mode():
        raise HTTPException(400, "当前是 Leelaa 模式，直接用 Leelaa 的播放地址")
    ch = local_books.chapter_by_id(bid, cid)
    if not ch:
        raise HTTPException(404, "章节不存在")
    p = Path(ch["file"])
    if not p.exists():
        raise HTTPException(404, "音频文件丢失")
    mt = {".m4a": "audio/mp4", ".aac": "audio/aac", ".flac": "audio/flac",
          ".wav": "audio/wav", ".ogg": "audio/ogg", ".opus": "audio/ogg"}.get(
        p.suffix.lower(), "audio/mpeg")
    return FileResponse(p, media_type=mt, headers={"Accept-Ranges": "bytes"})


@app.get("/api/books/{bid}/cover")
async def book_cover(bid: str):
    """封面代理：Leelaa 的封面接口需要 token，浏览器 img 带不上 header"""
    if books_local_mode():
        b = local_books.get(bid)
        if not b or not b.get("cover"):
            raise HTTPException(404, "封面不存在")
        p = b["cover"]
        mt = {".png": "image/png", ".webp": "image/webp",
              ".bmp": "image/bmp"}.get(p.suffix.lower(), "image/jpeg")
        return FileResponse(p, media_type=mt,
                            headers={"Cache-Control": "public, max-age=86400"})
    _need_leelaa()
    tok = await leelaa._auth()
    base = leelaa._base()
    async with httpx.AsyncClient(trust_env=False, timeout=20) as c:
        r = await c.get(f"{base}/api/v1/books/{bid}/cover",
                        headers={"Authorization": f"Bearer {tok}"})
    if r.status_code != 200 or not r.content:
        raise HTTPException(404, "封面不存在")
    return Response(content=r.content,
                    media_type=r.headers.get("content-type") or "image/jpeg",
                    headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/books/{bid}")
async def book_detail(bid: str):
    """书籍详情 + 章节 + 播放进度"""
    if books_local_mode():
        b = local_books.get(bid)
        if not b:
            raise HTTPException(404, "书不存在")
        prog = BOOK_PROGRESS.get(b["id"])
        return {
            "book": local_books._brief(b, prog),
            "chapters": local_books.chapters_of(b["id"]),
            "progress": {"chapter_id": prog.get("chapter_id"),
                         "chapter_index": prog.get("chapter_index"),
                         "last_position": prog.get("last_position") or 0,
                         "progress": (prog.get("chapter_index") or 0) / max(1, b["chapter_count"])},
        }
    _need_leelaa()
    try:
        d = await leelaa.req("GET", f"/books/{bid}")
        chs = await leelaa.chapters(bid)
        prog = await leelaa.progress(bid)
    except HTTPException as e:
        raise HTTPException(502, str(e.detail))
    book = d.get("book") if isinstance(d, dict) and d.get("book") else d
    chs.sort(key=lambda c: int(c.get("chapter_index") or 0))
    return {
        "book": _book_brief(book if isinstance(book, dict) else {}),
        "chapters": [{"id": c.get("id"),
                      "index": c.get("chapter_index") or i + 1,
                      "title": c.get("title") or f"第{c.get('chapter_index') or i + 1}章",
                      "duration": c.get("duration") or 0} for i, c in enumerate(chs)],
        "progress": {"chapter_id": prog.get("chapter_id"),
                     "chapter_index": prog.get("chapter_index"),
                     "last_position": prog.get("last_position") or 0,
                     "progress": prog.get("progress") or 0},
    }


class BookPlayModel(BaseModel):
    book_id: int
    chapter_id: int


@app.post("/api/books/play")
async def books_play(m: BookPlayModel):
    """网页上点章节 → 推送到小爱音箱播放"""
    if books_local_mode():
        b = local_books.get(m.book_id)
        if not b:
            raise HTTPException(404, "书不存在")
        ch = local_books.chapter_by_id(m.book_id, m.chapter_id)
        if not ch:
            raise HTTPException(404, "章节不存在")
        url = local_books.stream_url(m.book_id, ch["id"])
        BOOK_PROGRESS.set(m.book_id, chapter_id=ch["id"],
                          chapter_index=ch["chapter_index"],
                          chapter_title=ch["title"], position=0)
        _CURRENT_PLAY.clear()
        _CURRENT_PLAY.update({"book": {"id": b["id"], "title": b["title"]}, "chapter": ch})
        asyncio.create_task(_delayed_play(url, 1.5))
        return {"ok": True, "title": b["title"], "chapter": ch["title"], "url": url}
    _need_leelaa()
    try:
        tok = await leelaa._auth()
        chs = await leelaa.chapters(m.book_id)
        d = await leelaa.req("GET", f"/books/{m.book_id}")
    except HTTPException as e:
        raise HTTPException(502, str(e.detail))
    chapter = next((c for c in chs if str(c.get("id")) == str(m.chapter_id)), None)
    if not chapter:
        raise HTTPException(404, "章节不存在")
    book = (d.get("book") if isinstance(d, dict) and d.get("book") else d) or {}
    url = leelaa.play_url(m.chapter_id, tok)
    _CURRENT_PLAY.clear()
    _CURRENT_PLAY.update({"book": book, "chapter": chapter})
    asyncio.create_task(_delayed_play(url, 1.5))
    return {"ok": True, "title": book.get("title"),
            "chapter": chapter.get("title") or f"第{chapter.get('chapter_index')}章"}


@app.get("/api/mpdl/status")
async def mpdl_status():
    """当前下载任务（设置页/调试用）"""
    try:
        dl = await mp.download_list()
    except Exception as e:  # noqa
        raise HTTPException(502, f"MoviePilot 下载列表获取失败：{str(e)[:120]}")
    out = [{"hash": d.get("hash"), "name": d.get("name"), "title": d.get("title"),
            "season": d.get("season_episode"), "progress": round(float(d.get("progress") or 0) * 100, 1),
            "state": d.get("state"), "speed": d.get("dlspeed"), "size": d.get("size"),
            "downloader": d.get("downloader")} for d in dl if isinstance(d, dict)]
    return {"ok": True, "tts": tts_on(), "downloading": out}


@app.get("/api/tts/devices")
async def tts_devices():
    """列出小米账号下的小爱音箱，供设置页下拉选择"""
    cfg = load_config()
    if not cfg.get("xiaomi_user"):
        raise HTTPException(400, "请先保存小米账号（User ID）")
    from miservice import MiAccount, MiNAService
    account = MiAccount(None, cfg["xiaomi_user"], cfg["xiaomi_pass"], str(DATA_DIR / "mi.token"))
    mina = MiNAService(account)
    devices = await mina.device_list()
    lst = []
    if isinstance(devices, dict):
        lst = devices.get("deviceList") or []
    elif isinstance(devices, list):
        lst = devices
    out = [{"deviceID": d.get("deviceID") or d.get("deviceId"),
            "name": d.get("name") or d.get("alias") or "",
            "alias": d.get("alias") or "",
            "model": d.get("model") or "",
            "current": bool(d.get("current"))} for d in lst if isinstance(d, dict)]
    return {"ok": True, "devices": out}


@app.post("/api/tts/test")
async def tts_test(request: Request):
    """设置页「试播」按钮"""
    try:
        data = await request.json()
    except Exception:  # noqa
        data = {}
    text = str((data or {}).get("text") or "你好，我是MediaHub，测试播报成功").strip()[:100]
    ok = await say(text)
    return {"ok": bool(ok), "text": text}


@app.get("/")
def index():
    return FileResponse(WEB_DIR / "index.html")


@app.get("/sw.js")
def service_worker():
    """Service Worker 必须从根路径提供，否则 scope 只能是 /static/"""
    return FileResponse(WEB_DIR / "sw.js", media_type="application/javascript")


app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8899)
