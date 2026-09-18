"""文案脚本工作台 · HTTP 服务

    python service.py                 # 监听 http://127.0.0.1:8771
    python service.py --port 8780
    python service.py --no-browser

设计：服务基本无状态 —— 真值（project.json）由前端持有并随请求传入，
服务只负责「合成 / 对齐 / 补丁 / 导出 / 指令队列」这些必须落地的动作。
所有写操作都落在工作目录 <dir>/narration/ 下（PRD §7）。

端点一览见 PRD §7。安全：密钥只从环境变量读，绝不回显（验收 A7）。
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import mimetypes
import os
import shutil
import subprocess
import threading
import time
import traceback
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import exporter
import model
import tts
import material_import
from project_library import Library, atomic_json
from app_settings import Settings

ROOT = Path(__file__).resolve().parent
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
LIBRARY_ROOT = Path(os.environ.get('SCRIPT_EDITOR_LIBRARY_ROOT') or ROOT / 'projects')
SETTINGS = Settings(ROOT / 'settings.json', LIBRARY_ROOT, ROOT / 'exports')
LIBRARY_ROOT = Path(SETTINGS.get()['projectRoot'])
LIBRARY = Library(LIBRARY_ROOT, ROOT / 'projects' / 'library.json')
PROJECT_LOCKS = {}
PROJECT_LOCKS_GUARD = threading.Lock()


class RevisionConflict(ValueError):
    pass


# ── 工作目录 ────────────────────────────────────────────────────────────

class Workspace:
    """<dir>/narration/ 下的项目目录。dir 默认是视频项目的 _runtime 或工具自身。"""

    def __init__(self, base: str | Path):
        self.base = Path(base)
        if LIBRARY.is_deleted(self.base) or (self.base / '.deleted-script.json').exists():
            raise RevisionConflict('项目已彻底删除，拒绝写回旧目录')
        if (self.base / '.moved-project.json').is_file():
            raise RevisionConflict('项目已迁入草稿箱，请从项目列表重新打开；拒绝写回旧位置')
        self.root = self.base / "narration"
        self.project_file = self.root / "project.json"
        self.index_file = self.root / "index.json"
        with PROJECT_LOCKS_GUARD:
            self.lock = PROJECT_LOCKS.setdefault(str(self.base.resolve()).casefold(), threading.RLock())

    def ensure(self) -> "Workspace":
        if LIBRARY.is_deleted(self.base) or (self.base / '.deleted-script.json').exists():
            raise RevisionConflict('项目已彻底删除，拒绝写回旧目录')
        for d in ("units", "preview", "exports", "backup"):
            (self.root / d).mkdir(parents=True, exist_ok=True)
        return self

    def load_project(self) -> dict:
        with self.lock:
            return self._load_project()

    def _load_project(self) -> dict:
        if self.project_file.exists():
            raw = json.loads(self.project_file.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or not isinstance(raw.get('meta'), dict) or not isinstance(raw.get('segments'), list) or not isinstance(raw.get('sentences'), list):
                raise ValueError('项目格式无效：需要 meta、segments 和 sentences')
            if raw['meta'].get('schemaVersion', 1) != 1:
                raise ValueError('项目版本不受支持，请勿用旧版工作台覆盖')
            if any(not isinstance(row, dict) for row in raw['segments'] + raw['sentences']):
                raise ValueError('项目段落或文稿格式无效')
            project = model.normalize_project(
                raw)
            if not project["meta"].get("createdAt"):
                stat = self.project_file.stat()
                birth = getattr(stat, "st_birthtime", None)
                if birth is None and os.name == "nt":
                    birth = stat.st_ctime
                if birth:
                    project["meta"]["createdAt"] = int(birth * 1000)
                    project["meta"]["createdAtSource"] = "project-file"
            project['meta'].setdefault('revision', 0)
            project['meta'].setdefault('schemaVersion', 1)
            migrated = False
            for key in ('projectId', 'createdAt', 'createdAtSource', 'revision', 'schemaVersion'):
                if key not in raw['meta'] and key in project['meta']:
                    raw['meta'][key] = project['meta'][key]
                    migrated = True
            if migrated:
                self._keep_history()
                atomic_json(self.project_file, raw)
            return project
        raise FileNotFoundError('没有找到 narration/project.json，请新建或选择有效项目目录')

    HISTORY_SLOTS = 20

    def _keep_history(self) -> None:
        """把**当前**的 project.json 留一份到 .history/（环形 N 份，不删除任何文件）。

        这样"覆盖"永远可回退 —— 事故当时就是因为没有历史，用户改过的稿子被一次
        误写吃掉了。用环形槽位而不是"写满就删最旧"，是为了避开沙箱里 unlink 被拦。
        """
        if not self.project_file.exists():
            return
        try:
            old = self.project_file.read_text(encoding="utf-8")
        except OSError:
            return
        if not old.strip():
            return
        hist = self.root / ".history"
        hist.mkdir(parents=True, exist_ok=True)
        slots = list(hist.glob('h*.json'))
        free = next((hist / ('h%02d.json' % n) for n in range(self.HISTORY_SLOTS)
                     if not (hist / ('h%02d.json' % n)).exists()), None)
        slot = free or min(slots, key=lambda f: f.stat().st_mtime_ns)
        slot.write_text(old, encoding="utf-8")

    def save_project(self, project: dict) -> dict:
        with self.lock:
            return self._save_project(project)

    def _save_project(self, project: dict) -> dict:
        if not isinstance(project, dict) or not isinstance(project.get('meta'), dict) or not isinstance(project.get('segments'), list) or not isinstance(project.get('sentences'), list):
            raise ValueError('保存需要完整项目数据')
        if project['meta'].get('schemaVersion', 1) != 1:
            raise ValueError('项目版本不受支持')
        self.ensure()
        p = model.normalize_project(project)
        existing_project = self.load_project() if self.project_file.exists() else None
        if existing_project:
            if p['meta'].get('projectId') != existing_project['meta']['projectId']:
                raise RevisionConflict('项目身份不匹配，拒绝覆盖已有项目')
            expected = p['meta'].get('revision')
            current = existing_project['meta'].get('revision', 0)
            if expected != current and not (expected is None and current == 0):
                raise RevisionConflict('项目已有更新，已保留本机草稿；请重新打开并核对后恢复')
            p['meta']['revision'] = current
        else:
            p['meta']['revision'] = 0
        p['meta']['schemaVersion'] = 1
        if not p["meta"].get("createdAt"):
            existing = self.load_project()["meta"] if self.project_file.exists() else {}
            p["meta"]["createdAt"] = existing.get("createdAt") or (int(time.time() * 1000) if not self.project_file.exists() else None)
            p["meta"]["createdAtSource"] = existing.get("createdAtSource") or "project"
        body = json.dumps(p, ensure_ascii=False, indent=2)
        try:
            if self.project_file.exists() and self.project_file.read_text(encoding="utf-8") == body:
                return p                      # 内容没变，别占历史槽位
        except OSError:
            pass
        self._keep_history()
        p['meta']['revision'] += 1
        atomic_json(self.project_file, p)
        return p

    def load_index(self, project: dict) -> dict:
        with self.lock:
            return self._load_index(project)

    def _load_index(self, project: dict) -> dict:
        idx = model.load_index(self.index_file)
        idx = model.sync_index(project, idx)
        # 以磁盘音频文件的真实长度校准时长（按指纹缓存，文件没变则零开销）。
        # 保证「时间轴总长 == 成片音轨长度」，SRT 不与声音漂移。
        changed = model.reconcile_durations(self.root, idx)
        changed += model.gc_index(self.root, idx)     # 只清「文件真的没了」的条目
        if changed:
            model.save_index(self.index_file, idx)
        return idx

    def save_index(self, index: dict) -> None:
        with self.lock:
            self.ensure()
            model.save_index(self.index_file, index)


_CLI_DIR: str | None = None       # --dir 覆盖，优先级最高


def _default_base() -> Path:
    """默认项目基目录。优先级：--dir 参数 > default-project.txt > 工具自身。

    为什么要可配置：工具每次打开都要知道"打开哪个项目"。前端会记住上次用的目录，
    但第一次用 / 换了浏览器 / 清过缓存时，得有个服务端侧的默认值兜底 ——
    否则用户会看到一个空的「未命名」，以为东西丢了。
    """
    if _CLI_DIR:
        return Path(_CLI_DIR)
    f = ROOT / "default-project.txt"
    if f.exists():
        try:
            t = f.read_text(encoding="utf-8").strip()
        except OSError:
            t = ""
        if t and Path(t).exists():
            return Path(t)
    return ROOT


# ── 批量合成作业 ────────────────────────────────────────────────────────

def _run_batch(job_id: str, ws: Workspace, project: dict, backend_id: str,
               uids: list[str] | None) -> None:
    job = JOBS[job_id]
    try:
        index = ws.load_index(project)
        plan = {u["uid"]: u for u in model.plan_all(project)}
        targets = ([plan[u] for u in uids if u in plan] if uids
                   else model.dirty_units(project, index))
        job["total"] = len(targets)
        for u in targets:
            if job.get("cancel"):
                job["status"] = "cancelled"
                break
            job["current"] = {"uid": u["uid"], "text": u["text"][:40]}
            out = model.audio_path_for(ws.root, u["hash"])
            try:
                r = tts.synthesize(project, u.get("backend") or backend_id, u["text"], out,
                                   mode="master")
                model.mark_synthesized(index, u["uid"],
                                       str(out.relative_to(ws.root)).replace("\\", "/"),
                                       r["duration"], backend_id)
                job["done"] += 1
                job["results"].append({"uid": u["uid"], "ok": True,
                                       "duration": r["duration"], "waited": r.get("waited")})
                with ws.lock:
                    current = ws.load_project()
                    latest = ws.load_index(current)
                    latest['cache'][u['hash']] = index['cache'][u['hash']]
                    ws.save_index(model.sync_index(current, latest))
            except Exception as exc:          # 单个失败不拖垮整批
                job["failed"] += 1
                job["results"].append({"uid": u["uid"], "ok": False, "error": str(exc)})
        if job["status"] != "cancelled":
            job["status"] = "done"
    except Exception as exc:
        job["status"] = "error"
        job["error"] = f"{exc}"
        job["trace"] = traceback.format_exc()[-600:]
    finally:
        job["finishedAt"] = time.strftime("%H:%M:%S")


# ── HTTP ────────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    server_version = "ScriptEditor/3.0"

    def log_message(self, fmt, *args):        # 静音默认日志，保留错误
        if not str(args[1] if len(args) > 1 else "").startswith("2"):
            print(f"[{self.log_date_time_string()}] {fmt % args}")

    # ---- 基础 ----
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")

    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _err(self, code: int, msg: str, extra: dict | None = None):
        self._json({"ok": False, "error": msg, **(extra or {})}, code)

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        return json.loads(self.rfile.read(n).decode("utf-8"))

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        self._route("GET")

    def do_HEAD(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    # 会**写磁盘**的端点。它们必须由调用方明确指定项目目录 —— 见 _api 里的守卫。
    WRITE_ENDPOINTS = {"/api/project/delete", "/api/project/reveal", "/api/project", "/api/export", "/api/export/simple", "/api/estimate", "/api/patch", "/api/tts/batch",
                       "/api/agent/request", "/api/history/restore", "/api/recording"}

    # ---- 路由 ----
    def _route(self, method: str):
        u = urlparse(self.path)
        path, q = u.path, parse_qs(u.query)
        try:
            # 守卫：写操作不许静默回落到默认项目目录。
            # 事故记录：api_smoke 调 /api/export 忘了带 dir=，服务端回落到默认目录
            # （真实视频项目），把测试数据写进了真实项目并覆盖掉用户改过的稿子。
            if (method == "POST" and path in self.WRITE_ENDPOINTS
                    and not (q.get("dir") or [""])[0].strip()):
                return self._err(400, f"{path} 是写操作，必须显式带 dir=<项目基目录>"
                                      "（拒绝回落到默认目录，防误写）")
            # Browser-history routes are served by the same local shell; the
            # frontend resolves opaque project IDs from its local registry.
            if path in ("/", "/index.html", "/projects", "/projects/trash", "/open", "/import", "/settings") or path.startswith("/project/"):
                return self._serve_html()
            if path.startswith("/tokens/") or path.startswith("/media/"):
                return self._serve_static(path)
            if path == "/api/health":
                return self._json({"ok": True, **tts.health(), "version": "3.0",
                                   "root": str(_default_base()),
                                   "projectLibraryRoot": SETTINGS.get()['projectRoot']})
            if path == "/audio":
                return self._serve_audio(q)
            if path.startswith("/api/"):
                return self._api(path, method, q)
            self._err(404, f"未知路径 {path}")
        except RevisionConflict as exc:
            self._err(409, str(exc), {'conflict': True})
        except FileNotFoundError as exc:
            self._err(404, str(exc), {'missing': True})
        except ValueError as exc:
            self._err(400, str(exc))
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return  # browser navigation cancelled an in-flight response
        except Exception as exc:
            self._err(500, f"{type(exc).__name__}: {exc}",
                      {"trace": traceback.format_exc()[-500:]})

    def _api(self, path: str, method: str, q: dict):
        if path == '/api/import':
            if method != 'POST':
                return self._err(405, '只允许 POST')
            if int(self.headers.get('Content-Length') or 0) > 92_000_000:
                return self._err(413, '资料合计不能超过 64MB')
            base = material_import.create_import(SETTINGS.get()['projectRoot'], self._read_json())
            imported = Workspace(base)
            project = imported.load_project()
            primary = project['meta'].get('primaryAudio')
            LIBRARY.register(base, project, opened=True, patch={
                'durationSeconds': primary['duration'] if primary else None,
                'durationIsEstimate': not bool(primary)})
            index = imported.load_index(project)
            return self._json({'ok': True, 'base': str(base), 'project': project,
                               **self._derived(project, index, imported)})
        if path == '/api/settings':
            if method == 'POST':
                # Register the old root before changing future project placement.
                LIBRARY.list(lambda base: Workspace(base).load_project())
                settings = SETTINGS.save(self._read_json())
                LIBRARY.root = Path(settings['projectRoot'])
            else:
                settings = SETTINGS.get()
            return self._json({'ok': True, 'settings': settings})
        if path == '/api/library':
            warnings = []
            if method == 'POST':
                body = self._read_json()
                for entry in body.get('projects', [])[:200]:
                    base = entry.get('dir')
                    if not base:
                        raise ValueError('登记项目需要明确目录')
                    try:
                        project = Workspace(base).load_project()
                        LIBRARY.register(base, project, opened=bool(entry.get('opened')), patch=entry)
                    except (OSError, ValueError, TypeError) as error:
                        warnings.append({'dir': base, 'error': str(error)})
            return self._json({'ok': True, 'warnings': warnings, 'projects': LIBRARY.list(lambda base: Workspace(base).load_project())})
        if path == '/api/project/snapshot':
            identity = (q.get('id') or [''])[0]
            base = LIBRARY.resolve(identity)
            project = Workspace(base).load_project()
            return self._json({'ok': True, 'schemaVersion': 1, 'project': project,
                               'revision': project['meta']['revision']})
        ws = Workspace(q.get("dir", [str(_default_base())])[0])

        tombstone = ws.base / ".deleted-script.json"
        if path == "/api/project/reveal":
            if method != "POST":
                return self._err(405, "只允许 POST")
            # Explorer is an external side effect. Only reveal a concrete,
            # existing project base whose narration/project.json is present;
            # never accept a loose path or silently fall back to the default.
            base, target = ws.base.resolve(), ws.root.resolve()
            if (not base.is_dir() or not ws.project_file.is_file()
                    or target != base / "narration" or ws.base.is_symlink()):
                return self._err(400, "拒绝打开非项目目录")
            try:
                subprocess.Popen(["explorer.exe", str(base)])
            except FileNotFoundError:
                return self._err(501, "当前系统不支持打开资源管理器")
            return self._json({"ok": True, "revealed": str(base)})
        if path == "/api/project/delete":
            if method != "POST":
                return self._err(405, "只允许 POST")
            body = self._read_json()
            if body.get("confirm") is not True:
                return self._err(400, "彻底删除需要明确确认")
            if tombstone.exists():
                return self._json({"ok": True, "alreadyDeleted": True})
            base = ws.base.resolve()
            target = base
            if base.parent != LIBRARY.root.resolve() or base == ROOT or ws.base.is_symlink() or (hasattr(ws.base, 'is_junction') and ws.base.is_junction()):
                return self._err(400, "拒绝删除不安全的项目路径")
            if not ws.project_file.is_file():
                return self._err(404, "稿件不存在")
            project = ws.load_project()
            if body.get("title") != project["meta"]["title"]:
                return self._err(409, "稿件名称已变化，请刷新后重新确认")
            if any(job.get("status") == "running" and Path(job.get("dir", ".")).resolve() in (base, ws.root.resolve()) for job in JOBS.values()):
                return self._err(409, "该稿件配音生成中，暂不能删除")
            for item in target.rglob("*"):
                if item.is_symlink() or (hasattr(item, "is_junction") and item.is_junction()):
                    return self._err(400, "项目包含链接目录，拒绝递归删除")
            # Catalogue scans acquire library then workspace locks; do not take
            # the library lock while holding a workspace lock here.
            # Persist protection first; partial failures must not resurrect data.
            LIBRARY.mark_deleted(base, project['meta']['projectId'])
            with ws.lock:
                shutil.rmtree(target)
            LIBRARY.forget(base)
            return self._json({"ok": True, "deleted": str(target)})
        if tombstone.exists():
            return self._err(410, "稿件已彻底删除", {"gone": True})

        # Recorded audio is adopted explicitly, with the text hash captured before
        # recording. Keep original takes; edited text cannot reuse a stale take.
        if path == "/api/recording":
            if method != "POST":
                return self._err(405, "只允许 POST")
            if int(self.headers.get("Content-Length") or 0) > 45_000_000:
                return self._err(413, "录音过大，请控制在 32MB 内")
            body = self._read_json()
            p = ws.load_project()
            idx = ws.load_index(p)
            uid = body.get("uid")
            unit = idx["units"].get(uid)
            if not unit or unit["hash"] != body.get("hash"):
                return self._err(409, "文稿已变化，请重新录音")
            try:
                raw = base64.b64decode(body.get("data", ""), validate=True)
            except (ValueError, TypeError, binascii.Error):
                return self._err(400, "录音数据无效")
            if not raw or len(raw) > 32_000_000:
                return self._err(400, "录音为空或超过 32MB")
            ffmpeg, probe = shutil.which("ffmpeg"), shutil.which("ffprobe")
            if not ffmpeg or not probe:
                return self._err(503, "本机缺少 FFmpeg，暂不能保存录音")
            takes = ws.ensure().root / "recordings"
            takes.mkdir(exist_ok=True)
            take = uuid.uuid4().hex
            original, wav = takes / (take + ".source"), takes / (take + ".wav")
            original.write_bytes(raw)
            try:
                subprocess.run([ffmpeg, "-v", "error", "-i", str(original),
                                "-vn", "-ac", "1", "-ar", "24000", str(wav)],
                               capture_output=True, check=True, timeout=60)
                duration = float(subprocess.check_output(
                    [probe, "-v", "error", "-show_entries", "format=duration",
                     "-of", "default=noprint_wrappers=1:nokey=1", str(wav)],
                    timeout=10))
            except (subprocess.SubprocessError, ValueError):
                return self._err(400, "无法解析录音，请导入有效音频")
            if duration <= 0:
                return self._err(400, "录音没有有效时长")
            with ws.lock:
                current = ws.load_project()
                idx = ws.load_index(current)
                unit = idx["units"].get(uid)
                if not unit or unit["hash"] != body.get("hash"):
                    return self._err(409, "保存期间文稿已变化；录音保留在本地，未采用")
                model.mark_synthesized(idx, uid, "recordings/" + take + ".wav", duration, "recorded")
                ws.save_index(idx)
            return self._json({"ok": True, "duration": duration,
                               **self._derived(current, idx, ws)})

        # ── 助手：交给 Agent 的收件箱 ──
        # 工具里没有内置 AI，这里也不假装有。这个端点只做一件事：把请求落成
        # 项目目录里的文件，让 agent 能直接读 —— 不依赖剪贴板权限，不怕粘错，可追溯。
        if path == "/api/agent/request":
            import time as _t
            _b = self._read_json()
            payload = (_b.get("payload") or "").strip()
            self._body_scope = _b.get("scope") or ""
            if not payload:
                return self._err(400, "没有内容可交给 Agent")
            inbox = ws.root / "agent" / "inbox"
            inbox.mkdir(parents=True, exist_ok=True)
            name = _t.strftime("%Y%m%d-%H%M%S") + ".md"
            (inbox / name).write_text(payload + "\n", encoding="utf-8")
            return self._json({"ok": True, "name": name,
                               "path": str(inbox / name),
                               "scope": getattr(self, "_body_scope", ""),
                               "lines": payload.count("\n") + 1})
        if path == "/api/agent":
            import re as _re
            import time as _t
            inbox = ws.root / "agent" / "inbox"
            items = []
            if inbox.is_dir():
                fs = sorted(inbox.glob("*.md"),
                            key=lambda x: x.stat().st_mtime, reverse=True)
                for f in fs[:20]:
                    try:
                        txt = f.read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        continue
                    m = _re.search(r"^- 范围：(.+)$", txt, _re.M)
                    items.append({
                        "name": f.name,
                        "scope": (m.group(1).strip() if m else ""),
                        "lines": txt.count("\n"),
                        "at": _t.strftime("%m-%d %H:%M", _t.localtime(f.stat().st_mtime)),
                    })
            return self._json({"ok": True, "pending": items})

        # ── 版本历史（环形快照 .history/hNN.json，mtime = 保存时刻）──
        # 后端每次保存都会留一份进 .history/（见 _keep_history），这里把"覆盖"
        # 变成永远可回退。前端提供浏览 + 预览 + 恢复。
        def _snap_meta(p: dict):
            sents = p.get("sentences") or []
            return (len(p.get("segments") or []), len(sents),
                    sum(len(s.get("text", "")) for s in sents))

        if path == "/api/history":
            hist = ws.root / ".history"
            cur_raw = (ws.project_file.read_text(encoding="utf-8")
                       if ws.project_file.exists() else "")
            cur_h = hashlib.md5(cur_raw.encode("utf-8")).hexdigest() if cur_raw.strip() else ""
            snaps = []
            if hist.is_dir():
                for f in sorted(hist.glob("h*.json"),
                                key=lambda x: x.stat().st_mtime, reverse=True):
                    try:
                        raw = f.read_text(encoding="utf-8")
                        p = json.loads(raw)
                    except Exception:
                        continue
                    segs, sents, chars = _snap_meta(p)
                    h = hashlib.md5(raw.encode("utf-8")).hexdigest()
                    snaps.append({
                        "slot": f.stem,
                        "at": time.strftime("%m-%d %H:%M:%S",
                                            time.localtime(f.stat().st_mtime)),
                        "segs": segs, "sents": sents, "chars": chars,
                        "bytes": f.stat().st_size,
                        "current": h == cur_h,
                    })
            return self._json({"ok": True, "snapshots": snaps})

        if path == "/api/history/preview":
            slot = (q.get("slot") or [""])[0].strip()
            if slot not in {'h%02d' % n for n in range(ws.HISTORY_SLOTS)}:
                return self._err(400, '快照标识无效')
            f = (ws.root / ".history" / (slot + ".json")) if slot else None
            if not f or not f.is_file():
                return self._err(404, "快照不存在")
            try:
                p = model.normalize_project(json.loads(f.read_text(encoding="utf-8")))
            except Exception as exc:
                return self._err(400, "快照损坏：" + str(exc))
            segs, sents, chars = _snap_meta(p)
            return self._json({"ok": True, "project": p,
                               "segs": segs, "sents": sents, "chars": chars})

        if path == "/api/history/restore":
            b = self._read_json()
            slot = (b.get("slot") or "").strip()
            if slot not in {'h%02d' % n for n in range(ws.HISTORY_SLOTS)}:
                return self._err(400, '快照标识无效')
            f = (ws.root / ".history" / (slot + ".json")) if slot else None
            if not f or not f.is_file():
                return self._err(404, "快照不存在")
            # 先把"当前这一版"也留进历史 —— 恢复动作本身也能回退。
            try:
                p = model.normalize_project(json.loads(f.read_text(encoding="utf-8")))
            except Exception as exc:
                return self._err(400, "快照损坏：" + str(exc))
            with ws.lock:
                current = ws.load_project()
                p['meta']['projectId'] = current['meta']['projectId']
                p['meta']['revision'] = current['meta']['revision']
                p = ws.save_project(p)
            idx = ws.load_index(p)
            ws.save_index(idx)
            return self._json({"ok": True, "dir": str(ws.root), "project": p,
                               **self._derived(p, idx, ws)})

        # ── 真值读写 ──
        if path == "/api/project":
            if method == "GET":
                p = ws.load_project()
                LIBRARY.register(ws.base, p)
                idx = ws.load_index(p)
                return self._json({"ok": True, "dir": str(ws.root), "project": p,
                                   **self._derived(p, idx, ws)})
            body = self._read_json()
            p = ws.save_project(body.get("project") or {})
            LIBRARY.register(ws.base, p)
            idx = ws.load_index(p)
            ws.save_index(idx)
            return self._json({"ok": True, "dir": str(ws.root), "project": p,
                               **self._derived(p, idx, ws)})

        # ── 无状态试算：不落盘，供前端实时预览 ──
        if path == "/api/split":
            body = self._read_json()
            p = model.normalize_project(body.get("project") or {})
            # 读工作目录里已有的缓存（若有）—— 这样已合成的单元给出真实时长，
            # 只有脏单元才退化为预估。否则前端永远只看到估算值。
            idx = ws.load_index(p)
            return self._json({"ok": True, **self._derived(p, idx, ws)})

        if path == "/api/estimate":
            body = self._read_json()
            p = ws.save_project(body["project"])
            idx = ws.load_index(p)
            ws.save_index(idx)
            return self._json({"ok": True, "project": p,
                               "estimate": model.estimate(p, idx, body.get("backend"))})

        # ── 补丁 ──
        if path == "/api/patch":
            body = self._read_json()
            # 真值由前端持有并随请求传入 —— 不要从磁盘读，否则首次使用（磁盘无
            # project.json）会拿到空项目，补丁里的句号一个都对不上。
            before = model.normalize_project(body["project"]) if body.get("project") \
                else ws.load_project()
            after = model.apply_patch(before, body.get("ops") or [])
            changed = model.diff_changed_segments(before, after)
            p = ws.save_project(after)
            idx = ws.load_index(p)
            ws.save_index(idx)
            return self._json({"ok": True, "project": p, "changed": changed,
                               **self._derived(p, idx, ws)})

        if path == "/api/tts/stream":
            body = self._read_json()
            p = model.normalize_project(body["project"])
            u = next((x for x in model.plan_all(p) if x["uid"] == body["uid"]), None)
            if not u:
                return self._err(404, "找不到播放单元")
            cfg = tts.backend(p, p["meta"].get("previewBackend", "cosyvoice"))
            if cfg.get("kind") != "cosyvoice" or cfg.get("cost") != "free" or cfg.get("enabled") is False:
                return self._err(400, "本机流式预览尚未启用，不会调用付费后端")
            text = u["text"]
            first = body.get("fromSentence")
            if first is not None:
                if first not in u["sentenceIds"]:
                    return self._err(400, "起始句不属于播放单元")
                rows = [x for x in p["sentences"] if x["i"] in u["sentenceIds"] and x["i"] >= first]
                if body.get("sentenceOnly"):
                    rows = [x for x in rows if x["i"] == first]
                texts = [x["text"] for x in rows]
                text = model.unit_text(texts, list(range(len(texts))), True)
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers(); self.close_connection = True
            try:
                for frame in tts.stream_preview(cfg, text):
                    self.wfile.write((json.dumps(frame, ensure_ascii=False) + "\n").encode("utf-8"))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return
            except Exception as e:
                try:
                    self.wfile.write((json.dumps({"type":"error", "error":str(e)}, ensure_ascii=False) + "\n").encode("utf-8"))
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
            return

        # ── 试听（L1，单单元，流式，快）──
        if path == "/api/tts/preview":
            body = self._read_json()
            p = model.normalize_project(body["project"])
            uid = body["uid"]
            u = next((x for x in model.plan_all(p) if x["uid"] == uid), None)
            if not u:
                return self._err(404, f"找不到单元 {uid}")
            backend_id = body.get("backend") or p["meta"].get("previewBackend") or \
                model.resolve_backend_id(p, u["sid"])
            if backend_id == "browser":
                return self._json({"ok": True, "mode": "client",
                                   "reason": "本机朗读在前端完成，不发请求"})
            ws.ensure()
            out = ws.root / "preview" / f"{uid}.mp3"
            r = tts.synthesize(p, backend_id, u["text"], out, mode="preview")
            return self._json({**r, "uid": uid, "backend": backend_id,
                               "url": f"/audio?dir={ws.root}&rel=preview/{uid}.mp3",
                               "chars": u["chars"]})

        # ── 定稿（L2，批量，显式）──
        if path == "/api/tts/batch":
            body = self._read_json()
            p = ws.save_project(body["project"])
            idx = ws.load_index(p)
            ws.save_index(idx)
            backend_id = body.get("backend") or p["meta"].get("defaultBackend") or "doubao"
            uids = body.get("uids")
            est = model.estimate(p, idx, backend_id)
            if not body.get("confirm"):
                return self._json({"ok": True, "project": p, "needConfirm": True, "estimate": est})
            job_id = uuid.uuid4().hex[:12]
            with JOBS_LOCK:
                JOBS[job_id] = {"id": job_id, "status": "running", "total": 0, "done": 0,
                                "failed": 0, "results": [], "dir": str(ws.root),
                                "backend": backend_id, "startedAt": time.strftime("%H:%M:%S")}
            threading.Thread(target=_run_batch, args=(job_id, ws, p, backend_id, uids),
                             daemon=True).start()
            return self._json({"ok": True, "project": p, "jobId": job_id, "estimate": est})

        if path == "/api/job":
            jid = (q.get("id") or [""])[0]
            job = JOBS.get(jid)
            if not job:
                return self._err(404, f"未知作业 {jid}")
            return self._json({"ok": True, "job": job})

        if path == "/api/job/cancel":
            jid = (q.get("id") or [""])[0]
            if jid in JOBS:
                JOBS[jid]["cancel"] = True
            return self._json({"ok": True})

        # ── 指令队列（人机同权，PRD §4.6 / §6.3）──
        # ── 导出 ──
        if path == "/api/export/simple":
            body = self._read_json()
            with ws.lock:
                p = ws.load_project()
                if body.get('revision') != p['meta']['revision']:
                    raise RevisionConflict('文稿版本已变化，请保存或重新打开后再导出')
                idx = ws.load_index(p)
                export_root = str(body.get('destination') or '').strip() or SETTINGS.get()['exportRoot']
                destination = Path(export_root)
                if not destination.is_absolute() or destination.resolve() == Path(destination.anchor):
                    raise ValueError('导出位置需要完整的文件夹路径，不可使用盘符根目录')
                if destination.exists() and not destination.is_dir():
                    raise ValueError('导出位置必须是文件夹，不能是文件')
                result = exporter.export_simple(p, idx, ws.root, body.get('format', 'text'), body.get('uid'), destination)
            return self._json(result)

        if path == "/api/export":
            body = self._read_json()
            # 只读不写：导出不是"保存"。保存由前端的自动保存负责；
            # 这里如果落盘，一次忘带 dir 的调用就能覆盖掉真实项目（真实事故）。
            p = model.normalize_project(body.get("project") or ws.load_project())
            idx = model.sync_index(p, model.load_index(ws.index_file))
            return self._json(exporter.export_all(
                p, model.compute_timeline(p, idx), ws.root / "exports"))


        self._err(404, f"未知接口 {path}")

    # ---- 派生数据（前端每次都拿同一份，保证视图一致）----
    def _derived(self, project: dict, index: dict, ws: "Workspace | None" = None) -> dict:
        tl = model.compute_timeline(project, index)
        return {
            # 音频根目录由服务端给出 —— 前端拿它拼 /audio?dir=…，不要自己拼
            # "/narration"（踩过：前端用工作目录当音频根，播放全部 404）。
            "audioBase": str(ws.root) if ws else None,
            "units": list(index["units"].values()),
            "dirty": [u["uid"] for u in model.dirty_units(project, index)],
            "timeline": tl,
            "estimate": model.estimate(project, index),
            "health": tts.health(project),
        }

    # ---- 静态与音频 ----
    def _serve_html(self):
        f = ROOT / "文案脚本工作台.html"
        if not f.exists():
            return self._send(200, "<h1>文案脚本工作台</h1><p>前端文件尚未生成。"
                                   "API 端点可用，见 /api/health。</p>".encode("utf-8"),
                              "text/html; charset=utf-8")
        self._send(200, f.read_bytes(), "text/html; charset=utf-8")

    def _serve_static(self, path: str):
        f = (ROOT / path.lstrip("/")).resolve()
        if not str(f).startswith(str(ROOT)) or not f.exists() or f.is_dir():
            return self._err(404, "静态资源不存在")
        ct = mimetypes.guess_type(str(f))[0] or "application/octet-stream"
        self._send(200, f.read_bytes(), ct)

    def _serve_audio(self, q: dict):
        base = Path((q.get("dir") or [""])[0]).resolve()
        rel = unquote((q.get("rel") or [""])[0])
        f = (base / rel).resolve()
        if not str(f).startswith(str(base)) or not f.exists():
            return self._err(404, "音频不存在")
        ct = mimetypes.guess_type(str(f))[0] or "audio/wav"
        data = f.read_bytes()
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            try:
                a, b = rng[6:].split("-")
                a = int(a) if a else 0
                b = int(b) if b else len(data) - 1
                b = min(b, len(data) - 1)
                self.send_response(206)
                self.send_header("Content-Type", ct)
                self.send_header("Content-Range", f"bytes {a}-{b}/{len(data)}")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(b - a + 1))
                self._cors()
                self.end_headers()
                return self.wfile.write(data[a:b + 1])
            except Exception:
                pass
        self._send(200, data, ct)


def _pick_port(host: str, want: int, tries: int = 30) -> int:
    """端口被占用时明确报错并顺延，绝不靠 SO_REUSEADDR 悄悄双绑。

    （踩过的坑：Windows 上 allow_reuse_address=1 会让两个进程同时监听同一端口，
      请求随机落到其中一个，症状是"接口返回了别的应用的响应"。）
    """
    import socket
    for p in range(want, want + tries):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind((host, p))          # 不加 SO_REUSEADDR：占用即失败
            if p != want:
                print(f"  ! 端口 {want} 已被占用，已顺延到 {p}")
            return p
        except OSError:
            continue
        finally:
            s.close()
    raise SystemExit(f"  x {host}:{want}~{want + tries - 1} 全部被占用，请用 --port 指定其它端口")


class Srv(ThreadingHTTPServer):
    allow_reuse_address = False        # 关键：禁止双绑


def main():
    ap = argparse.ArgumentParser(description="文案脚本工作台 · 本地服务")
    ap.add_argument("--port", type=int, default=8781)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--dir", default=None,
                    help="默认项目基目录（项目落在它的 narration/ 下）")
    args = ap.parse_args()
    global _CLI_DIR
    _CLI_DIR = args.dir

    port = _pick_port(args.host, args.port)
    base = f"http://{args.host}:{port}"

    h = tts.health()
    print(f"  文案脚本工作台 · 服务 {base}")
    print(f"  工作根目录：{ROOT}")
    print(f"  ffmpeg：{'就绪' if h['ffmpeg'] else '缺失'}   "
          f"豆包密钥：{'就绪' if h['doubaoKey'] else '缺失'}")
    for b in h["backends"]:
        flag = "可用" if b["ready"] else "不可用"
        print(f"    · {b['label']:<20} {b['cost']:<5} auto={str(b['auto']):<5} {flag}"
              f"{'' if b['ready'] else '  -- ' + b['reason']}")
    print(f"  浏览器打开 {base} 即可使用（前端与 API 同源，不必知道端口）")

    srv = Srv((args.host, port), Handler)
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(base + "/")).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  已停止")
        srv.server_close()


if __name__ == "__main__":
    main()
