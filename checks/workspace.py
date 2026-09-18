"""Fail-closed layout check and one generated directory map."""
import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
START, END = '<!-- DIRECTORY-MAP:START -->', '<!-- DIRECTORY-MAP:END -->'


def contract():
    return json.loads((ROOT / 'checks/layout.json').read_text(encoding='utf-8'))


def map_text():
    entries = contract()['entries']
    lines = [START, '```text', ROOT.name + '/']
    for item in sorted(ROOT.iterdir(), key=lambda p: (not p.is_dir(), p.name.casefold())):
        if item.name == '.git':
            continue
        lines.append('  ' + item.name + ('/' if item.is_dir() else '') +
                     ' — ' + entries.get(item.name, '未声明入口，门禁失败'))
    return '\n'.join(lines + ['```', END])


def check():
    config = contract()
    errors = []
    for item in ROOT.iterdir():
        if item.name != '.git' and item.name not in config['entries']:
            errors.append('未声明一级入口：' + item.name)
    for name in config['required']:
        if not (ROOT / name).exists():
            errors.append('缺少必配入口：' + name)
    for item in (ROOT / '_runtime').iterdir():
        if item.name not in config['runtimeEntries']:
            errors.append('运行仓用途未声明：' + item.name)
    projects = ROOT / 'projects'
    for item in projects.iterdir():
        if item.is_dir() and not (item / 'narration/project.json').is_file():
            errors.append('草稿箱残留非项目目录：' + item.name)
    catalogue = projects / 'library.json'
    if catalogue.exists():
        for identity, entry in json.loads(catalogue.read_text(encoding='utf-8')).get('projects', {}).items():
            file = Path(entry['dir']) / 'narration/project.json'
            if entry.get('available', True) and not file.is_file():
                errors.append('可用登记缺少项目：' + identity)
            elif file.is_file():
                actual = json.loads(file.read_text(encoding='utf-8'))['meta']['projectId']
                if identity != actual:
                    errors.append('登记与项目 ID 不一致：' + identity)
    if errors:
        raise SystemExit('\n'.join(errors))
    print('PASS: declared roots, runtime lifecycle, project folders and registered identities')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['check', 'map'])
    parser.add_argument('--check', action='store_true', dest='verify_map')
    args = parser.parse_args()
    if args.action == 'check':
        check()
        return
    file = ROOT / 'README.md'
    contents = file.read_text(encoding='utf-8')
    if contents.count(START) != 1 or contents.count(END) != 1:
        raise SystemExit('README map markers missing or duplicated')
    before, rest = contents.split(START, 1)
    old, after = rest.split(END, 1)
    generated = map_text()
    if args.verify_map:
        if START + old + END != generated:
            raise SystemExit('directory map drift; run python checks/workspace.py map')
        print('PASS: README map matches disk')
    else:
        file.write_text(before + generated + after, encoding='utf-8')
        print('Updated README directory map from disk')


if __name__ == '__main__':
    main()
