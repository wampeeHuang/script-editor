"""Unit checks for fail-closed structure validation and protected cache cleanup."""
import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, file):
    spec = importlib.util.spec_from_file_location(name, file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


workspace = load_module('workspace_check', ROOT / 'checks/workspace.py')
cleaner = load_module('runtime_cleaner', ROOT / 'checks/clean_runtime.py')


class WorkspaceChecks(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix='script-editor-structure-')
        self.root = Path(self.scratch.name)
        self.root_patch = patch.object(workspace, 'ROOT', self.root)
        self.root_patch.start()
        (self.root / 'checks').mkdir()
        (self.root / '_runtime').mkdir()
        (self.root / 'projects').mkdir()
        (self.root / 'README.md').write_text(workspace.START + '\n' + workspace.END, encoding='utf-8')
        config = {'entries': {x: x for x in ['checks', '_runtime', 'projects', 'README.md']},
                  'required': ['checks', '_runtime', 'projects'], 'runtimeEntries': ['logs']}
        (self.root / 'checks/layout.json').write_text(json.dumps(config), encoding='utf-8')

    def tearDown(self):
        self.root_patch.stop()
        self.scratch.cleanup()

    def test_declared_layout_passes(self):
        with contextlib.redirect_stdout(io.StringIO()):
            workspace.check()

    def test_unknown_root_fails(self):
        (self.root / 'patch_v99.py').write_text('pass')
        with self.assertRaisesRegex(SystemExit, '未声明一级入口'):
            workspace.check()

    def test_source_in_runtime_fails(self):
        (self.root / '_runtime/test.py').write_text('pass')
        with self.assertRaisesRegex(SystemExit, '运行仓用途未声明'):
            workspace.check()

    def test_orphan_project_fails(self):
        (self.root / 'projects/deleted-id').mkdir()
        with self.assertRaisesRegex(SystemExit, '草稿箱残留非项目目录'):
            workspace.check()

    def test_map_generation_and_drift(self):
        with patch.object(sys, 'argv', ['workspace.py', 'map']), contextlib.redirect_stdout(io.StringIO()):
            workspace.main()
        with patch.object(sys, 'argv', ['workspace.py', 'map', '--check']), contextlib.redirect_stdout(io.StringIO()):
            workspace.main()
        (self.root / 'extra.txt').write_text('unexpected')
        with patch.object(sys, 'argv', ['workspace.py', 'map', '--check']):
            with self.assertRaisesRegex(SystemExit, 'drift'):
                workspace.main()

    def test_cleaner_preview_apply_and_protected_data(self):
        cache = self.root / '__pycache__'
        cache.mkdir()
        (cache / 'test.pyc').write_bytes(b'cache')
        original = self.root / 'projects/original.wav'
        original.write_bytes(b'user content')
        with patch.object(cleaner, 'ROOT', self.root), patch.object(sys, 'argv', ['clean_runtime.py']), contextlib.redirect_stdout(io.StringIO()):
            cleaner.main()
        self.assertTrue(cache.exists())
        with patch.object(cleaner, 'ROOT', self.root), patch.object(sys, 'argv', ['clean_runtime.py', '--apply']), contextlib.redirect_stdout(io.StringIO()):
            cleaner.main()
        self.assertFalse(cache.exists())
        self.assertEqual(original.read_bytes(), b'user content')

    def test_cleaner_refuses_unknown_cache_content(self):
        cache = self.root / '__pycache__'
        cache.mkdir()
        (cache / 'important.txt').write_text('not cache')
        with patch.object(cleaner, 'ROOT', self.root), patch.object(sys, 'argv', ['clean_runtime.py', '--apply']):
            with self.assertRaisesRegex(SystemExit, 'unexpected cache contents'):
                cleaner.main()
        self.assertTrue((cache / 'important.txt').exists())


if __name__ == '__main__':
    unittest.main()
