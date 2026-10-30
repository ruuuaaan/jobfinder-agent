"""Short-lived, policy-bound robots evidence; never cache job content or credentials."""
from __future__ import annotations
import hashlib
import json
import time
import urllib.request
from pathlib import Path
from uuid import uuid4


class RobotsCache:
    def __init__(self, directory=None):
        self.directory = Path(directory) if directory is not None else None
        self.memory = {}

    @staticmethod
    def key(host, policy):
        # Ordinary page approvals remain checked on every read. A new approved
        # page on the same origin does not change that origin's robots rules.
        relevant = {k: v for k, v in policy.items() if k not in ('urls', 'reviewed_at', 'evidence_url')}
        context = dict(version=1, host=host, policy=relevant,
                       proxy_context=hashlib.sha256(json.dumps(urllib.request.getproxies(), sort_keys=True).encode()).hexdigest())
        return hashlib.sha256(json.dumps(context, sort_keys=True).encode()).hexdigest()

    def get(self, key):
        entry = self.memory.get(key)
        if entry is None and self.directory is not None:
            path = self.directory / (key + '.json')
            try:
                if path.stat().st_size > 15_000_000:
                    return None
                entry = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                return None
        if not isinstance(entry, dict):
            return None
        try:
            created, expires = float(entry['created']), float(entry['expires'])
            ttl_limit = 60 if entry['kind'] == 'failure' else 3600
            if not (created <= time.time() < expires <= created + ttl_limit):
                return None
            payload = entry['payload']
            digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
            if entry.get('key') != key or entry.get('digest') != digest:
                return None
        except (KeyError, TypeError, ValueError):
            return None
        self.memory[key] = entry
        return entry

    def put(self, key, kind, payload, ttl):
        ttl = min(ttl, 60 if kind == 'failure' else 3600)
        if ttl <= 0:
            return
        created = time.time()
        entry = dict(key=key, kind=kind, payload=payload, created=created, expires=created + ttl,
                     last_request_at=created,
                     digest=hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest())
        self.memory[key] = entry
        self._write(key, entry)

    def touch(self, key):
        entry = self.get(key)
        if entry is not None:
            entry['last_request_at'] = time.time()
            self._write(key, entry)

    def _write(self, key, entry):
        if self.directory is not None:
            temporary = self.directory / (key + '.' + uuid4().hex + '.tmp')
            try:
                self.directory.mkdir(parents=True, exist_ok=True)
                temporary.write_text(json.dumps(entry, ensure_ascii=False), encoding='utf-8')
                temporary.replace(self.directory / (key + '.json'))
            except OSError:
                # Losing an optimization must not lose the evidence or abort a run.
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
