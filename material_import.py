"""Copy selected materials into a new managed project, without touching sources."""
import base64
import binascii
import hashlib
import json
import math
import shutil
import subprocess
import tempfile
from pathlib import Path

import model
from project_library import atomic_json

TEXT = {'.md', '.markdown', '.txt', '.lrc', '.srt'}
AUDIO = {'.mp3', '.wav', '.flac', '.m4a', '.ogg'}
IMAGE = {'.jpg', '.jpeg', '.png', '.webp'}
MAX_TOTAL = 64 * 1024 * 1024


def create_import(root, body):
    files = body.get('files')
    if not isinstance(files, list) or not 1 <= len(files) <= 100:
        raise ValueError('请选择 1–100 个支持的资料文件')
    project = body.get('project')
    if not isinstance(project, dict) or not isinstance(project.get('meta'), dict):
        raise ValueError('导入项目数据无效')
    if not isinstance(project.get('segments'), list) or not isinstance(project.get('sentences'), list):
        raise ValueError('导入项目缺少正文结构')
    decoded, total, mains = [], 0, 0
    for item in files:
        if not isinstance(item, dict):
            raise ValueError('资料列表无效')
        name = str(item.get('name', ''))
        suffix = Path(name).suffix.lower()
        if suffix not in TEXT | AUDIO | IMAGE:
            raise ValueError('暂不支持此文件格式：' + name)
        try:
            data = base64.b64decode(item.get('data', ''), validate=True)
        except (ValueError, TypeError, binascii.Error):
            raise ValueError('文件数据无效：' + name)
        total += len(data)
        if not data or total > MAX_TOTAL or suffix in TEXT and len(data) > 2 * 1024 * 1024:
            raise ValueError('资料为空或超过限制：文字单文件 2MB，资料合计 64MB')
        role = item.get('role', 'attachment')
        if role not in {'script', 'lyrics', 'mainAudio', 'attachment'}:
            raise ValueError('文件角色无效')
        if role == 'mainAudio':
            if suffix not in AUDIO:
                raise ValueError('主音频必须是支持的音频文件')
            mains += 1
        if suffix in AUDIO and role != 'mainAudio':
            raise ValueError('每个项目只允许一个主音频，请移除其他歌曲')
        decoded.append((item, suffix, data))
    if mains > 1:
        raise ValueError('请只保留一个主音频')
    if not mains and not project['sentences']:
        raise ValueError('请添加有正文的文案、歌词或一个主音频')
    p = json.loads(json.dumps(project))
    for key in ('projectId', 'revision', 'createdAt', 'createdAtSource', 'primaryAudio', 'materials'):
        p['meta'].pop(key, None)
    p = model.normalize_project(p)
    p['meta']['kind'] = 'music' if mains else 'script'
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    target = root / p['meta']['projectId']
    staging = Path(tempfile.mkdtemp(prefix='.import-', dir=root))
    try:
        assets = staging / 'narration' / 'assets'
        assets.mkdir(parents=True)
        records = []
        for n, (item, suffix, data) in enumerate(decoded):
            digest = hashlib.sha256(data).hexdigest()
            relative = 'assets/' + str(n + 1) + '-' + digest[:16] + suffix
            destination = staging / 'narration' / relative
            destination.write_bytes(data)
            if hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
                raise ValueError('资料复制校验失败')
            rec = {'name': item['name'], 'sourceRelativePath': str(item.get('relativePath') or item['name']),
                   'path': relative, 'role': item.get('role', 'attachment'),
                   'size': len(data), 'sha256': digest}
            if rec['role'] == 'mainAudio':
                probe = shutil.which('ffprobe')
                if not probe:
                    raise ValueError('本机缺少 ffprobe，暂不能导入音乐')
                try:
                    result = subprocess.run([probe, '-v', 'error', '-protocol_whitelist', 'file,pipe', '-show_entries',
                        'format=duration:stream=codec_type', '-of', 'json', str(destination)],
                        capture_output=True, check=True, timeout=20)
                    info = json.loads(result.stdout)
                    duration = float(info['format']['duration'])
                    if not math.isfinite(duration) or duration <= 0 or not any(
                            s.get('codec_type') == 'audio' for s in info.get('streams', [])):
                        raise ValueError()
                except (subprocess.SubprocessError, ValueError, KeyError):
                    raise ValueError('无法读取有效音频：' + item['name'])
                rec['duration'] = duration
                p['meta']['primaryAudio'] = dict(rec)
            records.append(rec)
        p['meta']['materials'] = records
        atomic_json(staging / 'narration' / 'materials.json', {'schemaVersion': 1, 'files': records})
        # Commit only after every file and the audio are validated. The catalogue
        # ignores dot-prefixed staging directories. No user source is removed.
        atomic_json(staging / 'narration' / 'project.json', p)
        staging.rename(target)
        return target
    except Exception:
        # This directory was created by this call and contains only our copies.
        shutil.rmtree(staging)
        raise
