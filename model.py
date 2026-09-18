"""文案脚本工作台 · 模型层

职责（对应 PRD §3 §4.1 §4.4 §4.5 §4.6）：
    - 真值模型：segments（叙事/画面单元）+ sentences（文本）
    - 派生：合成单元切分、两层时间轴、导出物
    - 补丁：人机同权的 ops[] 应用
    - 增量：单元级脏检测（按文本哈希，不按下标）

三层单位（勿混）：
    段 segment      叙事 + 画面单元，挂镜头；也是音色/后端指定单位
    单元 unit       合成单元 = 段内按「句末标点」切出的整句；决定连贯性与增量粒度
    句 sentence     时间 + 字幕单元 = 用户写的一行（常按逗号断）

设计要点：
    * 真值（project.json）与派生缓存（index.json）分离；前者可进 git，后者可随时重建
    * 单元边界由「构造」得到 —— 逐单元合成，边界 = 时长前缀和，0 误差（§4.4）
    * 脏检测比对的是「单元文本哈希」，因此标点改动引起的重新切分会被自动捕获（§4.5）
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
import wave
from pathlib import Path
from project_library import atomic_json

# ── 标点 ────────────────────────────────────────────────────────────────

SENT_END = set("。！？!?…")
"""句末标点：合成单元的边界（实测选定，PRD §4.1）。"""

CLAUSE_END = set("，、,;；:：")
TONE_END = set("~～")            # 语气符号：自带收尾感，不再补标点（「吧~」不该变成「吧~。」）
ANY_PUNCT = SENT_END | CLAUSE_END | TONE_END | set("、…—")
"""子句标点：逗号类。段尾若以此收尾，合成期规范化为句号。"""

_PUNCT = SENT_END | CLAUSE_END | set("—～~()（）《》〈〉「」“”‘’·\"'")
_SKIP = set(" \t\r\n\u3000")


def char_weight(ch: str) -> float:
    """字符的时间权重：标点也占时间但短于一个音节。用于单元内细分。"""
    if ch in _SKIP:
        return 0.0
    if ch in _PUNCT:
        return 0.45
    return 1.0


def text_weight(text: str) -> float:
    return sum(char_weight(c) for c in text)


# ── 真值模型 ────────────────────────────────────────────────────────────

DEFAULT_META = {
    "title": "未命名",
    "defaultBackend": "doubao",
    "previewBackend": "browser",
    "canvas": {"w": 1920, "h": 1080, "fps": 30},
    "gapBetweenSegments": 0.45,
    "gapBetweenUnits": 0.0,
    "secondsPerChar": 0.17,      # 无音频时的时长预估（仅用于预览，标记 estimated）
    "backends": [],              # 由 tts.py 注入
    "subtitle": {"orr": "p", "font": "Noto Serif SC", "size": 4.2,
                 "bottom": 8, "stroke": True, "bar": False, "fontSource": ""},
}


def new_project(title: str = "未命名") -> dict:
    meta = json.loads(json.dumps(DEFAULT_META))
    meta["title"] = title
    return {
        "meta": meta,
        "segments": [{"id": "s1", "title": "第一段", "backend": "", "shots": []}],
        "sentences": [],
    }


def _fallback_backends() -> list[dict]:
    """后端注册表默认值。真值里没带时补上，保证模型层可独立使用（A9 降级）。"""
    try:
        import tts
        return tts.default_backends()
    except Exception:
        return [{"id": "doubao", "label": "豆包", "kind": "doubao", "cost": "paid",
                 "auto": False, "keep": True, "enabled": True, "pricePer1kChars": 0,
                 "currency": "CNY"}]


def normalize_project(project: dict) -> dict:
    """补齐缺省字段，校验基本结构。返回新对象。"""
    p = json.loads(json.dumps(project))
    meta = p.setdefault("meta", {})
    for k, v in DEFAULT_META.items():
        if k not in meta:
            meta[k] = json.loads(json.dumps(v))
        elif isinstance(v, dict):
            for kk, vv in v.items():
                meta[k].setdefault(kk, vv)
    # Stable local identity for URL routing. It is stored with the project, not
    # derived from an absolute Windows path that may be renamed or leak in URLs.
    if not isinstance(meta.get("projectId"), str) or not meta["projectId"].strip():
        meta["projectId"] = uuid.uuid4().hex
    # 前端可能只传部分 meta（例如只带 defaultBackend）—— 后端注册表必须补齐，
    # 否则合成时会报"未知后端"。
    if not meta.get("backends"):
        meta["backends"] = _fallback_backends()
    else:
        have = {b.get("id") for b in meta["backends"]}
        for b in _fallback_backends():
            if b["id"] not in have:
                meta["backends"].append(b)
    # Keep retired settings for recovery, but expose only the two supported paths.
    retired = [b for b in meta["backends"] if b.get("id") not in ("browser", "doubao")]
    if retired:
        archive = meta.setdefault("retiredBackends", [])
        have = {b.get("id") for b in archive}
        archive.extend(b for b in retired if b.get("id") not in have)
    meta["backends"] = [b for b in meta["backends"] if b.get("id") in ("browser", "doubao")]
    meta["previewBackend"] = "browser"
    meta["defaultBackend"] = "doubao"
    p.setdefault("segments", [])
    p.setdefault("sentences", [])
    if not p["segments"]:
        p["segments"] = [{"id": "s1", "title": "第一段", "backend": "", "shots": []}]
    # 句号连续编号（全局）+ 归属校验
    seen = {s["id"] for s in p["segments"]}
    p["sentences"] = [s for s in p["sentences"] if s.get("sid") in seen]
    for n, s in enumerate(p["sentences"], 1):
        s["i"] = n
        s.setdefault("text", "")
    for s in p["segments"]:
        s.setdefault("title", "")
        s.setdefault("backend", "")
        if s["backend"] not in ("", "doubao"):
            s["retiredBackend"] = s["backend"]
            s["backend"] = ""
        s.setdefault("shots", [])
    return p


def sentences_of(project: dict, sid: str) -> list[dict]:
    return [s for s in project["sentences"] if s["sid"] == sid]


# ── 合成单元切分（PRD §4.1）─────────────────────────────────────────────

def split_units(texts: list[str]) -> list[list[int]]:
    """把段内句文本切成合成单元，返回「句下标组」的列表。

    规则（实测依据见 PRD §4.1）：
      * 只有「句末标点」才结束一个单元 —— 因此无标点的行永远与相邻行粘在一起，
        绝不会出现"半句话被单独合成"（那正是实测中输出不可预测的情形）
      * 全段无句末标点 ⇒ 整段一个单元
      * 末尾残余自成一个单元（它是文本结尾，收束语气本就正确）
    """
    units: list[list[int]] = []
    cur: list[int] = []
    for idx, t in enumerate(texts):
        cur.append(idx)
        s = (t or "").rstrip()
        if s and s[-1] in SENT_END:
            units.append(cur)
            cur = []
    if cur:
        units.append(cur)
    return units


def unit_text(texts: list[str], unit: list[int], is_last_of_segment: bool) -> str:
    """单元的实际**合成文本**。注意：这不是用户看到的文本 —— 显示文本永远原样保留。

    合成期做三件规范化（实测依据见 PRD §4.1）：

      1. 单元内相邻两行之间若没写标点 → 补「，」。
         不补的话 TTS 会把两行连读成一口气：实测「我搓了一个网页版的小指南」+
         「就挂在我的小红书上」合成后是「…小指南就挂在我的小红书上」，听感上少一拍。
         用户常常为了排版好看而不打行尾逗号，所以这条例外必须由合成期兜住。
      2. 段尾以逗号类标点收尾 → 换成「。」（它是结束，本就该收束）。
      3. 段尾完全没有标点 → 补「。」。
    """
    parts = [(texts[i] or "").strip() for i in unit]
    parts = [x for x in parts if x]
    if not parts:
        return ""
    out = parts[0]
    for nxt in parts[1:]:
        # 两端都检查：上一行已经有收尾符号，或下一行以标点开头（用户手写的「，」），
        # 都不补 —— 否则会出「百搭牌，，两个7」这种双逗号。
        if (out and out[-1] not in ANY_PUNCT
                and (not nxt or nxt[0] not in ANY_PUNCT)):
            out += "，"
        out += nxt
    if is_last_of_segment and out:
        if out[-1] in CLAUSE_END:
            out = out[:-1] + "。"
        elif out[-1] not in SENT_END and out[-1] not in TONE_END:
            out += "。"
    return out


def _merge_empty(groups: list[list[int]], texts: list[str]) -> list[list[int]]:
    """空行/空白行不单独成单元 —— 合成空文本会失败，界面上也会出现 0.0s 的幽灵条。

    规则：空行并入前一个单元；若它出现在开头（前面还没单元），先攒着并入后面的
    第一个单元；整段全空则返回空（该段没有可合成内容）。
    """
    out: list[list[int]] = []
    pending: list[int] = []
    for g in groups:
        if any((texts[i] or "").strip() for i in g):
            out.append(pending + g if pending else g)
            pending = []
        elif out:
            out[-1].extend(g)
        else:
            pending.extend(g)
    return out if out else []


def unit_plan(project: dict, sid: str) -> list[dict]:
    """某段的单元计划：uid / 文本 / 覆盖句下标 / 各句文本。

    uid 采用位置式（s1u2），内容变化通过 hash 体现 —— 这样编辑不会打乱 uid，
    音频缓存按 hash 失效，符合「未改单元零重合成」。
    """
    sents = sentences_of(project, sid)
    texts = [s["text"] for s in sents]
    groups = _merge_empty(split_units(texts), texts)
    out = []
    for n, g in enumerate(groups, 1):
        is_last = n == len(groups)
        txt = unit_text(texts, g, is_last)
        out.append({
            "uid": f"{sid}u{n}",
            "sid": sid,
            "index": n,
            "sentenceIds": [sents[i]["i"] for i in g],
            "texts": [texts[i] for i in g],
            "text": txt,
            "chars": len(re.sub(r"\s", "", txt)),
        })
    return out


def backend_signature(project: dict, sid: str) -> str:
    """音频缓存键中的后端部分：段级后端（空 = 用全局默认）+ 音色参数。"""
    seg = next((s for s in project["segments"] if s["id"] == sid), {})
    bid = seg.get("backend") or project["meta"].get("defaultBackend") or "doubao"
    conf = next((b for b in project["meta"].get("backends", []) if b["id"] == bid), {})
    key = {k: conf.get(k) for k in ("id", "resourceId", "speaker", "speechRate", "emotion")}
    return json.dumps(key, ensure_ascii=False, sort_keys=True)


def unit_hash(text: str, backend_sig: str) -> str:
    return hashlib.sha1(f"{backend_sig}\x00{text}".encode("utf-8")).hexdigest()[:16]


def plan_all(project: dict) -> list[dict]:
    """全项目的单元计划，附 hash 与所属段。"""
    out = []
    for seg in project["segments"]:
        sig = backend_signature(project, seg["id"])
        for u in unit_plan(project, seg["id"]):
            u["backendSig"] = sig
            u["hash"] = unit_hash(u["text"], sig)
            out.append(u)
    return out


# ── 派生缓存 index.json ─────────────────────────────────────────────────
#
# 缓存按「内容哈希」索引，不按 uid —— 这是烟测抓出的一个真 bug 的修正：
# 改一个逗号为句号会改变切分，位置式 uid 随之平移，未改动的单元会被误判为脏
# （白白重合成，违反 I2）。改成内容寻址后：
#     * 重新切分/插入/删除都不会误伤未改内容
#     * 音频文件内容寻址（units/<hash>.wav），同样的文本天然复用
#     * 同一文本配不同后端 → hash 已含 backendSig，不会串音

def empty_index() -> dict:
    return {"units": {}, "cache": {}, "sentences": {}, "version": 2}


def load_index(path: Path) -> dict:
    if not Path(path).exists():
        return empty_index()
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return empty_index()
    if d.get("version") != 2:          # v1 = 按 uid 索引的旧结构，直接丢弃重建
        return empty_index()
    d.setdefault("units", {})
    d.setdefault("cache", {})
    d.setdefault("sentences", {})
    return d


def save_index(path: Path, index: dict) -> None:
    atomic_json(path, index)


def audio_path_for(project_dir: Path, h: str) -> Path:
    """内容寻址的音频路径。"""
    return Path(project_dir) / "units" / f"{h}.wav"


def sync_index(project: dict, index: dict) -> dict:
    """按当前真值刷新布局表；状态由「哈希是否在缓存里」决定。返回 index（原地更新）。"""
    plan = plan_all(project)
    units, cache = {}, index.setdefault("cache", {})
    for u in plan:
        rec = cache.get(u["hash"]) or {}
        units[u["uid"]] = {
            "uid": u["uid"], "sid": u["sid"], "index": u["index"],
            "hash": u["hash"], "chars": u["chars"], "text": u["text"],
            "sentenceIds": u["sentenceIds"],
            "audio": rec.get("audio"),
            "duration": rec.get("duration"),
            "backend": rec.get("backend"),
            "status": "clean" if rec.get("audio") and rec.get("duration") else "dirty",
        }
    index["units"] = units
    # 这里**只**清理「从来没合成成功过」的空条目（无 audio，留着没意义）。
    #
    # 曾经在这里按「是否仍被引用」回收，结果是：把一句逗号改成句号，原来的合并单元
    # 从计划里消失 → 它的音频缓存被删；用户再改回去（撤销 / 试两种写法）就变成
    # cache miss → 提示「待合成」→ 为同一段文字再付一次钱。实测复现。
    #
    # 「改一版再改回去」是写作里最常见的动作之一，缓存必须能扛住它。
    # 条目本身很小（哈希 + 相对路径 + 时长），真正需要清理的只有「文件确实不在磁盘」
    # 的情况 —— 那交给 gc_index()，它在知道工程目录的地方跑。
    for h, rec in list(cache.items()):
        if not rec.get("audio"):
            cache.pop(h, None)
    return index


def gc_index(project_dir, index: dict) -> int:
    """清理「音频文件确实不在磁盘上」的缓存条目，并把引用它的单元打回脏。

    注意与 sync_index 的分工：这里只按**文件存在性**清理，不按引用计数
    （理由见 sync_index 里的注释）。返回清理条数。
    """
    from pathlib import Path

    cache = index.get("cache", {})
    removed = 0
    for h, rec in list(cache.items()):
        ap = rec.get("audio")
        if ap and (Path(project_dir) / ap).exists():
            continue
        cache.pop(h, None)
        removed += 1
        for u in index.get("units", {}).values():
            if u.get("hash") == h:
                u.update({"audio": None, "duration": None, "status": "dirty"})
    return removed


def mark_synthesized(index: dict, uid: str, audio: str, duration: float,
                     backend: str) -> dict:
    """合成成功后回写缓存，并把所有引用同一哈希的单元一起转 clean。"""
    rec = index["units"].get(uid)
    if not rec:
        return index
    h = rec["hash"]
    index["cache"][h] = {"audio": audio, "duration": float(duration),
                         "backend": backend, "chars": rec["chars"]}
    for r in index["units"].values():
        if r["hash"] == h:
            r.update({"audio": audio, "duration": index["cache"][h]["duration"],
                      "backend": backend, "status": "clean"})
    return index


def reconcile_durations(project_dir, index: dict) -> int:
    """把缓存里的单元时长与磁盘音频文件对齐（以文件真实长度为准）。

    必要性：时长若来自 TTS 返回值而非文件本身，成片音轨会比时间轴长，
    字幕逐段漂移。这里按「文件大小+修改时间」做指纹缓存，只在文件变化时
    才调用 ffprobe，因此常规请求几乎零开销。
    返回修正条数。
    """
    import shutil
    import subprocess
    from pathlib import Path

    probe = shutil.which("ffprobe")
    if not probe:
        return 0
    fixed = 0
    for uid, u in index["units"].items():
        rec = index["cache"].get(u["hash"])
        if not rec or not rec.get("audio"):
            continue
        f = Path(project_dir) / rec["audio"]
        if not f.exists():
            continue
        try:
            st = f.stat()
        except OSError:
            continue
        fp = f"{st.st_size}:{st.st_mtime_ns}"
        if rec.get("fileFingerprint") == fp and rec.get("duration"):
            continue
        r = subprocess.run([probe, "-v", "error", "-show_entries", "format=duration",
                            "-of", "csv=p=0", str(f)],
                           capture_output=True, text=True, errors="replace")
        try:
            if f.suffix.lower() == '.wav':
                with wave.open(str(f), 'rb') as audio:
                    d = audio.getnframes() / audio.getframerate()
            else:
                d = float(r.stdout.strip())
        except (ValueError, OSError, wave.Error):
            continue
        rec["fileFingerprint"] = fp
        if rec.get("duration") != d:
            rec["duration"] = d
            fixed += 1
        for uu in index["units"].values():
            if uu["hash"] == u["hash"]:
                uu["duration"] = d
    return fixed


def dirty_units(project: dict, index: dict) -> list[dict]:
    """当前需要（重新）合成的单元，按段与顺序排列。"""
    plan = {u["uid"]: u for u in plan_all(project)}
    out = []
    for uid, rec in index["units"].items():
        if rec.get("status") != "clean":
            out.append({**plan.get(uid, {}), **rec})
    out.sort(key=lambda r: (r.get("sid", ""), r.get("index", 0)))
    return out


def estimate(project: dict, index: dict, backend_id: str | None = None) -> dict:
    """定稿确认窗的数据源：N 单元 / M 字 / 预估费用 / 预计耗时（PRD A10）。"""
    du = dirty_units(project, index)
    if backend_id:
        du = [u for u in du if resolve_backend_id(project, u["sid"]) == backend_id]
    chars = sum(u.get("chars", 0) for u in du)
    conf = _backend_conf(project, backend_id) if backend_id else {}
    price = float(conf.get("pricePer1kChars") or 0)
    cost = round(chars / 1000.0 * price, 4) if price > 0 else (0.0 if conf.get("cost") == "free" else None)
    return {
        "units": len(du),
        "segments": len({u["sid"] for u in du}),
        "chars": chars,
        "backend": backend_id or project["meta"].get("defaultBackend") or "doubao",
        "costFree": (conf.get("cost") == "free") if conf else None,
        "estimatedCost": cost,
        "currency": conf.get("currency", "CNY"),
        "unitList": [{"uid": u["uid"], "chars": u.get("chars", 0)} for u in du],
    }


def _backend_conf(project: dict, backend_id: str | None) -> dict:
    if not backend_id:
        return {}
    return next((b for b in project["meta"].get("backends", []) if b["id"] == backend_id), {})


def resolve_backend_id(project: dict, sid: str) -> str:
    seg = next((s for s in project["segments"] if s["id"] == sid), {})
    return seg.get("backend") or project["meta"].get("defaultBackend") or "doubao"


# ── 两层时间轴（PRD §4.4）───────────────────────────────────────────────

def compute_timeline(project: dict, index: dict) -> dict:
    """骨架（单元，构造而来）+ 细分（单元内按字符权重切）。

    返回 {segments:[{id,start,end,units}], sentences:[{i,start,end,stale}], total, stale}
    """
    meta = project["meta"]
    gap_seg = float(meta.get("gapBetweenSegments", 0.0))
    gap_unit = float(meta.get("gapBetweenUnits", 0.0))
    spc = float(meta.get("secondsPerChar", 0.17))

    segs_out, sent_out = [], {}
    cursor = 0.0
    any_stale = False

    for si, seg in enumerate(project["segments"]):
        sid = seg["id"]
        if si > 0:
            cursor += gap_seg
        seg_start = cursor
        units_out = []
        for u in unit_plan(project, sid):
            rec = index["units"].get(u["uid"], {})
            stale = rec.get("status") != "clean"
            dur = rec.get("duration")
            if dur is None:
                dur = round(u["chars"] * spc, 3)
                stale = True
            dur = float(dur)
            any_stale = any_stale or stale
            u_start, u_end = cursor, cursor + dur
            # 细分：单元内各句按字符权重分配 [u_start, u_end]
            weights = [text_weight(t) for t in u["texts"]]
            wsum = sum(weights) or float(len(u["texts"]) or 1)
            weights = weights if sum(weights) > 0 else [1.0] * len(u["texts"])
            wsum = sum(weights)
            acc = u_start
            for k, s_i in enumerate(u["sentenceIds"]):
                span = (u_end - u_start) * (weights[k] / wsum)
                sent_out[s_i] = {"i": s_i, "start": round(acc, 3),
                                 "end": round(acc + span, 3), "stale": stale}
                acc += span
            units_out.append({"uid": u["uid"], "index": u["index"], "start": round(u_start, 3),
                              "end": round(u_end, 3), "duration": round(dur, 3),
                              "stale": stale, "text": u["text"],
                              "sentenceIds": u["sentenceIds"]})
            cursor = u_end + gap_unit
        if units_out:
            cursor -= gap_unit        # 末尾多余的那个 gap 收回来
        segs_out.append({"id": sid, "title": seg.get("title", ""),
                         "start": round(seg_start, 3), "end": round(cursor, 3),
                         "duration": round(cursor - seg_start, 3), "units": units_out})

    total = cursor
    return {
        "segments": segs_out,
        "sentences": [sent_out[k] for k in sorted(sent_out)],
        "total": round(total, 3),
        "stale": any_stale,
    }


# ── 补丁（PRD §4.6）─────────────────────────────────────────────────────

OP_TYPES = {"replaceText", "insertAfter", "delete", "setSegmentTitle", "setShots",
            "setSegmentBackend", "addSegment", "deleteSegment", "moveSegment"}


def apply_patch(project: dict, ops: list[dict]) -> dict:
    """应用补丁，返回新的 project（不修改入参）。ops 见 PRD §4.6。"""
    p = json.loads(json.dumps(project))     # 深拷贝
    p = normalize_project(p)
    by_i = {s["i"]: s for s in p["sentences"]}
    sid_order = [s["id"] for s in p["segments"]]

    for op in ops:
        kind = op.get("op")
        if kind not in OP_TYPES:
            raise ValueError(f"未知补丁操作：{kind}")

        if kind == "replaceText":
            tgt = by_i.get(op.get("i"))
            if tgt is None:
                raise ValueError(f"replaceText：找不到第 {op.get('i')} 句")
            tgt["text"] = op.get("text", "")

        elif kind == "insertAfter":
            base = op.get("i")
            sid = op.get("sid") or by_i.get(base, {}).get("sid")
            if sid not in sid_order:
                raise ValueError(f"insertAfter：段 {sid} 不存在")
            texts = op.get("texts") or []
            rows = [{"sid": sid, "text": t} for t in texts]
            pos = max((n for n, s in enumerate(p["sentences"]) if s["i"] == base),
                      default=len(p["sentences"]) - 1) + 1
            p["sentences"][pos:pos] = rows

        elif kind == "delete":
            ids = set(op.get("ids") or [])
            p["sentences"] = [s for s in p["sentences"] if s["i"] not in ids]

        elif kind == "setSegmentTitle":
            seg = _need_seg(p, op.get("sid"))
            seg["title"] = op.get("title", "")

        elif kind == "setShots":
            seg = _need_seg(p, op.get("sid"))
            seg["shots"] = op.get("shots") or []

        elif kind == "setSegmentBackend":
            seg = _need_seg(p, op.get("sid"))
            seg["backend"] = op.get("backend", "")

        elif kind == "addSegment":
            sid = op.get("sid") or _next_sid(p)
            after = op.get("after")
            seg = {"id": sid, "title": op.get("title", ""),
                   "backend": op.get("backend", ""), "shots": op.get("shots") or []}
            idx = sid_order.index(after) + 1 if after in sid_order else len(p["segments"])
            p["segments"].insert(idx, seg)
            sid_order = [s["id"] for s in p["segments"]]

        elif kind == "deleteSegment":
            sid = op.get("sid")
            p["segments"] = [s for s in p["segments"] if s["id"] != sid]
            p["sentences"] = [s for s in p["sentences"] if s["sid"] != sid]
            sid_order = [s["id"] for s in p["segments"]]
            if not p["segments"]:
                p["segments"] = [{"id": "s1", "title": "", "backend": "", "shots": []}]
                sid_order = ["s1"]

        elif kind == "moveSegment":
            sid, to = op.get("sid"), int(op.get("to", 0))
            seg = _need_seg(p, sid)
            p["segments"].remove(seg)
            p["segments"].insert(max(0, min(to, len(p["segments"]))), seg)
            sid_order = [s["id"] for s in p["segments"]]

        by_i = {s["i"]: s for s in p["sentences"]}

    return normalize_project(p)


def _need_seg(p: dict, sid) -> dict:
    seg = next((s for s in p["segments"] if s["id"] == sid), None)
    if seg is None:
        raise ValueError(f"段 {sid} 不存在")
    return seg


def _next_sid(p: dict) -> str:
    n = 1
    used = {s["id"] for s in p["segments"]}
    while f"s{n}" in used:
        n += 1
    return f"s{n}"


def diff_changed_segments(before: dict, after: dict) -> dict:
    """比对真值，返回受影响的段与单元（脏检测的落点）。

    按「单元文本哈希」比对而非下标 —— 因此把 `，` 改成 `。` 导致的重新切分
    也会被正确捕获（§4.5）。
    """
    b = {(u["sid"], u["uid"]): u["hash"] for u in plan_all(before)}
    a = {(u["sid"], u["uid"]): u["hash"] for u in plan_all(after)}
    changed_segs, added, removed, modified = set(), [], [], []
    for key, h in a.items():
        if key not in b:
            added.append(key)
            changed_segs.add(key[0])
        elif b[key] != h:
            modified.append(key)
            changed_segs.add(key[0])
    for key in b:
        if key not in a:
            removed.append(key)
            changed_segs.add(key[0])
    return {"segments": sorted(changed_segs),
            "added": [f"{s}/{u}" for s, u in added],
            "removed": [f"{s}/{u}" for s, u in removed],
            "modified": [f"{s}/{u}" for s, u in modified]}
