# -*- coding: utf-8 -*-
"""插件工作台页面（MediaTidy UI v2）。

设计目标：**打开就能用，点一下就跑**。
  1. 首次：填 Emby 地址 + API Key，保存
  2. 之后：点「一键生成」即可
  3. 想先看效果：勾「仅预览」

⚠️ UI v2 的限制：每个组件只接受白名单 props，且**不支持 children 嵌套**，
所以页面只能平铺列出来，显示顺序 = 代码顺序。
"""
from __future__ import annotations

import json

from mt_plugin import Context
from mt_plugin.ui import page, component


def _invoke(action_id: str, input: dict | None = None) -> dict:
    """触发插件自己的动作。"""
    return {"type": "invoke", "actionId": action_id, "input": input or {}}


def _host(name: str) -> dict:
    """触发宿主内置动作（saveConfig / refreshPage ...）。"""
    return {"type": "host", "name": name}


def _btn(label: str, event: dict, variant: str = "default",
         size: str = "default") -> dict:
    return component("Button", label=label, variant=variant, size=size,
                     onClick=[event])


_STATUS_TEXT = {
    "uploaded": "✅ 已写回",
    "failed": "❌ 失败",
    "skipped": "⏭ 跳过",
    "dry-run": "👁 仅预览",
    "pending": "…",
}


def _library_skeleton(cfg: dict) -> str:
    """把当前 Emby 媒体库列表渲染成可复制的 JSON 骨架。"""
    libs = cfg.get("_libraries") or []
    if not isinstance(libs, list) or not libs:
        return ("**当前媒体库**：点上方「🔄 刷新结果」后重新进入本页即可读到库名；\n"
                "也可以直接在 Emby 后台照抄媒体库名称。")

    tmap = cfg.get("titleMap") or {}
    if isinstance(tmap, str):
        try:
            tmap = json.loads(tmap or "{}")
        except Exception:
            tmap = {}

    lines = ["**当前媒体库名单**（可直接挑选填进上面的映射表）：", ""]
    for lib in libs:
        name = lib.get("name") or ""
        ctype = lib.get("collectionType") or ""
        cur = tmap.get(name) if isinstance(tmap, dict) else None
        if isinstance(cur, dict) and (cur.get("title") or cur.get("subtitle")):
            lines.append("- `%s`（%s）— 已配置" % (name, ctype or "-"))
        else:
            lines.append("- `%s`（%s）" % (name, ctype or "-"))

    sample = {}
    for lib in libs[:3]:
        name = lib.get("name") or ""
        default_en = {"movies": "MOVIES", "tvshows": "TV SHOWS",
                      "boxsets": "BOXSET"}.get(lib.get("collectionType") or "", "")
        sample[name] = {"title": "", "subtitle": default_en}
    lines += ["", "**复制下面的骨架再改**：", "", "```json",
              json.dumps(sample, ensure_ascii=False, indent=2), "```"]
    return "\n".join(lines)


_SAMPLE_JSON = """{
  \"外语电影\": { \"title\": \"\", \"subtitle\": \"MOVIES\" },
  \"国产剧集\": { \"title\": \"国产剧\", \"subtitle\": \"CHINESE DRAMA\" },
  \"日本动漫\": { \"title\": \"日本动漫\", \"subtitle\": \"\" }
}"""

_MAP_DOC = """**填写要点**

| 字段 | 说明 |
|---|---|
| 键（如 `外语电影`） | **必须和 Emby 里的媒体库名一字不差** |
| `title` | 封面主标题（中文大字）。留空 `""` → 用媒体库原名 |
| `subtitle` | 封面副标题（英文小字）。留空 `""` → 不显示 |

**没写进映射表的库** → 自动用默认（库名 + MOVIES/TV SHOWS）。

**副标题参考**：`MOVIES` 电影 · `TV SHOWS` 剧集 · `ANIME` 动漫 · `BOXSET` 合集 · `DOCUMENTARY` 纪录

只需列出想改的库，不必写全部。注意用英文双引号，最后一项后面不加逗号。
"""


def _toggle_label(cfg: dict) -> str:
    on = bool(cfg.get("titleMapEnabled"))
    try:
        rc = int(cfg.get("mapRowCount") or 1)
    except Exception:
        rc = 1
    if on or rc > 1:
        return "✖ 关闭标题映射"
    return "➕ 开启标题映射"


def _vis(bind_path: str) -> dict:
    """生成 visible 表达式（绑定到 state.mapOpen）。

    ⚠️ 不能绑 config.xxx：插件无法写宿主 config，add-row 后刷新会变回 False，
    导致输入框整体消失。state 是插件每次渲染自己给的，可靠。
    """
    return {"$op": "if", "args": [{"$ref": "state.mapOpen"}, True, False]}


def _rows_count(cfg: dict | None = None) -> int:
    """返回要显示的行数。

    MediaTidy UI v2 无法动态增减行（动作改不了表单、刷新会丢输入），
    所以固定给 6 行，没填的行自动忽略。
    """
    return 6


def _unused_rows_count() -> int:
    """固定展示多少行映射编辑器。"""
    return 12


def _map_rows_ui(cfg: dict) -> list[dict]:
    """生成扁平字段的映射表格。

    MediaTidy UI v2 的 bind 不支持数组索引，只能用 config.mapNKey 这种扁平字段。
    页面渲染时由 gather_map(cfg) 把扁平行还原成 titleMap。
    """
    # 优先用 get_page 已拆好的 mapN 字段回显
    pairs: list[tuple[str, str, str]] = []
    for i in range(_rows_count(cfg)):
        k = cfg.get("map%dKey" % i)
        if k:
            pairs.append((str(k), str(cfg.get("map%dTitle" % i) or ""),
                          str(cfg.get("map%dSub" % i) or "")))
    if not pairs:
        tmap = cfg.get("titleMap") or {}
        if isinstance(tmap, str):
            try:
                tmap = json.loads(tmap or "{}")
            except Exception:
                tmap = {}
        if isinstance(tmap, dict):
            pairs = [(k, (v or {}).get("title") or "", (v or {}).get("subtitle") or "")
                     for k, v in tmap.items() if isinstance(v, dict)]

    rows_n = _rows_count(cfg)
    nodes: list[dict] = []
    for i in range(rows_n):
        row = pairs[i] if i < len(pairs) else ("", "", "")
        vis = _vis("config.titleMapEnabled")
        nodes.append(component("Input", label="媒体库名", bind="state.map%dKey" % i,
                               placeholder=row[0] or "如：外语电影", visible=vis))
        nodes.append(component("Input", label="主标题", bind="state.map%dTitle" % i,
                               placeholder=row[1] or "留空=用库原名", visible=vis))
        nodes.append(component("Input", label="副标题", bind="state.map%dSub" % i,
                               placeholder=row[2] or "如：MOVIES", visible=vis))
        nodes.append(component("Divider", visible=vis))
    return nodes


def gather_map(cfg: dict) -> dict:
    """把 config.mapNKey/Title/Sub 扁平行还原成 titleMap。"""
    out: dict = {}
    for i in range(30):
        key = str(cfg.get("map%dKey" % i) or "").strip()
        if not key:
            continue
        out[key] = {
            "title": str(cfg.get("map%dTitle" % i) or "").strip(),
            "subtitle": str(cfg.get("map%dSub" % i) or "").strip(),
        }
    return out


def _titlemap_json(cfg: dict) -> str:
    """映射表渲染成 Textarea 初始文本。"""
    tmap = cfg.get("titleMap") or {}
    if isinstance(tmap, str):
        return tmap
    if not isinstance(tmap, dict) or not tmap:
        return ""
    return json.dumps(tmap, ensure_ascii=False, indent=2)


def _titlemap_md(rows: list[dict]) -> str:
    """把标题映射渲染成 Markdown 表格。"""
    if not rows:
        return "（还没有数据，点上面的「📥 读取媒体库列表」）"
    out = ["| 媒体库名 | 封面主标题 | 封面副标题 |", "|---|---|---|"]
    for r in rows:
        out.append("| %s | %s | %s |" % (
            r.get("library") or "", r.get("title") or "（用库原名）",
            r.get("subtitle") or "（不显示）"))
    return "\n".join(out)


def build_page(ctx: Context, cfg_override: dict | None = None) -> dict:
    # 宿主下发的工作配置（get_page 会额外注入 _libraries）
    if cfg_override is not None:
        cfg = dict(cfg_override)
    else:
        try:
            cfg = getattr(ctx, "config", None) or {}
            if isinstance(cfg, str):
                cfg = json.loads(cfg or "{}")
        except Exception:
            cfg = {}

    # 插件自己存的暂存（load-titlemap 的结果），优先级低于用户已保存的 config
    try:
        staged = ctx.mt.storage.get(key="cover_studio_config") or {}
    except Exception:
        staged = {}
    # 已保存的 config 里没有映射时，用 load-titlemap 存进 storage 的结果兜底
    if isinstance(staged, dict) and staged.get("titleMap"):
        cur = cfg.get("titleMap")
        empty = (not cur) or (isinstance(cur, str) and cur.strip() in ("", "{}")) \
            or (isinstance(cur, dict) and not cur)
        if empty:
            cfg = {**staged, **cfg, "titleMap": staged["titleMap"]}
            if not cfg.get("titleMapEnabled"):
                cfg["titleMapEnabled"] = True

    try:
        state = ctx.mt.storage.get(key="cover_studio_state") or {}
    except Exception:
        state = {}

    has_key = bool(cfg.get("embyApiKey"))
    results = (state or {}).get("results") or []
    last_run = (state or {}).get("lastRun") or ""
    ok = sum(1 for r in results if r.get("status") == "uploaded")
    fail = sum(1 for r in results if r.get("status") == "failed")

    rows = [{
        "libraryName": r.get("libraryName") or "",
        "status": _STATUS_TEXT.get(r.get("status"), r.get("status") or ""),
        "posters": r.get("posterLoaded") or 0,
        "total": r.get("totalCount") or 0,
        "message": r.get("message") or "",
    } for r in results]

    nodes: list[dict] = []

    if not has_key:
        # ── 首次使用：两步引导 ──
        nodes += [
            component("Section", title="🎬 媒体库封面工坊",
                      description="第 1 步（只需做一次）：填好 Emby 地址和 API Key，点保存。"),
            component("Alert", tone="warning",
                      text="API Key 在 Emby 后台 → 设置 → 高级 → API 密钥 → 新建 里获取。"),
            component("Input", label="Emby 地址", bind="config.embyUrl",
                      placeholder="http://192.168.1.10:8096"),
            component("Input", label="Emby API Key", bind="config.embyApiKey",
                      placeholder="粘贴 API Key"),
            component("Checkbox", label="在封面左上角显示媒体数量角标",
                      bind="config.badgeEnabled"),
            _btn("💾 保存，下一步", _host("saveConfig"), "default", "lg"),
        ]
    else:
        # ── 已配置：一键生成 ──
        nodes += [
            component("Section", title="🎬 媒体库封面工坊",
                      description="点下面的按钮开始：读取各库海报 → 按作品去重 → 合成封面 → 写回 Emby。"),
            _btn("🚀 一键生成全部封面", _invoke("generate", {"dryRun": False}),
                 "default", "lg"),
            _btn("🔄 刷新结果", _host("refreshPage"), "ghost", "default"),
            component("Divider"),
            component("Section", title="上次运行",
                      description=(f"{last_run}　共 {len(rows)} 个库 · 成功 {ok} · 失败 {fail}"
                                   if rows else "还没有运行过")),
        ]
        if rows:
            lines = ["| 媒体库 | 状态 | 海报 | 总数 | 说明 |", "|---|---|---|---|---|"]
            for r in rows:
                lines.append("| %s | %s | %s | %s | %s |" % (
                    r["libraryName"], r["status"], r["posters"],
                    r["total"], r["message"]))
            nodes.append(component("Markdown", text="\n".join(lines)))
        nodes += [
            component("Divider"),
            component("Section", title="⚙️ 设置",
                      description="改完记得点最下面的「保存设置」。"),
            component("Input", label="Emby 地址", bind="config.embyUrl"),
            component("Input", label="Emby API Key", bind="config.embyApiKey"),
            component("Checkbox", label="在封面左上角显示媒体数量",
                      bind="config.badgeEnabled"),
            component("Select", label="角标背景深浅", bind="config.badgeAlpha",
                      options=[{"value": "255", "label": "不透明"},
                               {"value": "217", "label": "轻微透明"},
                               {"value": "180", "label": "半透明（默认）"},
                               {"value": "140", "label": "比较透"},
                               {"value": "100", "label": "很透"}]),
            component("Select", label="封面暗色调", bind="config.style",
                      options=[{"value": "cinema-mesh", "label": "跟随海报取色（推荐）"},
                               {"value": "plain", "label": "固定纯色"}]),
            component("Divider"),
            component("Section", title="🔤 封面标题映射（可选）",
                      description="默认关闭。开启后按媒体库自定义封面上的中英文标题。"),
            component("Checkbox", label="开启自定义标题", bind="config.titleMapEnabled"),
            component("Textarea", label="映射表（JSON 格式）",
                      bind="config.titleMap",
                      placeholder=_SAMPLE_JSON),
            component("Markdown", text=_MAP_DOC),
            component("Button", label="💾 保存设置", variant="default", size="lg",
                      onClick=[_host("saveConfig"), _host("refreshPage")]),
        ]

    # 把完整配置（含 mapN 扁平行）放进 state，供各 Input 回显
    state_cfg = {k: v for k, v in cfg.items()
                 if not k.startswith("_") and not isinstance(v, (list, dict))}
    # Textarea 需要字符串才能回显
    tm = cfg.get("titleMap")
    if isinstance(tm, dict) and tm:
        state_cfg["titleMap"] = json.dumps(tm, ensure_ascii=False, indent=2)
    elif isinstance(tm, str):
        state_cfg["titleMap"] = tm
    else:
        state_cfg["titleMap"] = ""
    if not state_cfg.get("titleMapEnabled"):
        state_cfg.setdefault("titleMapEnabled", cfg.get("titleMapEnabled"))
    # mapOpen：只要有行数 > 1 或已开启，就保持展开
    try:
        rc = int(cfg.get("mapRowCount") or 1)
    except Exception:
        rc = 1
    map_open = bool(cfg.get("titleMapEnabled")) or rc > 1 or bool(cfg.get("map0Key"))
    return page(*nodes, state={"config": state_cfg, "mapOpen": map_open})
