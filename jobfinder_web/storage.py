from __future__ import annotations
import json
import os
import sqlite3
import uuid
from copy import deepcopy
from contextlib import contextmanager
from pathlib import Path
from jobfinder.models import now
from jobfinder.spec import task_contract, revise_execution

ROOT = Path(__file__).resolve().parents[1]
DATA = Path(os.environ.get('JOBFINDER_WEB_DATA', ROOT / 'data/webapp'))


class Store:
    def __init__(self, path=DATA):
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)
        self.db = self.path / 'workbench.sqlite3'
        with self.connect() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS connections (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, body TEXT NOT NULL,
                    secret BLOB NOT NULL, active INTEGER NOT NULL DEFAULT 1);
                CREATE TABLE IF NOT EXISTS commands (
                    id TEXT PRIMARY KEY, kind TEXT, body TEXT, state TEXT, result TEXT);
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.db, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def get(self, task_id):
        with self.connect() as db:
            row = db.execute('SELECT body FROM tasks WHERE id=?', (task_id,)).fetchone()
        if not row:
            raise KeyError('任务不存在')
        return task_contract(json.loads(row['body']))

    def all(self):
        with self.connect() as db:
            return sorted([task_contract(json.loads(r['body'])) for r in db.execute('SELECT body FROM tasks')], key=lambda t:t['created_at'], reverse=True)

    def create(self, body):
        body = dict(body, id=uuid.uuid4().hex[:16], created_at=now(), updated_at=now())
        with self.connect() as db:
            db.execute('INSERT INTO tasks VALUES (?,?)', (body['id'], json.dumps(body, ensure_ascii=False)))
        return body

    def update(self, task_id, change):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT body FROM tasks WHERE id=?', (task_id,)).fetchone()
            if not row:
                raise KeyError('任务不存在')
            body = task_contract(json.loads(row['body']))
            previous=deepcopy(body['input'])
            change(body)
            edits={k:v for k,v in body['input'].items() if previous.get(k)!=v}
            if edits:revise_execution(body,edits)
            task_contract(body)
            body['updated_at'] = now()
            db.execute('UPDATE tasks SET body=? WHERE id=?', (json.dumps(body, ensure_ascii=False), task_id))
        return body

    def event(self, task_id, message, kind='info'):
        return self.update(task_id, lambda t:t['events'].append({'at':now(), 'message':message, 'kind':kind}))

    def command(self, kind, body):
        cid = uuid.uuid4().hex
        with self.connect() as db:
            db.execute('INSERT INTO commands VALUES (?,?,?,?,?)', (cid, kind, json.dumps(body), 'pending', '{}'))
        return cid

    def connection(self, kind=None, cid=None, reveal=False):
        with self.connect() as db:
            row = db.execute('SELECT * FROM connections WHERE id=?' if cid else 'SELECT * FROM connections WHERE kind=? AND active=1', (cid or kind,)).fetchone()
        if not row:
            return None
        data = json.loads(row['body']) | {'id':row['id'], 'kind':row['kind'], 'has_key':bool(row['secret'])}
        if reveal:
            data['api_key'] = unprotect(bytes(row['secret']), self.path) if row['secret'] else ''
        return data

    def save_connection(self, kind, body, secret=None):
        old = self.connection(kind, reveal=True)
        if secret is None:
            secret = old['api_key'] if old else ''
        cid = uuid.uuid4().hex
        with self.connect() as db:
            db.execute('UPDATE connections SET active=0 WHERE kind=?', (kind,))
            db.execute('INSERT INTO connections VALUES (?,?,?,?,1)', (cid, kind, json.dumps(body, ensure_ascii=False), protect(secret, self.path)))
        return self.connection(cid=cid)

    def recover(self):
        for task in self.all():
            if task['state'] == 'running':
                def change(t):
                    t['state'] = 'paused'
                    for q in t['queries']:
                        if q['status'] == 'running':
                            q['status'] = 'interrupted'
                            q['error'] = '上次请求中断；已消耗预算，未自动重发'
                            for attempt in q.get('attempts',[]):
                                if attempt['status']=='running':attempt.update(status='interrupted',finished_at=now(),failure_type='interrupted',failure_reason=q['error'])
                    for c in t['candidates']:
                        if c['status'] == 'reading':
                            c['status'] = 'pending_extract' if c.get('page_snapshot') else 'pending'
                        for b in c.get('batches',[]):
                            if b['status']=='running':
                                b.update(status='failed',issue={'kind':'interrupted','message':'后台中断，本批未完成；用量保留，可明确重试'})
                                c.update(status='system_error',error='整理批次被中断，可重试未完成批次',issue={'kind':'interrupted','action':'system','message':'整理批次被中断，可重试未完成批次'})
                    t['events'].append({'at':now(), 'message':'后台重启，已保留用量和证据。可继续任务。', 'kind':'info'})
                self.update(task['id'], change)
        with self.connect() as db:
            db.execute("UPDATE commands SET state='failed', result=? WHERE state IN ('pending','running')", (json.dumps({'error':'后台重启，请重新操作'}, ensure_ascii=False),))


def _dpapi(data, decrypt=False):
    import ctypes
    from ctypes import wintypes
    class Blob(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_char))]
    buffer = ctypes.create_string_buffer(data)
    incoming = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    outgoing = Blob()
    fn = ctypes.windll.crypt32.CryptUnprotectData if decrypt else ctypes.windll.crypt32.CryptProtectData
    if not fn(ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing)):
        raise OSError('Windows 密钥保护不可用')
    try:
        return ctypes.string_at(outgoing.data, outgoing.size)
    finally:
        ctypes.windll.kernel32.LocalFree(outgoing.data)


def protect(value, folder):
    if not value:
        return b''
    if os.name == 'nt':
        return b'W' + _dpapi(value.encode())
    from cryptography.fernet import Fernet
    key = folder / '.credentials.key'
    if not key.exists():
        fd = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as f:
            f.write(Fernet.generate_key())
    return b'F' + Fernet(key.read_bytes()).encrypt(value.encode())


def unprotect(value, folder):
    if value[:1] == b'W':
        return _dpapi(value[1:], True).decode()
    from cryptography.fernet import Fernet
    return Fernet((folder / '.credentials.key').read_bytes()).decrypt(value[1:]).decode()
