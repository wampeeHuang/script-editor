"""句级时间戳对齐 —— 不依赖任何 TTS 厂商。

两条路径，但**优先级不同**（2026-09-16 实测后修正）：

  默认路径（不在本模块，见 service 层）
      逐句合成 + 时长前缀和。边界是「构造」出来的，0 误差、零成本、不依赖音频内容。
      代价是句间语气不如整段连贯。

  align_by_vad(lines, audio)  —— 可选，配合"整段合成"路线
      音频侧：ffmpeg silencedetect 找候选停顿点 → 用「N 行 ⇒ 恰好 N-1 个边界」
      这一硬约束，在候选点里做 DP 单调匹配，选出真正的行边界。
      ⚠ 实测局限：豆包在逗号处常不停顿（`…小指南，就挂在我…` 从 2.94s 连续说到
      7.39s，-50dB 下仍无静音），导致行边界漏检。**-35/-40/-45/-50dB 四档均无效**
      —— 这不是"不够准"，而是音频里压根没有该边界的信息，属"无解"。
      因此本函数只应作为整段合成时的可选增强；一旦候选点数不足即返回 ok=False，
      由调用方回落默认路径，**绝不硬猜**。

  align_by_words(lines, words)  —— 校准基准
      文本侧：若 TTS 恰好返回字级时间戳（如豆包非流式），直接做字符级 LCS 对齐。
      既可作为精度基准来体检默认路径，也可在异常时定点复核。

设计取舍见 PRD §4.3。
"""

from __future__ import annotations

import re
import shutil
import subprocess

__all__ = [
    "align_by_vad",
    "align_by_words",
    "detect_silences",
    "ffprobe_duration",
]

# 标点权重：标点也占时间，但短于一个音节。
_PUNCT = set("，。！？；：、,.!?;:…—～~（）()《》〈〉「」“”‘’\"'·")
# 不占时间的字符
_SKIP = set(" \t\r\n\u3000")


def _char_weight(ch: str) -> float:
    if ch in _SKIP:
        return 0.0
    if ch in _PUNCT:
        return 0.45
    return 1.0


def _weight(text: str) -> float:
    return sum(_char_weight(c) for c in text)


# ── 音频探测 ────────────────────────────────────────────────────────────

def _ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise RuntimeError("找不到 ffmpeg，无法做音频侧对齐")
    return exe


def ffprobe_duration(path: str) -> float:
    """返回音频/视频时长（秒）。"""
    probe = shutil.which("ffprobe")
    if not probe:
        raise RuntimeError("找不到 ffprobe")
    out = subprocess.run(
        [probe, "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", path],
        capture_output=True, text=True, errors="replace",
    )
    try:
        return float(out.stdout.strip())
    except ValueError:
        raise RuntimeError(f"无法读取时长：{path}（{out.stderr.strip()[:120]}）")


def detect_silences(path: str, noise_db: float = -35.0,
                    min_dur: float = 0.12) -> list[tuple[float, float]]:
    """用 ffmpeg silencedetect 检出静音区间 [(start, end), ...]。"""
    cmd = [
        _ffmpeg(), "-hide_banner", "-nostdin", "-i", path,
        "-af", f"silencedetect=noise={noise_db}dB:d={min_dur}",
        "-f", "null", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    sils: list[tuple[float, float]] = []
    cur: float | None = None
    for line in proc.stderr.splitlines():
        m = re.search(r"silence_start:\s*(-?[\d.]+)", line)
        if m:
            cur = float(m.group(1))
            continue
        m = re.search(r"silence_end:\s*([\d.]+)", line)
        if m and cur is not None:
            sils.append((max(0.0, cur), float(m.group(1))))
            cur = None
    return sils


# ── 预测与 DP ───────────────────────────────────────────────────────────

def _predict(lines: list[str], span_start: float, span_end: float) -> list[float]:
    """按字符权重预测 N-1 个边界应出现的位置（绝对秒）。"""
    weights = [_weight(ln) for ln in lines]
    total = sum(weights)
    if total <= 0:
        return []
    span = span_end - span_start
    out, cum = [], 0.0
    for w in weights[:-1]:
        cum += w
        out.append(span_start + cum / total * span)
    return out


def _dp_choose(cands: list[tuple[float, float]],
               preds: list[float]) -> list[int] | None:
    """在 cands 里选 len(preds) 个、严格递增的下标，使与 preds 的偏差和最小。

    cands 用停顿中点与预测位置比较；O(P·M)。
    """
    P, M = len(preds), len(cands)
    if P == 0:
        return []
    if M < P:
        return None
    mids = [(s + e) / 2 for s, e in cands]
    INF = float("inf")
    dp = [[INF] * M for _ in range(P)]
    par = [[-1] * M for _ in range(P)]
    for j in range(M):
        dp[0][j] = abs(mids[j] - preds[0])
    for i in range(1, P):
        best, bj = INF, -1
        for j in range(M):
            if j - 1 >= 0 and dp[i - 1][j - 1] < best:
                best, bj = dp[i - 1][j - 1], j - 1
            if best < INF:
                cost = best + abs(mids[j] - preds[i])
                if cost < dp[i][j]:
                    dp[i][j], par[i][j] = cost, bj
    # 回溯
    end = min(range(M), key=lambda j: dp[P - 1][j])
    if dp[P - 1][end] == INF:
        return None
    picked = [end]
    for i in range(P - 1, 0, -1):
        picked.append(par[i][picked[-1]])
    picked.reverse()
    return picked if all(picked[k] < picked[k + 1] for k in range(len(picked) - 1)) else None


# ── 主路径一：音频侧 VAD + 文本约束 ─────────────────────────────────────

def align_by_vad(lines: list[str], audio: str, *,
                 noise_db: float = -35.0, min_dur: float = 0.12,
                 edge_guard: float = 0.15) -> dict:
    """返回 {ok, method, lines:[{i,start,end}], diagnostics}。失败时 ok=False。"""
    lines = [ln for ln in lines]
    if not lines:
        return {"ok": False, "method": "vad", "lines": [], "reason": "没有句子"}

    try:
        duration = ffprobe_duration(audio)
        sils = detect_silences(audio, noise_db, min_dur)
    except RuntimeError as exc:
        return {"ok": False, "method": "vad", "lines": [], "reason": str(exc)}

    # 开头 / 结尾静音单独处理，不计入候选边界
    lead, trail = 0.0, duration
    inner: list[tuple[float, float]] = []
    for s, e in sils:
        if s <= edge_guard:
            lead = max(lead, e)
        elif e >= duration - edge_guard:
            trail = min(trail, s)
        else:
            inner.append((s, e))

    need = len(lines) - 1
    if need == 0:
        return {"ok": True, "method": "vad", "duration": duration,
                "lines": [{"i": 0, "start": round(lead, 3), "end": round(trail, 3)}],
                "diagnostics": {"candidates": len(inner), "needed": 0}}

    if len(inner) < need:
        return {"ok": False, "method": "vad", "lines": [], "duration": duration,
                "reason": f"候选停顿点不足：需要 {need} 个行边界，只检出 {len(inner)} 个",
                "diagnostics": {"candidates": len(inner), "needed": need}}

    preds = _predict(lines, lead, trail)
    picked = _dp_choose(inner, preds)
    if picked is None:
        return {"ok": False, "method": "vad", "lines": [], "duration": duration,
                "reason": "DP 未能选出合法单调边界序列"}

    bounds = [inner[i] for i in picked]
    out = []
    for k in range(len(lines)):
        start = lead if k == 0 else bounds[k - 1][1]
        end = trail if k == len(lines) - 1 else bounds[k][0]
        out.append({"i": k, "start": round(start, 3), "end": round(end, 3)})

    dev = [round(abs((bounds[k][0] + bounds[k][1]) / 2 - preds[k]), 3)
           for k in range(len(bounds))]
    return {
        "ok": True, "method": "vad", "duration": round(duration, 3), "lines": out,
        "diagnostics": {"candidates": len(inner), "needed": need,
                        "rejected": len(inner) - need,
                        "max_deviation_from_prior": max(dev) if dev else 0.0,
                        "noise_db": noise_db, "min_dur": min_dur},
    }


# ── 主路径二：字级时间戳 + 字符 LCS 对齐 ────────────────────────────────

def _lcs_map(orig: str, norm: str) -> list[int | None]:
    """orig 每个下标 → norm 下标（未匹配为 None）。"""
    n, m = len(orig), len(norm)
    if n == 0 or m == 0:
        return [None] * n
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        row, nxt = dp[i], dp[i + 1]
        for j in range(m - 1, -1, -1):
            row[j] = nxt[j + 1] + 1 if orig[i] == norm[j] else max(nxt[j], row[j + 1])
    mapping: list[int | None] = [None] * n
    i = j = 0
    while i < n and j < m:
        if orig[i] == norm[j]:
            mapping[i] = j
            i += 1
            j += 1
        elif dp[i + 1][j] >= dp[i][j + 1]:
            i += 1
        else:
            j += 1
    return mapping


def align_by_words(lines: list[str], words: list[dict]) -> dict:
    """words: [{'word': str, 'startTime': float, 'endTime': float}, ...] 按序。

    适合豆包非流式返回的 sentences[].words[]（每项可能多字，逐字展开）。
    """
    words = [w for w in words if w.get("word")]
    if not words or not lines:
        return {"ok": False, "method": "words", "lines": [], "reason": "缺少 words 或句子"}

    norm_chars: list[str] = []
    starts: list[float] = []
    ends: list[float] = []
    for w in words:
        s, e = float(w["startTime"]), float(w["endTime"])
        for ch in w["word"]:
            norm_chars.append(ch)
            starts.append(s)
            ends.append(e)
    norm = "".join(norm_chars)

    orig = "".join(lines)
    mapping = _lcs_map(orig, norm)

    out: list[dict | None] = []
    pos = 0
    for line in lines:
        hit = [mapping[pos + k] for k in range(len(line)) if mapping[pos + k] is not None]
        pos += len(line)
        out.append(None if not hit else
                   {"start": round(starts[min(hit)], 3), "end": round(ends[max(hit)], 3)})

    # 借位填补（纯标点行 / 完全未匹配行）
    for k, item in enumerate(out):
        if item is None:
            prev = next((out[x] for x in range(k - 1, -1, -1) if out[x]), None)
            nxt = next((out[x] for x in range(k + 1, len(out)) if out[x]), None)
            if prev and nxt:
                out[k] = {"start": prev["end"], "end": nxt["start"]}
            elif prev:
                out[k] = {"start": prev["end"], "end": prev["end"]}
            elif nxt:
                out[k] = {"start": nxt["start"], "end": nxt["start"]}

    matched = sum(1 for v in mapping if v is not None)
    return {
        "ok": True, "method": "words",
        "lines": [dict(i=k, **item) for k, item in enumerate(out) if item],
        "diagnostics": {"norm_chars": len(norm), "orig_chars": len(orig),
                        "matched": matched,
                        "match_rate": round(matched / len(orig), 3) if orig else 0.0},
    }
