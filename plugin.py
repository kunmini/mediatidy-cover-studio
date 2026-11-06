# -*- coding: utf-8 -*-
"""媒体库封面工坊（MediaTidy 插件）。

从 Emby 各媒体库取作品海报，按「作品名 + 年份」去重后合成 1920×1080
分类封面，再写回对应媒体库。

开箱即用：填一次 Emby 地址 + API Key，之后点一下按钮就行。
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional

from mt_plugin import Context, Plugin as PluginBase, action, query

from mt_cover import emby_source
from mt_cover.emby_source import SUB_BY_COLLECTION
from mt_cover.renderer import load_fonts, render_cover

logger = logging.getLogger("media.cover-studio")

STORAGE_CONFIG = "cover_studio_config"
STORAGE_STATE = "cover_studio_state"
STORAGE_LIBS = "cover_studio_libs"


# ── 工具 ──────────────────────────────────────────────────────
def _storage_value(ctx: Context, key: str, default=None):
    """读 storage 并解包。

    ⚠️ SDK 的 storage.get 返回结构是
       {"key":..., "found":bool, "value":<真实数据>, "bytes":..., "revision":...}
    真实数据在 value 字段里，直接当数据用会拿到整个信封。
    """
    try:
        res = ctx.mt.storage.get(key=key)
    except Exception as e:
        logger.warning("storage.get(%s) 失败: %s", key, e)
        return default
    if isinstance(res, dict) and "value" in res and ("found" in res or "key" in res):
        return res.get("value")
    return res if res is not None else default


def _write_map_rows(cfg: Dict[str, Any], tmap: Dict[str, Any], max_rows: int = 30) -> None:
    """把 titleMap 拆成扁平字段 mapNKey / mapNTitle / mapNSub，供页面回显。

    ⚠️ MediaTidy UI v2 的 bind 不支持数组索引（实测 config.titleMap[0].title
    和 config.titleMap.0.title 都报「必须绑定到可写路径」），只能一个字段一行。
    """
    for i in range(max_rows):
        cfg.pop("map%dKey" % i, None)
        cfg.pop("map%dTitle" % i, None)
        cfg.pop("map%dSub" % i, None)
    for i, (k, v) in enumerate(list(tmap.items())[:max_rows]):
        if not isinstance(v, dict):
            continue
        cfg["map%dKey" % i] = k
        cfg["map%dTitle" % i] = v.get("title") or ""
        cfg["map%dSub" % i] = v.get("subtitle") or ""


def _is_empty_map(v) -> bool:
    """判断映射值是否为空。"""
    if v is None:
        return True
    if isinstance(v, str):
        t = v.strip()
        return t in ("", "{}", "null")
    if isinstance(v, dict):
        return len(v) == 0
    return False


def _as_int(v, default: int) -> int:
    """把配置值（可能是字符串）安全转成 int。"""
    try:
        n = int(str(v).strip())
        return n if 0 <= n <= 255 else default
    except Exception:
        return default


def _get_config(ctx: Context) -> Dict[str, Any]:
    """读配置：优先宿主下发的 ctx.config，再退回插件自己的 storage。"""
    try:
        raw = getattr(ctx, "config", None)
        if isinstance(raw, dict) and raw:
            return dict(raw)
        if isinstance(raw, str) and raw.strip():
            import json
            val = json.loads(raw)
            if isinstance(val, dict):
                return val
    except Exception as e:
        logger.warning("读取 ctx.config 失败: %s", e)

    try:
        val = _storage_value(ctx, STORAGE_CONFIG)
        if isinstance(val, dict) and val:
            return val
    except Exception as e:
        logger.warning("读取 storage 失败: %s", e)
    return {}


def _save_config(ctx: Context, cfg: Dict[str, Any]) -> bool:
    try:
        ctx.mt.storage.set(key=STORAGE_CONFIG, value=cfg)
        return True
    except Exception as e:
        logger.error("保存配置失败: %s", e)
        return False


def _get_state(ctx: Context) -> Dict[str, Any]:
    try:
        val = _storage_value(ctx, STORAGE_STATE)
        if isinstance(val, dict):
            return val
    except Exception:
        pass
    return {"lastRun": None, "results": [], "totalOk": 0, "totalFail": 0}


def _save_state(ctx: Context, state: Dict[str, Any]) -> None:
    try:
        ctx.mt.storage.set(key=STORAGE_STATE, value=state)
    except Exception as e:
        logger.warning("保存状态失败: %s", e)


def _emby_request(ctx: Context):
    """包装 SDK 的 emby.request。

    ⚠️ 必须用关键字参数调用：SDK 只在 kwargs 路径下做 snake_case→camelCase
    转换（server_id→serverId），传 dict 位置参数会原样透传而被宿主拒绝。
    """
    def _req(**kwargs):
        return ctx.mt.emby.request(**kwargs)
    return _req


def _resolve_server(ctx: Context, cfg: Dict[str, Any]) -> str:
    """确认可用的 Emby serverId。

    MediaTidy 的 emby.request 只认宿主自己下发的 serverId（如 4f656ee3），
    不是 Emby 侧的 Guid，所以必须先问 emby.servers.list()。
    """
    try:
        servers = ctx.mt.emby.servers.list({})
        items = servers if isinstance(servers, list) else \
            (servers or {}).get("items") or (servers or {}).get("data") or []
        ids = [str(s.get("id") or s.get("Id") or "")
               for s in items if isinstance(s, dict)]
        ids = [i for i in ids if i]
        if ids:
            want = str(cfg.get("serverId") or "").strip()
            if want in ids:
                return want
            logger.info("配置的 serverId=%s 不在宿主列表 %s，改用 %s",
                        want or "(空)", ids, ids[0])
            return ids[0]
    except Exception as e:
        logger.warning("枚举 Emby 服务器失败: %s", e)
    return str(cfg.get("serverId") or "").strip()



def _resolve_titles(cfg: Dict[str, Any], library_name: str,
                    default_en: str) -> tuple[str, str]:
    """解析封面主/副标题（对齐 ALSemby 的「封面标题」机制）。

    映射表结构（键 = Emby 媒体库原名）：
        {"外语电影": {"title": "外语电影", "subtitle": "MOVIES"}}

    规则：
      - title 留空 → 用媒体库原名
      - subtitle 留空 → 用默认英文（MOVIES / TV SHOWS ...）

    未开启 titleMapEnabled 时直接用库原名 + 默认英文。
    """
    if not cfg.get("titleMapEnabled"):
        return library_name, default_en

    tmap = cfg.get("titleMap") or {}
    # 兼容 JSON 字符串
    if isinstance(tmap, str):
        try:
            import json as _json
            tmap = _json.loads(tmap or "{}")
        except Exception:
            tmap = {}
    if not isinstance(tmap, dict):
        return library_name, default_en

    entry = tmap.get(library_name)
    if not isinstance(entry, dict):
        return library_name, default_en

    title = str(entry.get("title") or "").strip() or library_name
    subtitle = str(entry.get("subtitle") or "").strip() or default_en
    return title, subtitle


def _filter_libraries(libs: List[Dict], cfg: Dict[str, Any]) -> List[Dict]:
    """按配置过滤要处理的媒体库。"""
    skip = set(str(x) for x in (cfg.get("skipLibraries") or []))
    only = cfg.get("onlyLibraries") or []
    out = []
    for lib in libs:
        if lib["name"] in skip or lib["id"] in skip:
            continue
        if only and lib["name"] not in only and lib["id"] not in only:
            continue
        out.append(lib)
    return out


# ── 插件 ──────────────────────────────────────────────────────
class Plugin(PluginBase):
    """媒体库封面工坊。"""

    def validate_config(self, config: Dict[str, Any], ctx: Context) -> Optional[Dict[str, Any]]:
        return None

    def on_init(self, ctx: Context) -> None:
        cfg = _get_config(ctx)
        logger.info("媒体库封面工坊已初始化 (serverId=%s)",
                    cfg.get("serverId") or "自动")

    def on_stop(self, ctx: Context) -> None:
        logger.info("媒体库封面工坊已停止")

    def get_page(self, ctx: Context):
        """工作台页面：一键生成。"""
        try:
            from ui_page import build_page

            # 注意：page 上下文不允许调用 emby.request，
            # 所以库列表只能由 generate 动作写入 storage 后读回。
            cfg = dict(_get_config(ctx))
            # storage 里的配置优先补进来（generate 会写库列表）
            try:
                staged = _storage_value(ctx, STORAGE_CONFIG)
                if isinstance(staged, dict):
                    merged_cfg = {**staged, **cfg}
                    if staged.get("mapRowCount"):
                        merged_cfg["mapRowCount"] = staged["mapRowCount"]
                    for k, v in staged.items():
                        if k.startswith("map"):
                            merged_cfg[k] = v
                    cfg = merged_cfg
                if isinstance(staged, dict) and staged.get("titleMap") and \
                        not cfg.get("map0Key"):
                    cur = cfg.get("titleMap")
                    if not cur or (isinstance(cur, (dict, str)) and not cur):
                        cfg["titleMap"] = staged["titleMap"]
                        cfg.setdefault("titleMapEnabled", True)
                        logger.info("get_page 使用 storage 中的标题映射 (%d 条)",
                                    len(staged["titleMap"]))
            except Exception as e:
                logger.warning("读取暂存映射失败: %s", e)
            # ① config 里已经有 mapNKey 扁平行 → 直接用（save-config 存的就是这个）
            # ② 否则从 titleMap 拆
            try:
                if cfg.get("map0Key"):
                    logger.info("get_page 使用 config 中的扁平行回显")
                else:
                    tmap = cfg.get("titleMap")
                    if isinstance(tmap, str):
                        tmap = json.loads(tmap or "{}")
                    if isinstance(tmap, dict) and tmap:
                        for i, (k, v) in enumerate(list(tmap.items())[:30]):
                            if isinstance(v, dict):
                                cfg["map%dKey" % i] = k
                                cfg["map%dTitle" % i] = v.get("title") or ""
                                cfg["map%dSub" % i] = v.get("subtitle") or ""
            except Exception as e:
                logger.warning("拆分映射到行失败: %s", e)
            try:
                libs = _storage_value(ctx, STORAGE_LIBS)
                logger.info("get_page 读到库缓存: type=%s len=%s",
                            type(libs).__name__, len(libs) if hasattr(libs, "__len__") else "?")
                if isinstance(libs, list) and libs:
                    cfg["_libraries"] = libs
            except Exception as e:
                logger.warning("读取库列表缓存失败: %s", e)
            return build_page(ctx, cfg_override=cfg)
        except Exception as e:
            logger.error("构建页面失败: %s", e)
            from mt_plugin.ui import page, component
            return page(component("Text", text="页面加载失败：%s" % e))

    # ── 查询 ──────────────────────────────────────────────────
    @query("status")
    def status(self, input: Dict[str, Any], ctx: Context) -> Dict[str, Any]:
        cfg = _get_config(ctx)
        state = _get_state(ctx)
        servers: List[Dict[str, Any]] = []
        try:
            raw = ctx.mt.emby.servers.list({})
            items = raw if isinstance(raw, list) else \
                (raw or {}).get("items") or (raw or {}).get("data") or []
            servers = [{"id": str(s.get("id") or s.get("Id") or ""),
                        "name": s.get("name") or s.get("Name") or ""}
                       for s in items]
        except Exception as e:
            logger.warning("读取服务器列表失败: %s", e)

        libs: List[Dict[str, Any]] = []
        sid = str(cfg.get("serverId") or (servers[0]["id"] if servers else ""))
        if sid:
            try:
                libs = emby_source.list_libraries(_emby_request(ctx), sid)
            except Exception as e:
                logger.warning("读取媒体库失败: %s", e)

        return {"config": cfg, "servers": servers, "libraries": libs, "state": state}

    @query("libraries")
    def libraries(self, input: Dict[str, Any], ctx: Context) -> Dict[str, Any]:
        cfg = _get_config(ctx)
        sid = _resolve_server(ctx, cfg)
        if not sid:
            return {"libraries": [], "error": "未找到可用的 Emby 服务器"}
        try:
            return {"libraries": emby_source.list_libraries(_emby_request(ctx), sid)}
        except Exception as e:
            return {"libraries": [], "error": str(e)}

    # ── 动作 ──────────────────────────────────────────────────
    @action("save-config")
    def save_config(self, input: Dict[str, Any], ctx: Context) -> Dict[str, Any]:
        cfg = _get_config(ctx)

        # 先从插件暂存里取之前「读取媒体库」生成的结果，避免用空值覆盖
        staged: Dict[str, Any] = {}
        try:
            val = _storage_value(ctx, STORAGE_CONFIG)
            if isinstance(val, dict):
                staged = val
        except Exception:
            pass

        for key in ("embyUrl", "embyApiKey", "serverId", "style",
                    "badgeEnabled", "badgeAlpha", "badgeRandomColor", "skipLibraries", "onlyLibraries",
                    "titleMapEnabled", "titleMap", "mapRowCount"):
            if key not in input or input[key] is None:
                continue
            v = input[key]
            if key == "titleMap":
                # Textarea 传来的是 JSON 字符串，转成 dict 存
                if isinstance(v, str):
                    if _is_empty_map(v):
                        if staged.get("titleMap"):
                            v = staged["titleMap"]
                        else:
                            continue
                    else:
                        try:
                            v = json.loads(v)
                        except Exception as e:
                            return {"ok": False, "error": "映射表 JSON 格式错误: %s" % e}
                elif _is_empty_map(v) and staged.get("titleMap"):
                    v = staged["titleMap"]
            cfg[key] = v

        # 扁平行（mapNKey / mapNTitle / mapNSub）→ titleMap
        # 值可能来自 input（动作入参）也可能来自 state（页面绑定），两处都读
        def _pick(name: str):
            if name in input:
                return input.get(name)
            return cfg.get(name)

        rows: Dict[str, Any] = {}
        for i in range(30):
            k = str(_pick("map%dKey" % i) or "").strip()
            if not k:
                continue
            rows[k] = {
                "title": str(_pick("map%dTitle" % i) or "").strip(),
                "subtitle": str(_pick("map%dSub" % i) or "").strip(),
            }
        if rows:
            cfg["titleMap"] = rows
            # 保留扁平行本身，便于页面回显
        for i in range(30):
            for suf in ("Key", "Title", "Sub"):
                key = "map%d%s" % (i, suf)
                v = _pick(key)
                if v is not None:
                    cfg[key] = v
        

        _save_config(ctx, cfg)
        return {"ok": True, "config": cfg}

    @action("generate")
    def generate(self, input: Dict[str, Any], ctx: Context) -> Dict[str, Any]:
        """为指定（或全部）媒体库生成封面并写回 Emby。"""
        cfg = _get_config(ctx)
        sid = _resolve_server(ctx, cfg)
        if not sid:
            return {"ok": False, "error": "未找到 Emby 服务器，请先在设置里选择"}

        direct = emby_source.EmbyDirect(cfg.get("embyUrl") or "",
                                        cfg.get("embyApiKey") or "")
        if not direct.ok:
            return {"ok": False,
                    "error": "请先在设置里填写 Emby 地址与 API Key"}

        req = _emby_request(ctx)
        dry_run = bool(input.get("dryRun"))
        lib_ids = input.get("libraryIds") or []

        try:
            libs = emby_source.list_libraries(req, sid)
        except Exception as e:
            return {"ok": False, "error": f"读取媒体库失败: {e}"}

        try:
            ctx.mt.storage.set(key=STORAGE_LIBS, value=[
                {"name": l["name"], "collectionType": l["collectionType"]} for l in libs])
        except Exception:
            pass

        targets = _filter_libraries(libs, cfg)
        if lib_ids:
            targets = [l for l in targets if l["id"] in lib_ids]
        if not targets:
            return {"ok": False, "error": "没有匹配的媒体库"}

        fonts = load_fonts()
        badge_on = bool(cfg.get("badgeEnabled"))
        badge_alpha = _as_int(cfg.get("badgeAlpha"), 180)
        bg_style = cfg.get("style") or "cinema-mesh"

        results: List[Dict[str, Any]] = []
        t0 = time.time()

        for lib in targets:
            name, lid, ctype = lib["name"], lib["id"], lib["collectionType"]
            entry: Dict[str, Any] = {"libraryId": lid, "libraryName": name,
                                     "status": "pending"}
            try:
                items, total = emby_source.fetch_items(req, sid, lid, ctype, limit=80)
                picked = emby_source.pick_poster_items(items, count=9)
                entry["itemCount"] = len(items)
                entry["totalCount"] = total
                entry["dedupPicked"] = len(picked)

                if len(picked) < 3:
                    entry["status"] = "skipped"
                    entry["message"] = f"可用海报不足（{len(picked)} 张）"
                    results.append(entry)
                    continue

                posters = [p for p in (direct.image(str(it["Id"])) for it in picked) if p]
                entry["posterLoaded"] = len(posters)
                if len(posters) < 3:
                    entry["status"] = "failed"
                    entry["message"] = "海报下载失败过多"
                    results.append(entry)
                    continue

                # 高级映射：自定义中文名 / 英文名（默认关闭，关闭时用库原名）
                title_cn, title_en = _resolve_titles(cfg, name,
                                                     SUB_BY_COLLECTION.get(ctype) or "MEDIA")
                png = render_cover(
                    posters, title_cn, title_en,
                    (fonts[0], fonts[1]),
                    background=(bg_style != "plain"),
                    badge_count=total if badge_on else None,
                    badge_alpha=badge_alpha,
                    random_color=bool(cfg.get("badgeRandomColor", True)),
                )
                entry["bytes"] = len(png)

                if dry_run:
                    entry["status"] = "dry-run"
                    entry["message"] = "仅生成未上传"
                else:
                    ok = direct.upload_image(lid, png)
                    entry["status"] = "uploaded" if ok else "failed"
                    if not ok:
                        entry["message"] = "写回 Emby 失败"
            except Exception as e:
                logger.exception("处理媒体库 %s 出错", name)
                entry["status"] = "failed"
                entry["message"] = str(e)[:200]
            results.append(entry)

        state = {
            "lastRun": time.strftime("%Y-%m-%d %H:%M:%S"),
            "elapsed": round(time.time() - t0, 1),
            "results": results,
            "totalOk": sum(1 for r in results if r["status"] == "uploaded"),
            "totalFail": sum(1 for r in results if r["status"] == "failed"),
            "totalSkip": sum(1 for r in results if r["status"] == "skipped"),
        }
        _save_state(ctx, state)
        return {"ok": True, "state": state}





    @action("restore-default")
    def restore_default(self, input: Dict[str, Any], ctx: Context) -> Dict[str, Any]:
        """清空运行记录（不影响 Emby 上已生成的封面）。"""
        _save_state(ctx, {"lastRun": None, "results": [], "totalOk": 0, "totalFail": 0})
        return {"ok": True}
