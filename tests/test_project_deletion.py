"""Isolated lifecycle checks; never writes production project contents."""
import json
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path
from http.server import ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import service
from project_library import Library


def request(url, value=None):
    data = json.dumps(value).encode() if value is not None else None
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data,
                headers={'Content-Type': 'application/json'})) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)


with tempfile.TemporaryDirectory(prefix='script-editor-delete-') as scratch:
    root = Path(scratch) / 'projects'
    root.mkdir()
    service.LIBRARY = Library(root, root / 'library.json')
    base = root / 'test-id'
    ws = service.Workspace(base)
    project = ws.save_project({'meta': {'title': '删除测试'}, 'segments': [], 'sentences': []})
    service.LIBRARY.register(base, project, patch={'trashed': True})
    (base / 'media').mkdir()
    (base / 'media' / 'original.wav').write_bytes(b'original media')
    server = ThreadingHTTPServer(('127.0.0.1', 0), service.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = 'http://127.0.0.1:%s' % server.server_port
    from urllib.parse import quote
    endpoint = url + '/api/project/delete?dir=' + quote(str(base))
    status, _ = request(endpoint, {'confirm': False})
    assert status == 400 and (base / 'media' / 'original.wav').exists()
    status, _ = request(endpoint, {'confirm': True, 'title': '错误名称'})
    assert status == 409 and base.exists()
    status, _ = request(endpoint, {'confirm': True, 'title': '删除测试'})
    assert status == 200 and not base.exists(), (status, _)
    assert not service.LIBRARY._read()
    assert service.LIBRARY.is_deleted(base)
    status, _ = request(url + '/api/project?dir=' + quote(str(base)), project)
    assert status == 409 and not base.exists(), (status, _)
    try:
        ws.save_project(project)
        raise AssertionError('stale Workspace recreated deleted project')
    except service.RevisionConflict:
        pass
    # Restarting the catalogue retains protection.
    assert Library(root, root / 'library.json').is_deleted(base)
    outside = Path(scratch) / 'external'
    external = service.Workspace(outside)
    p = external.save_project({'meta': {'title': '外部项目'}, 'segments': [], 'sentences': []})
    status, _ = request(url + '/api/project/delete?dir=' + quote(str(outside)),
                        {'confirm': True, 'title': '外部项目'})
    assert status == 400 and external.project_file.exists()
    server.shutdown()
    server.server_close()
    print('PASS: confirmation, title, full directory deletion, stale HTTP/object writes, persisted protection, external path guard')
