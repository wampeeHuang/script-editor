"""Rebuildable local project catalogue. Project contents remain in their folders."""
import json
import os
import threading
import time
import uuid
from pathlib import Path


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


class Library:
    def __init__(self, root, catalogue):
        self.root, self.catalogue = Path(root), Path(catalogue)
        self.lock = threading.RLock()

    def _read(self):
        if not self.catalogue.exists():
            return {}
        data = json.loads(self.catalogue.read_text(encoding='utf-8'))
        return data.get('projects', {})

    def _write(self, projects):
        atomic_json(self.catalogue, {'schemaVersion': 1, 'projects': projects})

    def _deleted(self):
        file = self.catalogue.with_name('deleted-projects.json')
        return json.loads(file.read_text(encoding='utf-8')).get('projects', {}) if file.exists() else {}

    def is_deleted(self, base):
        return str(Path(base).resolve()).casefold() in self._deleted()

    def mark_deleted(self, base, identity=None):
        with self.lock:
            records = self._deleted()
            records[str(Path(base).resolve()).casefold()] = {
                'projectId': identity, 'deletedAt': int(time.time() * 1000)}
            atomic_json(self.catalogue.with_name('deleted-projects.json'),
                        {'schemaVersion': 1, 'projects': records})

    def register(self, base, project, opened=False, patch=None):
        with self.lock:
            if self.is_deleted(base):
                raise ValueError('项目已彻底删除，拒绝重新登记旧目录')
            projects = self._read()
            directory = str(Path(base).resolve())
            meta = project['meta']
            identity = meta['projectId']
            old = projects.get(identity, {})
            if old and Path(old['dir']).resolve() != Path(directory):
                if (Path(old['dir']) / 'narration' / 'project.json').exists():
                    raise ValueError('项目 ID 重复：原工程仍存在，请先检查副本，不自动覆盖登记')
            entry = {**old, 'projectId': identity, 'dir': directory,
                     'title': meta.get('title') or '未命名稿件',
                     'createdAt': meta.get('createdAt'),
                     'createdAtSource': meta.get('createdAtSource'), 'available': True}
            if opened:
                entry['at'] = int(time.time() * 1000)
            for key in ('hidden', 'trashed', 'trashedAt', 'durationSeconds', 'durationIsEstimate', 'at'):
                if patch and key in patch:
                    entry[key] = patch[key]
            projects[identity] = entry
            self._write(projects)
            return entry

    def list(self, loader):
        # Scan only the managed root; never recurse through arbitrary external data.
        with self.lock:
            projects = self._read()
            bases = {x['dir'] for x in projects.values()}
            if self.root.exists():
                bases.update(str(x) for x in self.root.iterdir()
                             if not x.name.startswith('.') and x.is_dir() and not x.is_symlink() and (x / 'narration' / 'project.json').is_file())
            for base in sorted(bases):
                try:
                    project = loader(base)
                    identity = project['meta']['projectId']
                    old = projects.get(identity, {})
                    if old and Path(old['dir']).resolve() != Path(base).resolve():
                        continue  # do not silently redirect an existing identity
                    projects[identity] = {**old, 'dir': str(Path(base).resolve()),
                        'projectId': identity, 'title': project['meta'].get('title') or '未命名稿件',
                        'createdAt': project['meta'].get('createdAt'),
                        'createdAtSource': project['meta'].get('createdAtSource'), 'available': True}
                except (OSError, ValueError, TypeError, KeyError) as error:
                    for entry in projects.values():
                        if Path(entry['dir']).resolve() == Path(base).resolve():
                            entry.update(available=False, error=str(error))
            self._write(projects)
            return list(projects.values())

    def resolve(self, identity):
        with self.lock:
            entry = self._read().get(identity)
            if not entry or entry.get('trashed') or entry.get('hidden'):
                raise FileNotFoundError('项目未登记或已移入回收站')
            return entry['dir']

    def forget(self, base):
        with self.lock:
            projects = self._read()
            target = Path(base).resolve()
            self._write({key: value for key, value in projects.items()
                         if Path(value['dir']).resolve() != target})
