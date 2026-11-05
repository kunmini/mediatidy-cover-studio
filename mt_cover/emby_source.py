# -*- coding: utf-8 -*-
"""Emby 交互模块。

两条通道：
  1. 读（库列表 / 条目列表）—— 走 MediaTidy 的 SDK `emby.request`，
     宿主自动鉴权，插件无需持有 API Key 也能读。
  2. 读写图片 —— SDK 只支持 JSON/UTF-8 文本响应，拿不到图片二进制，
     所以用标准库 urllib 直连 Emby，需要用户在插件配置里填地址 + API Key。

⚠️ 关于写图的关键坑（已实测确认）：
   Emby 的 POST /Items/{id}/Images/Primary 接收的是 **base64 字符串**，
   不是二进制流。Emby 的 OpenAPI 写的是 "binary format"，那是错的。
   直接发二进制会得到 HTTP 204 但完全不落盘。
"""
from __future__ import annotations

import base64
import json
import logging
import urllib.parse
import urllib.request
from typing import Any, Callable

logger = logging.getLogger("media.cover-studio")

# 媒体库类型 → Emby 条目类型
ITEM_TYPE_BY_COLLECTION = {
    "movies": "Movie",
    "tvshows": "Series",
    "music": "MusicAlbum",
    "musicvideos": "MusicVideo",
    "books": "Book,AudioBook",
    "photos": "Photo,PhotoAlbum",
    "homevideos": "Video",
    "boxsets": "BoxSet",
}

# 媒体库类型 → 封面英文副标题
SUB_BY_COLLECTION = {
    "movies": "MOVIES",
    "tvshows": "TV SHOWS",
    "boxsets": "BOXSET",
    "music": "MUSIC",
    "musicvideos": "MUSIC VIDEOS",
    "homevideos": "VIDEOS",
}


def _unwrap(res):
    """SDK 返回形如 {"status":200,"data":{...}}，拆一层。"""
    if isinstance(res, dict) and "data" in res and (
            "status" in res or set(res) <= {"status", "data", "headers"}):
        return res.get("data")
    return res


# ── 直连 Emby（图片读写）──────────────────────────────────────
class EmbyDirect:
    """插件进程内直连 Emby 的轻量客户端。"""

    def __init__(self, base_url: str, api_key: str, timeout: int = 25):
        self.base = (base_url or "").rstrip("/")
        self.key = (api_key or "").strip()
        self.timeout = timeout

    @property
    def ok(self) -> bool:
        return bool(self.base and self.key)

    def _get(self, path: str, params: dict | None = None, raw: bool = False):
        if not self.ok:
            raise RuntimeError("Emby 直连未配置（缺少地址或 API Key）")
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={
            "X-Emby-Token": self.key, "Accept": "*/*"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            body = r.read()
            return body if raw else json.loads(body.decode("utf-8", "replace"))

    def image(self, item_id: str, max_width: int = 1000) -> bytes | None:
        """下载条目主封面。"""
        try:
            return self._get(f"/Items/{item_id}/Images/Primary",
                             {"MaxWidth": max_width, "Quality": 90}, raw=True)
        except Exception as e:
            logger.warning("下载海报失败 %s: %s", item_id, e)
            return None

    def upload_image(self, item_id: str, img: bytes) -> bool:
        """把封面写回 Emby 的 Primary 图。

        ⚠️ body 必须是 base64 字符串，不是二进制。详见模块 docstring。
        """
        mime = self._image_mime(img)
        if not mime:
            logger.error("封面图格式无效 (item %s)", item_id)
            return False
        try:
            url = f"{self.base}/Items/{item_id}/Images/Primary"
            payload = base64.b64encode(img)
            req = urllib.request.Request(url, data=payload, method="POST", headers={
                "X-Emby-Token": self.key,
                "Content-Type": mime,
            })
            with urllib.request.urlopen(req, timeout=max(self.timeout, 60)) as r:
                ok = 200 <= r.status < 300
            if ok:
                logger.info("封面已写入 Emby (item %s, %d bytes, %s)",
                            item_id, len(img), mime)
            else:
                logger.warning("写入返回 %s (item %s)", r.status, item_id)
            return ok
        except Exception as e:
            logger.error("上传封面失败 (item %s): %s", item_id, e)
            return False

    @staticmethod
    def _image_mime(data: bytes) -> str | None:
        """按文件头判断图片类型。"""
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return "image/png"
        if data[:2] == b"\xff\xd8":
            return "image/jpeg"
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return "image/webp"
        if data[:6] in (b"GIF87a", b"GIF89a"):
            return "image/gif"
        return None


# ── 经 SDK 读库与条目（JSON）──────────────────────────────────
def list_libraries(request: Callable[..., Any], server_id: str) -> list[dict]:
    """读取 Emby 所有媒体库。"""
    data = _unwrap(request(server_id=server_id, path="/Library/VirtualFolders"))
    if isinstance(data, dict):
        data = data.get("Items") or data.get("data") or []
    out = []
    for v in data or []:
        if not isinstance(v, dict):
            continue
        out.append({
            "id": str(v.get("ItemId") or v.get("Id") or ""),
            "name": v.get("Name") or "",
            "collectionType": (v.get("CollectionType") or "").lower(),
        })
    return [x for x in out if x["id"]]


def fetch_items(request: Callable[..., Any], server_id: str, library_id: str,
                collection_type: str, limit: int = 80) -> tuple[list[dict], int]:
    """取媒体库条目，返回 (条目列表, 该库总数)。

    总数用于封面上的数量角标。
    """
    itype = ITEM_TYPE_BY_COLLECTION.get(collection_type, "Movie,Series")
    data = _unwrap(request(server_id=server_id, path="/Items", query={
        "ParentId": library_id,
        "Recursive": "true",
        "IncludeItemTypes": itype,
        "Fields": "ProductionYear,ImageTags",
        "ImageTypeLimit": "1",
        "EnableImageTypes": "Primary",
        "SortBy": "SortName",
        "SortOrder": "Ascending",
        "Limit": str(limit),
        "EnableTotalRecordCount": "true",
    }))
    total = 0
    if isinstance(data, dict):
        try:
            total = int(data.get("TotalRecordCount") or 0)
        except Exception:
            total = 0
        data = data.get("Items") or []
    items = [it for it in (data or []) if isinstance(it, dict) and it.get("Id")]
    return items, (total or len(items))


def pick_poster_items(items: list[dict], count: int = 9) -> list[dict]:
    """按「作品名 + 年份」去重，挑出有海报的前 count 个。

    这是避免封面出现同一部片多次的关键。
    """
    seen: set[str] = set()
    picked: list[dict] = []
    for it in items:
        name = str(it.get("Name") or "").strip().lower()
        year = str(it.get("ProductionYear") or "")
        key = f"{name}|{year}"
        if key in seen:
            continue
        if not (it.get("ImageTags") or {}).get("Primary"):
            continue
        seen.add(key)
        picked.append(it)
        if len(picked) >= count:
            break
    return picked
