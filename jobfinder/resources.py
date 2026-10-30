"""Persistent company discoveries and field evidence. No network or implicit entity merges."""
import hashlib
import json
import os
import sqlite3
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import datetime
from .resource_view import markdown
from pathlib import Path
from .models import now
from .company_facts import STRUCTURED_FIELDS, normalize_value, combine_locations

FIELDS = ('industry', 'main_business', 'company_intro', 'operating_locations', 'hiring_history', 'official_url', 'career_url')


def name_key(value):
    return ''.join(unicodedata.normalize('NFKC', value).split()).casefold()


class ResourceStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS companies (
                    id TEXT PRIMARY KEY, name_key TEXT UNIQUE NOT NULL, company_name TEXT NOT NULL,
                    first_seen TEXT NOT NULL, last_seen TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS observations (
                    id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id),
                    field TEXT NOT NULL, value TEXT NOT NULL, status TEXT NOT NULL,
                    source_url TEXT NOT NULL, observed_at TEXT NOT NULL, method TEXT NOT NULL,
                    evidence TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS discoveries (
                    id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id),
                    source_url TEXT NOT NULL, observed_at TEXT NOT NULL, kind TEXT NOT NULL,
                    context TEXT NOT NULL);
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True, timeout=15) if getattr(self, 'read_only', False) else sqlite3.connect(self.path, timeout=15)
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    @classmethod
    def read_existing(cls, path):
        """Planning must not create a database, refresh snapshots or change evidence."""
        store = cls.__new__(cls)
        store.path, store.read_only = Path(path), True
        try:
            return store.records()
        except sqlite3.Error as exc:
            raise ValueError(f'无法只读打开企业库：{exc}') from exc

    def add(self, company_name, source_url, observed_at, kind, values=None,
            supported=(), evidence=None, context=None):
        key = name_key(company_name or '')
        if not key or not source_url or not observed_at:
            raise ValueError('企业资源必须有名称、来源和原始发现/读取时间')
        if datetime.fromisoformat(observed_at).tzinfo is None:
            raise ValueError('发现/读取时间必须含时区')
        values, evidence, context = values or {}, evidence or {}, context or {}
        if set(values) - set(FIELDS) or set(supported) - set(FIELDS):
            raise ValueError('不支持的企业字段')
        if supported and not (evidence.get('quote') and evidence.get('content_hash')):
            raise ValueError('原文支持字段须保留原文摘录和内容哈希')
        values = {field: normalize_value(field, value) for field, value in values.items()
                  if value is not None and value != '' and value != []}
        if evidence.get('entity_name') and name_key(evidence['entity_name']) != key:
            raise ValueError('证据主体与企业不一致，不能继承集团或子公司资料')
        for field in set(supported) & {'company_intro','operating_locations','hiring_history'} & values.keys():
            from .company_facts import CompanyFact
            proof = {k:evidence[k] for k in ('entity_name','quote','content_hash','kind','subject_relation') if k in evidence}
            CompanyFact.model_validate({'company_name':company_name,'field':field,'value':values[field],
                                        'evidence':proof | {'source_url':source_url,'observed_at':observed_at}})
        cid = hashlib.sha256(key.encode()).hexdigest()[:24]
        canonical_json = lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True)
        with self.connect() as db:
            db.execute('''INSERT INTO companies VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(name_key) DO UPDATE SET
                first_seen=CASE WHEN julianday(excluded.first_seen)<julianday(first_seen) THEN excluded.first_seen ELSE first_seen END,
                last_seen=CASE WHEN julianday(excluded.last_seen)>julianday(last_seen) THEN excluded.last_seen ELSE last_seen END''',
                (cid, key, company_name.strip(), observed_at, observed_at))
            payload = [cid, source_url, observed_at, kind, context]
            did = hashlib.sha256(canonical_json(payload).encode()).hexdigest()
            db.execute('INSERT OR IGNORE INTO discoveries VALUES (?,?,?,?,?,?)',
                       (did, cid, source_url, observed_at, kind, canonical_json(context)))
            for field, value in values.items():
                if value is None or not str(value).strip():
                    continue
                value = canonical_json(value) if field in STRUCTURED_FIELDS else value.strip()
                status = 'source_supported' if field in supported else 'candidate'
                payload = [cid, field, value, status, source_url, observed_at, kind, evidence]
                oid = hashlib.sha256(canonical_json(payload).encode()).hexdigest()
                db.execute('INSERT OR IGNORE INTO observations VALUES (?,?,?,?,?,?,?,?,?)',
                           (oid, cid, field, value, status, source_url, observed_at, kind, canonical_json(evidence)))
        return cid

    def records(self):
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            companies = [dict(r) for r in db.execute('SELECT * FROM companies ORDER BY company_name')]
            for company in companies:
                company.pop('name_key')
                observations = [dict(r) for r in db.execute('SELECT * FROM observations WHERE company_id=? ORDER BY julianday(observed_at),id', (company['id'],))]
                sightings = [dict(r) for r in db.execute('SELECT * FROM discoveries WHERE company_id=? ORDER BY julianday(observed_at),rowid', (company['id'],))]
                company['field_status'] = {}
                company['conflicts'] = {}
                for field in FIELDS:
                    evidence = [o for o in observations if o['field'] == field]
                    strong = list(dict.fromkeys(o['value'] for o in evidence if o['status'] == 'source_supported'))
                    weak = list(dict.fromkeys(o['value'] for o in evidence))
                    values = strong or weak
                    if field == 'operating_locations' and values:
                        combined = combine_locations([item for value in values for item in json.loads(value)])
                        if combined is not None:
                            values = [json.dumps(combined,ensure_ascii=False,sort_keys=True)]
                    if field == 'hiring_history':
                        # Different historical vacancies are additive, never evidence of current hiring.
                        records = [item for value in values for item in json.loads(value)]
                        values = [json.dumps(list({json.dumps(v, sort_keys=True, ensure_ascii=False): v for v in records}.values()), ensure_ascii=False, sort_keys=True)] if records else []
                    company[field] = values[0] if len(values) == 1 else None
                    if company[field] is not None and field in STRUCTURED_FIELDS:
                        company[field] = json.loads(company[field])
                    company['field_status'][field] = 'conflict' if len(values) > 1 else ('source_supported' if strong else 'candidate') if values else 'unknown'
                    if len(values) > 1:
                        company['conflicts'][field] = values
                for o in observations:
                    o.pop('company_id')
                    o['evidence'] = json.loads(o['evidence'])
                for sighting in sightings:
                    sighting.pop('company_id')
                    sighting['context'] = json.loads(sighting['context'])
                company['observations'] = observations
                company['discoveries'] = sightings
                entries = {}
                for sighting in sightings:
                    entry = sighting['context'].get('entry') if sighting['kind'] == 'entry_check' else None
                    if entry:
                        from .network import request_url
                        key = (request_url(entry['url']),entry['entry_type'])
                        previous = entries.get(key,{})
                        if entry['availability'] == 'not_checked' and previous:
                            entry = entry | {'availability':previous['availability'], 'last_check_status':'not_checked'}
                        entries[key] = entry
                company['entries'] = list(entries.values())
                company['discovery_count'] = len(sightings)
                company['last_verified_at'] = max((o['observed_at'] for o in observations if o['status'] == 'source_supported'), default=None, key=datetime.fromisoformat)
        return companies

    def snapshot(self, path=None):
        value = {'schema_version': '1.2', 'exported_at': now(), 'companies': self.records(),
                 'notes': ['企业资源不等于当前招聘或条件匹配', '按规范化完整名称合并；品牌/子公司/相同域名不会自动合并',
                           'source_supported表示该来源原文支持，不保证字段永久有效；冲突保留待复核']}
        path = Path(path) if path else self.path.with_suffix('.json')
        path.parent.mkdir(parents=True, exist_ok=True)
        for target, content in ((path, json.dumps(value, ensure_ascii=False, indent=2)),
                                (path.with_suffix('.md'), markdown(value))):
            temp = target.with_name(target.name + '.' + uuid.uuid4().hex + '.tmp')
            try:
                temp.write_text(content, encoding='utf-8')
                os.replace(temp, target)
            finally:
                temp.unlink(missing_ok=True)
        return value


def record_candidates(store, candidates, kind='search_candidate'):
    if any(c.synthetic for c in candidates):
        raise ValueError('合成候选不得写入真实企业资源库')
    ids = []
    for candidate in candidates:
        if not candidate.company:
            continue
        from .entries import source_type
        typed = source_type(candidate)
        ids.append(store.add(candidate.company, candidate.url, candidate.discovered_at, kind,
                             values={'official_url': candidate.official_url if typed not in ('platform','repost') else None,
                                     'career_url': candidate.career_url if typed not in ('platform','repost') else None},
                             context={'query': candidate.query, 'target_cities': candidate.target_cities,
                                      'task_ids': candidate.task_ids, 'announcement_url': candidate.announcement_url}))
    return list(dict.fromkeys(ids))


def record_task_companies(store, tasks, kind='known_enterprise_query'):
    """Retain known employers even when their executed query yields no job link."""
    ids = []
    for task in tasks:
        if task.get('status') not in ('success', 'failed', 'restricted'):
            continue
        if not all(task.get(k) for k in ('company', 'company_source', 'executed_at')):
            continue
        ids.append(store.add(task['company'], task['company_source'], task['executed_at'], kind,
                             context={'query': task['query'], 'task_id': task['task_id'],
                                      'target_city': task['target_city'], 'query_status': task['status']}))
    return list(dict.fromkeys(ids))
