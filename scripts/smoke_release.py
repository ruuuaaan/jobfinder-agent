"""Run portable-package checks without user data, model credentials, or network."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    manifest = json.loads((ROOT / 'MANIFEST.sha256.json').read_text(encoding='utf-8'))
    for name, digest in manifest.items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest, name
    skill = ROOT / 'jobfinder/SKILL.md'
    if skill.exists():
        with tempfile.TemporaryDirectory(prefix='jobfinder space ') as tmp:
            work = Path(tmp) / 'work'
            cmd = [sys.executable, str(skill.parent/'scripts/run.py'), '--workspace', str(work)]
            env = os.environ.copy()
            env.pop('PYTHONPATH', None)
            env.pop('JOBFINDER_WORKSPACE', None)
            for args in (['init'], ['offline','--config','configs/offline.synthetic.json',
                         '--candidates','tests/fixtures/candidates.synthetic.json',
                         '--output','outputs/test/smoke','--allow-example']):
                p = subprocess.run(cmd+args, cwd=tmp, env=env, capture_output=True, text=True, encoding='utf-8', errors='replace')
                if p.returncode:
                    raise RuntimeError(p.stderr + p.stdout[-2000:])
            from openpyxl import load_workbook
            book = load_workbook(work/'outputs/test/smoke/岗位汇总.xlsx')
            assert book.active.max_column == 10
            assert book.active.freeze_panes == 'A2'
            assert book.active.auto_filter.ref.startswith('A1:J')
            print('PASS: relocated skill, independent workspace, offline pipeline, 10-column Excel')
    else:
        sys.path.insert(0, str(ROOT))
        from fastapi.testclient import TestClient
        from jobfinder_web.api import create_app
        from jobfinder_web.rules import current_rules
        from zoneinfo import ZoneInfo
        assert ZoneInfo('Asia/Shanghai')
        assert 'CN_MAINLAND-4.0' in current_rules()['text']
        with tempfile.TemporaryDirectory() as tmp, TestClient(create_app(tmp)) as client:
            assert client.get('/api/health').json()['version'] == '0.3.0-beta.1'
            assert client.get('/api/results').json() == []
            token = client.get('/api/bootstrap').json()['token']
            assert client.post('/api/history/import', json={}, headers={'X-JobFinder-Token':token}).status_code == 400
            assert client.get('/').status_code == 200
            print('PASS: isolated agent, shared rules, timezone, empty startup, API and frontend')


if __name__ == '__main__':
    main()
