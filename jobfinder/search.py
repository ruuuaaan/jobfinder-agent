from pathlib import Path
from typing import Protocol
import json
from .models import Candidate, SearchConfig


class SearchProvider(Protocol):
    def discover(self, config: SearchConfig) -> tuple[list[Candidate], dict]: ...


class AgentCandidateProvider:
    """Consumes host-provided receipts; never calls a host's private tool API."""
    def __init__(self, path: Path):
        self.path = path

    def discover(self, config):
        data = json.loads(self.path.read_text(encoding="utf-8-sig"))
        candidates = [Candidate.model_validate(c) for c in data["candidates"]]
        mode = data.get("mode", "provided_links")
        log = data.get("search_log", [])
        if mode in ("agent_assisted", "codex_assisted") and not log and candidates:
            raise ValueError("Agent辅助检索必须保存实际执行的查询记录，不能将已知链接冒充搜索")
        if mode not in ("agent_assisted", "codex_assisted", "provided_links", "offline"):
            raise ValueError("未知发现模式")
        return candidates, {"provider":mode, "status":"provided_links_only",
                            "search_log":log, "coverage":data.get("coverage", "仅此文件候选；不是全网完整搜索"), "candidate_count":len(candidates)}


# Preserve imports and old receipts without rewriting their fingerprints.
CodexCandidateProvider = AgentCandidateProvider


class UnconfiguredSearchProvider:
    def discover(self, config):
        return [], {"provider":"independent", "status":"unconfigured", "coverage":"未执行独立网络搜索；需要配置获准使用且经实际验证的搜索服务"}
