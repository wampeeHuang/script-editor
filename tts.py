"""文案脚本工作台 · TTS 后端层

关键设计（PRD §4.2）：**成本策略是后端的属性，不是全局开关。**
    本地/浏览器后端 cost=free 且 auto=true  → 改完可自动重合成
    云端后端       cost=paid 且 auto=false → 必须显式批量触发（定稿确认窗）

已实现：
    doubao  豆包语音合成大模型（云端）
            preview → 双向流式，首包 ~0.4s，产物 mp3（试听用，不留档）
            master  → submit/query，产物 wav（定稿用，落档）

已声明但未接线（可在 project.json 里补 command 模板后启用）：
    browser  本机朗读 —— 前端 speechSynthesis，不经过服务
    indextts D:\\tools\\One-Click-VidGen\\tools\\IndexTTS25（本地、可克隆音色、免费）
    cosyvoice D:\\tools\\cosyvoice3（本地、免费）

安全：密钥只从环境变量读，绝不写入任何文件（验收 A7）。
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import requests

DOUBAO_SUBMIT = "https://openspeech.bytedance.com/api/v3/tts/submit"
DOUBAO_QUERY = "https://openspeech.bytedance.com/api/v3/tts/query"
DOUBAO_STREAM = "https://openspeech.bytedance.com/api/v3/tts/unidirectional"

# 已验证可用组合（2026-09-16 实测）。resourceId 与 speaker 必须匹配，否则报
# 55000000 "resource ID is mismatched with speaker related resource"。
DOUBAO_VERIFIED = {"resourceId": "seed-tts-2.0", "speaker": "zh_female_xiaohe_uranus_bigtts"}


def default_backends() -> list[dict]:
    """后端注册表默认值，注入 project.json 的 meta.backends。"""
    return [
        {"id": "browser", "label": "本机朗读", "kind": "browser",
         "cost": "free", "auto": True, "keep": False, "enabled": True,
         "note": "浏览器 speechSynthesis，免费，仅试听不留档；可用声音取决于浏览器"},
        {"id": "doubao", "label": "豆包 · 小何", "kind": "doubao",
         "cost": "paid", "auto": False, "keep": True, "enabled": True,
         "pricePer1kChars": 0, "currency": "CNY",
         "speechRate": 0, "resourceId": DOUBAO_VERIFIED["resourceId"],
         "speaker": DOUBAO_VERIFIED["speaker"],
         "note": "云端，按字符计费。单价由你填 —— 工具不猜，预估金额按你填的口径算"},
    ]


def backend(project: dict, backend_id: str) -> dict:
    for b in project["meta"].get("backends", []):
        if b["id"] == backend_id:
            return b
    raise ValueError(f"未知后端：{backend_id}")


def ffprobe_duration(path: str | Path) -> float | None:
    probe = shutil.which("ffprobe")
    if not probe:
        return None
    r = subprocess.run([probe, "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True, errors="replace")
    try:
        return round(float(r.stdout.strip()), 3)
    except ValueError:
        return None


def _api_key() -> str:
    k = os.environ.get("DOUBAO_SPEECH_API_KEY")
    if not k:
        raise RuntimeError("环境变量 DOUBAO_SPEECH_API_KEY 未设置")
    return k


def _hdr(cfg: dict, rid: str) -> dict:
    return {"X-Api-Key": _api_key(),
            "X-Api-Resource-Id": cfg.get("resourceId") or DOUBAO_VERIFIED["resourceId"],
            "X-Api-Request-Id": rid,
            "Content-Type": "application/json",
            "X-Control-Require-Usage-Tokens-Return": "true"}


# ── 豆包：定稿（非流式，落档 wav）────────────────────────────────────────

def _doubao_master(cfg: dict, text: str, out: Path, timeout: int = 240) -> dict:
    rid = str(uuid.uuid4())
    body = {"user": {"uid": "script-editor"}, "unique_id": rid, "req_params": {
        "text": text,
        "speaker": cfg.get("speaker") or DOUBAO_VERIFIED["speaker"],
        "audio_params": {"format": "wav", "sample_rate": 24000,
                         "speech_rate": int(cfg.get("speechRate") or 0),
                         "enable_timestamp": True},
        "additions": json.dumps({"disable_markdown_filter": False}, ensure_ascii=False)}}
    d = requests.post(DOUBAO_SUBMIT, headers=_hdr(cfg, rid), json=body,
                      timeout=(10, 60)).json()
    if d.get("code") != 20000000:
        raise RuntimeError(f"提交失败 HTTP code={d.get('code')} {d.get('message')}")
    tid = d["data"]["task_id"]

    t0 = time.time()
    while time.time() - t0 < timeout:
        time.sleep(1.2)
        r = requests.post(DOUBAO_QUERY, headers=_hdr(cfg, str(uuid.uuid4())),
                          json={"task_id": tid}, timeout=(10, 60)).json()
        st = r.get("data", {}).get("task_status")
        if st == 2:
            data = r["data"]
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(requests.get(data["audio_url"], timeout=(10, 120)).content)
            sents = data.get("sentences") or []
            # 时长必须以「音频文件真实长度」为准。豆包返回的 sentences[-1].endTime
            # 是末句语音结束点，不含文件尾部静音 —— 用它会造成时间轴比实际音轨短，
            # 成片里字幕与声音逐段错位（实测差 0.2~0.5s/单元）。
            dur = ffprobe_duration(out) or (round(sents[-1]["endTime"], 3) if sents else None)
            return {"ok": True, "path": str(out), "duration": dur,
                    "format": "wav", "engine": "doubao/master",
                    "sentences": [{"start": s["startTime"], "end": s["endTime"],
                                   "text": s["text"]} for s in sents],
                    "waited": round(time.time() - t0, 1)}
        if st == 3:
            raise RuntimeError(f"合成失败：{r.get('message')}")
    raise RuntimeError(f"合成超时（{timeout}s）")


# ── 豆包：试听（流式，快，mp3）─────────────────────────────────────────

def _doubao_preview(cfg: dict, text: str, out: Path, timeout: int = 90) -> dict:
    body = {"user": {"uid": "script-editor"}, "req_params": {
        "text": text,
        "speaker": cfg.get("speaker") or DOUBAO_VERIFIED["speaker"],
        "audio_params": {"format": "mp3", "sample_rate": 24000},
        "additions": json.dumps({"enable_timestamp": False})}}
    t0 = time.time()
    r = requests.post(DOUBAO_STREAM, headers=_hdr(cfg, str(uuid.uuid4())),
                      json=body, stream=True, timeout=(10, timeout))
    if r.status_code >= 400:
        raise RuntimeError(f"流式合成失败 HTTP {r.status_code}: {r.text[:160]}")

    buf = bytearray()
    first = None
    for line in r.iter_lines():
        if not line:
            continue
        try:
            msg = json.loads(line.decode("utf-8"))
        except Exception:
            continue
        chunk = msg.get("data")
        if isinstance(chunk, str) and chunk:
            if first is None:
                first = round(time.time() - t0, 2)
            try:
                buf += base64.b64decode(chunk)
            except Exception:
                pass
        if msg.get("code") == 20000000:
            break
    if not buf:
        raise RuntimeError("流式合成未返回音频数据")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(bytes(buf))
    return {"ok": True, "path": str(out), "duration": ffprobe_duration(out),
            "format": "mp3", "engine": "doubao/preview",
            "firstChunk": first, "total": round(time.time() - t0, 2)}


# ── 通用 command 后端（本地 TTS 接线位）────────────────────────────────

def _command_backend(cfg: dict, text: str, out: Path) -> dict:
    tpl = cfg.get("command")
    if not tpl:
        raise RuntimeError(f"后端 {cfg['id']} 未配置 command 模板")
    tmp = out.with_suffix(".txt")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(text, encoding="utf-8")
    cmd = (tpl.replace("{script}", cfg.get("script", ""))
              .replace("{text_file}", str(tmp))
              .replace("{out}", str(out)))
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                       errors="replace", timeout=int(cfg.get("timeout") or 900))
    if r.returncode != 0 or not out.exists():
        raise RuntimeError(f"本地后端失败（exit {r.returncode}）：{(r.stderr or r.stdout)[-300:]}")
    return {"ok": True, "path": str(out), "duration": ffprobe_duration(out),
            "format": out.suffix.lstrip("."), "engine": cfg["id"]}


def _cosyvoice_backend(cfg: dict, text: str, out: Path) -> dict:
    """Installed Gradio5 call/SSE API; kept separate from paid master audio cache."""
    from urllib.parse import urlparse
    base = cfg.get("endpoint", "http://127.0.0.1:8000").rstrip("/")
    if urlparse(base).hostname not in ("127.0.0.1", "localhost"):
        raise RuntimeError("本机预览只允许本机地址")
    session = requests.Session()
    session.trust_env = False
    api = base + "/gradio_api"
    try:
        with open(cfg["promptAudio"], "rb") as f:
            upload = session.post(api + "/upload", files={"files": ("prompt.wav", f, "audio/wav")}, timeout=15)
        upload.raise_for_status()
        prompt = {"path": upload.json()[0], "meta": {"_type": "gradio.FileData"}}
        data = [text, "3s极速复刻", "", cfg["promptText"], prompt, None, "", 0, False, 1]
        call = session.post(api + "/call/generate_audio", json={"data": data}, timeout=15)
        call.raise_for_status()
        output = None
        with session.get(api + "/call/generate_audio/" + call.json()["event_id"], stream=True, timeout=180) as response:
            response.raise_for_status()
            event = ""
            for raw in response.iter_lines():
                line = raw.decode("utf-8")
                if line.startswith("event: "):
                    event = line[7:]
                elif line.startswith("data: "):
                    value = json.loads(line[6:])
                    if event == "error":
                        raise RuntimeError("CosyVoice 推理失败，请查看本机服务日志")
                    if event in ("generating", "complete") and value:
                        output = value[0]
        if not output:
            raise RuntimeError("CosyVoice 未返回音频")
        audio_url = base + "/gradio_api/file=" + output["path"].replace("\\", "/")
        if urlparse(audio_url).hostname not in ("127.0.0.1", "localhost"):
            raise RuntimeError("本机音频返回地址异常")
        wav = out.with_suffix(".source.wav")
        wav.parent.mkdir(parents=True, exist_ok=True)
        result = session.get(audio_url, timeout=30)
        result.raise_for_status(); wav.write_bytes(result.content)
        duration = ffprobe_duration(wav)
        if not duration or duration < .3:
            raise RuntimeError("CosyVoice 返回音频过短或无法读取")
        subprocess.run([shutil.which("ffmpeg") or "ffmpeg", "-y", "-i", str(wav), str(out)], capture_output=True, check=True)
        return {"ok": True, "path": str(out), "duration": duration, "format": out.suffix.lstrip("."), "engine": "cosyvoice"}
    except requests.RequestException as e:
        raise RuntimeError("本机 CosyVoice 服务未就绪或请求失败；请从工具架启动 CosyVoice 3") from e
    finally:
        session.close()


def stream_preview(cfg: dict, text: str):
    """Only the local free backend can be used by continuous preview."""
    from urllib.parse import urlparse
    if cfg.get("kind") != "cosyvoice" or cfg.get("cost") != "free" or cfg.get("enabled") is False:
        raise RuntimeError("连续预览需要启用本机 CosyVoice，不会自动调用付费后端")
    endpoint = cfg.get("endpoint", "http://127.0.0.1:8000").rstrip("/")
    if urlparse(endpoint).hostname not in ("127.0.0.1", "localhost"):
        raise RuntimeError("本机预览地址异常")
    session = requests.Session(); session.trust_env = False
    try:
        with session.post(endpoint + "/script-editor/stream", json={"text":text}, stream=True, timeout=(5,180)) as response:
            response.raise_for_status()
            for line in response.iter_lines(chunk_size=1024):
                if line:
                    yield json.loads(line)
    finally:
        session.close()


# ── 统一入口 ────────────────────────────────────────────────────────────

def synthesize(project: dict, backend_id: str, text: str, out: Path,
               mode: str = "master") -> dict:
    """合成单段文本。mode: master（落档，wav）/ preview（试听，mp3）。"""
    if not (text or "").strip():
        raise ValueError("合成文本为空")
    cfg = backend(project, backend_id)
    kind = cfg.get("kind", "doubao")
    out = Path(out)

    if cfg.get("enabled") is False:
        raise RuntimeError(f"后端「{cfg.get('label', backend_id)}」尚未启用：{cfg.get('note', '')}")

    if kind == "browser":
        raise RuntimeError("本机朗读在前端完成，不经过服务")

    if kind == "doubao":
        return _doubao_master(cfg, text, out) if mode == "master" \
            else _doubao_preview(cfg, text, out)
    if kind == "cosyvoice":
        return _cosyvoice_backend(cfg, text, out)
    if kind == "command":
        return _command_backend(cfg, text, out)
    raise ValueError(f"未知后端类型：{kind}")


def health(project: dict | None = None) -> dict:
    """后端健康检查（§6.3 ②）。只报密钥是否就绪，绝不回显其值（A7）。"""
    out = {"ffmpeg": bool(shutil.which("ffmpeg")), "ffprobe": bool(shutil.which("ffprobe")),
           "doubaoKey": bool(os.environ.get("DOUBAO_SPEECH_API_KEY")), "backends": []}
    list_ = (project or {}).get("meta", {}).get("backends") or default_backends()
    for b in list_:
        item = {"id": b["id"], "label": b.get("label", b["id"]), "cost": b.get("cost"),
                "auto": b.get("auto"), "enabled": b.get("enabled", True),
                "ready": False, "reason": ""}
        if b.get("kind") == "doubao":
            item["ready"] = out["doubaoKey"]
            item["reason"] = "" if out["doubaoKey"] else "缺少环境变量 DOUBAO_SPEECH_API_KEY"
        elif b.get("kind") == "browser":
            item["ready"] = True
        elif b.get("kind") == "cosyvoice":
            try:
                response = requests.get(b.get("endpoint", "http://127.0.0.1:8000") + "/config", timeout=2, proxies={"http": None, "https": None})
                item["ready"] = response.ok and any(d.get("api_name") == "generate_audio" for d in response.json().get("dependencies", []))
            except Exception:
                pass
            item["reason"] = "" if item["ready"] else "请从工具架启动本机 CosyVoice 3"
        elif b.get("kind") == "command":
            script = b.get("script")
            item["ready"] = bool(script and Path(script).exists() and b.get("enabled"))
            if not item["ready"]:
                item["reason"] = "未启用或脚本路径不存在"
        out["backends"].append(item)
    return out
