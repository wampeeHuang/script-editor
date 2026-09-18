"""文案与旁白导出。每次交付独立目录；不向外部视频项目投放或覆盖文件。"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
import uuid
import wave
from pathlib import Path

import model

SRT_TIME = lambda s: (f"{int(s // 3600):02d}:{int(s % 3600 // 60):02d}:"
                      f"{int(s % 60):02d},{int(round(s % 1 * 1000)):03d}")


# ── 各导出物 ────────────────────────────────────────────────────────────

def build_script_json(project: dict, timeline: dict) -> dict:
    meta = project["meta"]
    segs = []
    for seg in project["segments"]:
        sents = model.sentences_of(project, seg["id"])
        segs.append({
            "id": seg["id"],
            "title": seg.get("title", ""),
            "narration": "".join(s["text"] for s in sents),
            "shots": seg.get("shots") or [],
        })
    return {
        "project": meta.get("slug") or meta.get("title", "untitled"),
        "page": meta.get("page", ""),
        "viewport": meta.get("viewport") or {"w": 1600, "h": 900, "dpr": 1.5},
        "annotationColor": meta.get("annotationColor", "#e8590c"),
        "segments": segs,
    }


def build_srt(project: dict, timeline: dict) -> str:
    t = {x["i"]: x for x in timeline["sentences"]}
    out = []
    for n, s in enumerate(project["sentences"], 1):
        tt = t.get(s["i"])
        if not tt:
            continue
        out.append(f"{n}\n{SRT_TIME(tt['start'])} --> {SRT_TIME(tt['end'])}\n{s['text']}\n")
    return "\n".join(out)


def build_md(project: dict, timeline: dict) -> str:
    t = {x["i"]: x for x in timeline["sentences"]}
    lines = [f"# {project['meta'].get('title', '')} 文案脚本（逐句）", "",
             "> 改文案后把整表返回，改动的单元会重新配音并重合成。", "",
             "| 段 | 句# | 文案 | 起 | 止 |", "|---|---|---|---|---|"]
    for s in project["sentences"]:
        tt = t.get(s["i"], {"start": 0, "end": 0})
        lines.append(f"| {s['sid']} | {s['i']} | {s['text']} | "
                     f"{tt['start']:.1f}s | {tt['end']:.1f}s |")
    return "\n".join(lines) + "\n"


def build_txt(project: dict, timeline: dict) -> str:
    """纯文本版：没有时间码、没有表格符号 —— 用来复制、发送、存档。

    与 `文案脚本.md` 的分工很清楚：
      * `文案脚本.md` 是**带时间码的核对表**（对着时间轴逐句核）；
      * `文案脚本.txt` 是**干净的文案本体**（拿出去用，读者不需要看到时间）。
    """
    out = []
    title = project["meta"].get("title", "")
    if title:
        out += [title, ""]
    for seg in project["segments"]:
        sents = [x for x in project["sentences"] if x["sid"] == seg["id"]]
        if not sents:
            continue
        out.append(f"〔{seg['id']}〕{seg.get('title') or ''}".rstrip())
        out += [x["text"] for x in sents]
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def build_units_jsonl(project: dict) -> str:
    rows = [json.dumps({"text": u["text"]}, ensure_ascii=False)
            for u in model.plan_all(project)]
    return "\n".join(rows) + "\n"


def build_manifest(project: dict, timeline: dict) -> dict:
    meta = project["meta"]
    tl_seg = {s["id"]: s for s in timeline["segments"]}
    segs = []
    for seg in project["segments"]:
        t = tl_seg.get(seg["id"], {})
        n_shots = max(1, len(seg.get("shots") or []) or 1)
        segs.append({
            "id": seg["id"], "title": seg.get("title", ""),
            "start": t.get("start"), "end": t.get("end"), "duration": t.get("duration"),
            "shotCount": len(seg.get("shots") or []),
            "shotDuration": round((t.get("duration") or 0) / n_shots, 4) if n_shots else None,
            "shots": seg.get("shots") or [],
            "units": [{"uid": u["uid"], "start": u["start"], "end": u["end"],
                       "duration": u["duration"], "chars": None, "text": u["text"]}
                      for u in t.get("units", [])],
        })
    return {
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "title": meta.get("title", ""),
        "canvas": meta.get("canvas"),
        "fps": (meta.get("canvas") or {}).get("fps", 30),
        "gapBetweenSegments": meta.get("gapBetweenSegments"),
        "gapBetweenUnits": meta.get("gapBetweenUnits"),
        "subtitle": meta.get("subtitle"),
        "totalDuration": timeline["total"],
        "stale": timeline["stale"],
        "segments": segs,
    }


# ── 导出 ────────────────────────────────────────────────────────────────

def export_simple(project: dict, index: dict, work_dir: Path, format='text', uid=None, export_root=None) -> dict:
    """Explicit MVP deliveries; never export stale audio or estimated subtitles."""
    if format not in ('text', 'audio', 'selected'):
        raise ValueError('不支持的导出格式')
    plan = [unit for unit in model.plan_all(project) if unit['text'].strip()]
    if format == 'selected':
        plan = [unit for unit in plan if unit['uid'] == uid]
        if not plan:
            raise ValueError('请先选择要导出的段落')
    parts = []
    if format != 'text':
        if not plan:
            raise ValueError('文稿没有可导出的声音')
        for unit in plan:
            rec = index['units'].get(unit['uid'], {})
            audio = rec.get('audio')
            path = Path(audio) if audio else None
            if path and not path.is_absolute():
                path = work_dir / path
            if rec.get('status') != 'clean' or rec.get('hash') != unit['hash'] or not path or not path.is_file():
                raise ValueError('旁白未覆盖或已过期：' + unit['uid'] + '。请更新声音，或明确选择已就绪段落导出')
            parts.append(path)
        ffmpeg = shutil.which('ffmpeg')
        if not ffmpeg:
            raise ValueError('本机缺少 FFmpeg，暂不能导出 WAV')
    destination = Path(export_root or work_dir / 'exports') / (time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8])
    destination.mkdir(parents=True)
    files = []
    try:
        if format == 'text':
            text = build_txt(project, {})
            md = ['# ' + project['meta'].get('title', '文稿'), '']
            for segment in project['segments']:
                sentences = model.sentences_of(project, segment['id'])
                if not sentences:
                    continue
                md.extend(['## ' + (segment.get('title') or segment['id']), ''])
                md.extend(sentence['text'] + '\n' for sentence in sentences)
            for name, content in (('文案.txt', text), ('文案.md', '\n'.join(md))):
                output = destination / name
                output.write_text(content, encoding='utf-8')
                files.append(str(output))
            duration = None
        else:
            output = destination / '旁白.wav'
            command = [ffmpeg, '-v', 'error', '-nostdin']
            for part in parts:
                command.extend(['-i', str(part)])
            filters = [f'[{i}:a]aresample=24000,aformat=sample_fmts=s16:channel_layouts=mono[a{i}]' for i in range(len(parts))]
            filters.append(''.join(f'[a{i}]' for i in range(len(parts))) + f'concat=n={len(parts)}:v=0:a=1[out]')
            command.extend(['-filter_complex', ';'.join(filters), '-map', '[out]', '-c:a', 'pcm_s16le', str(output)])
            subprocess.run(command, check=True, capture_output=True, timeout=120)
            with wave.open(str(output), 'rb') as wav:
                duration = wav.getnframes() / wav.getframerate()
            files.append(str(output))
        manifest = destination / '交付记录.json'
        manifest.write_text(json.dumps({'schemaVersion': 1, 'projectId': project['meta']['projectId'],
            'revision': project['meta']['revision'], 'format': format, 'duration': duration,
            'timing': 'adopted-audio-concatenation-no-gaps' if parts else None,
            'units': [{'uid': unit['uid'], 'hash': unit['hash']} for unit in plan] if parts else []}, ensure_ascii=False, indent=2), encoding='utf-8')
        files.append(str(manifest))
        return {'ok': True, 'dir': str(destination), 'files': files, 'duration': duration, 'format': format}
    except Exception:
        # This UUID directory was created by this call and contains no user input.
        shutil.rmtree(destination)
        raise

def export_all(project: dict, timeline: dict, out_dir: Path) -> dict:
    # Compatibility exports must obey the same no-overwrite delivery boundary.
    out_dir = Path(out_dir) / (time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8])
    out_dir.mkdir(parents=True)
    (out_dir / "tts").mkdir(parents=True, exist_ok=True)
    files = {
        "script.json": json.dumps(build_script_json(project, timeline),
                                  ensure_ascii=False, indent=2),
        "subs.srt": build_srt(project, timeline),
        "文案脚本.md": build_md(project, timeline),
        "文案脚本.txt": build_txt(project, timeline),
        "manifest.json": json.dumps(build_manifest(project, timeline),
                                    ensure_ascii=False, indent=2),
        "tts/batch.jsonl": build_units_jsonl(project),
    }
    written = []
    for name, content in files.items():
        p = out_dir / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        written.append(str(p))
    return {"ok": True, "dir": str(out_dir), "files": written}
