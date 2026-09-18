"""Preview by default; only delete explicitly declared reproducible cache files."""
import argparse
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    # No recursive traversal of user projects, archives, dependencies or agents.
    targets = [ROOT / '__pycache__', ROOT / 'tests/__pycache__', ROOT / 'checks/__pycache__']
    for target in targets:
        if not target.exists():
            continue
        resolved = target.resolve()
        if target.is_symlink() or (hasattr(target, 'is_junction') and target.is_junction()) or resolved.is_relative_to(ROOT / 'projects') or not resolved.is_relative_to(ROOT):
            raise SystemExit('unsafe cache path: ' + str(target))
        for item in target.rglob('*'):
            if item.is_symlink() or (hasattr(item, 'is_junction') and item.is_junction()):
                raise SystemExit('linked cache entry: ' + str(item))
            if item.is_file() and item.suffix != '.pyc':
                raise SystemExit('unexpected cache contents: ' + str(item))
        print(('REMOVE ' if args.apply else 'PREVIEW ') + str(target))
        if args.apply:
            shutil.rmtree(target)


if __name__ == '__main__':
    main()
