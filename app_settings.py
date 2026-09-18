"""Application storage preferences, not per-project metadata."""
import json
import threading
from pathlib import Path
from project_library import atomic_json


class Settings:
    def __init__(self, file, project_root, export_root):
        self.file = Path(file)
        self.defaults = {'projectRoot': str(Path(project_root).resolve()),
                         'exportRoot': str(Path(export_root).resolve())}
        self.lock = threading.RLock()

    def get(self):
        with self.lock:
            if not self.file.exists():
                return dict(self.defaults)
            data = json.loads(self.file.read_text(encoding='utf-8'))
            return {key: data.get(key, value) for key, value in self.defaults.items()}

    def save(self, data):
        with self.lock:
            result = self.get()
            for key in self.defaults:
                value = str(data.get(key, result[key])).strip()
                path = Path(value)
                if not value or not path.is_absolute() or path.resolve() == Path(path.anchor):
                    raise ValueError('请填写完整的本地文件夹路径，不可使用盘符根目录')
                if path.exists() and not path.is_dir():
                    raise ValueError('位置必须是文件夹，不能是文件')
                result[key] = str(path.resolve())
            atomic_json(self.file, {'schemaVersion': 1, **result})
            return result
