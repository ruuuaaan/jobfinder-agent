"""Validate assistant-authored role groups; no synonym generation or search."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator


def nonblank(value: str) -> str:
    if not value.strip():
        raise ValueError("文字不能为空白")
    return value


def clean_text(value: str) -> str:
    return " ".join(nonblank(value).split())


Text = Annotated[str, AfterValidator(clean_text)]
RawText = Annotated[str, AfterValidator(nonblank)]
Identity = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExplicitInput(Record):
    input_id: Identity
    raw_text: RawText


class DeferredInput(Record):
    input_id: Identity
    reason: Text


class RoleNature(Record):
    kind: Literal["specialist", "general", "mixed", "unclear"] = "unclear"
    rationale: Text = "旧产物未判断岗位性质，保守保留待核查"
    company_requirements: list[Text] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_requirements(self):
        if self.kind == "specialist" and not self.company_requirements:
            raise ValueError("专业岗位须说明工作内容需要的业务、产品或部门")
        return self


class RoleGroup(Record):
    group_id: Identity
    source_input_ids: list[Identity] = Field(min_length=1)
    primary_term: Text
    aliases: list[Text] = Field(default_factory=list)
    category: Literal["synonym", "specialization", "adjacent"]
    scope_relation: Literal["within", "outside", "unclear"]
    rationale: Text
    verification_conditions: list[Text] = Field(min_length=1)
    nature: RoleNature = Field(default_factory=RoleNature)

    @model_validator(mode="after")
    def normalize_terms(self):
        self.source_input_ids = list(dict.fromkeys(self.source_input_ids))
        seen = {self.primary_term.casefold()}
        aliases = []
        for alias in self.aliases:
            if alias.casefold() not in seen:
                aliases.append(alias)
                seen.add(alias.casefold())
        if len(aliases) > 2:
            raise ValueError("每组最多2个常用别名；不可静默截断用户指定岗位")
        self.aliases = aliases
        return self


class RoleExpansion(Record):
    schema_version: Literal["role-groups-1", "role-groups-2"]
    artifact_kind: Literal["example", "user"]
    original_request: RawText
    breadth: Literal["narrow", "broad"]
    scope_boundary: Text
    excluded_functions: list[Text] = Field(default_factory=list)
    explicit_inputs: list[ExplicitInput] = Field(min_length=1)
    ready_groups: list[RoleGroup] = Field(default_factory=list)
    conditional_groups: list[RoleGroup] = Field(default_factory=list)
    pending_confirmation_groups: list[RoleGroup] = Field(default_factory=list)
    deferred_inputs: list[DeferredInput] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_boundaries(self):
        inputs = [item.input_id for item in self.explicit_inputs]
        if len(set(inputs)) != len(inputs):
            raise ValueError("explicit_inputs的input_id必须唯一")
        known_inputs = set(inputs)
        represented = set()
        identities = {}
        terms = {}
        buckets = ("ready_groups", "conditional_groups", "pending_confirmation_groups")
        for bucket in buckets:
            unique = []
            for group in getattr(self, bucket):
                if self.schema_version == "role-groups-2" and (
                    'nature' not in group.model_fields_set or
                    not {'kind', 'rationale'} <= group.nature.model_fields_set
                ):
                    raise ValueError("role-groups-2每组须明确岗位性质与判断理由")
                previous = identities.get(group.group_id)
                if previous:
                    if previous == (bucket, group.model_dump()):
                        continue  # Only identical records; never merge different functions.
                    raise ValueError("group_id重复且内容或分类不同，须人工解决")
                identities[group.group_id] = (bucket, group.model_dump())
                unique.append(group)
                if not set(group.source_input_ids) <= known_inputs:
                    raise ValueError("词组引用了不存在的input_id")
                represented.update(group.source_input_ids)
                if bucket == "ready_groups":
                    if group.scope_relation != "within" or group.category == "adjacent":
                        raise ValueError("可直接搜索组仅限范围内的同义名称或同职能细分")
                elif bucket == "conditional_groups":
                    if group.scope_relation != "within" or group.category != "adjacent":
                        raise ValueError("条件组须是范围内、待原文职责核验的相邻职能")
                elif group.scope_relation == "within":
                    raise ValueError("待确认组须标明范围不清或超出范围")
                for term in [group.primary_term, *group.aliases]:
                    key = term.casefold()
                    if key in terms:
                        raise ValueError("同一规范词出现在不同组；请明确职能，不自动合并")
                    terms[key] = group.group_id
            setattr(self, bucket, unique)
        maximum = 4 if self.breadth == "narrow" else 6
        if len(identities) > maximum:
            raise ValueError(f"本批初始展开最多{maximum}组（含条件和待确认组）；请分批或记录待处理输入")
        deferred = [item.input_id for item in self.deferred_inputs]
        if len(set(deferred)) != len(deferred) or not set(deferred) <= known_inputs:
            raise ValueError("待处理input_id必须唯一且来自explicit_inputs")
        if represented.intersection(deferred):
            raise ValueError("同一输入不能既有词组又标记为整项待处理")
        if represented.union(deferred) != known_inputs:
            raise ValueError("有用户明确输入未被词组覆盖或记录为待处理，不可静默丢弃")
        return self

    def search_groups(self) -> list[dict]:
        """Only ready groups, with stable IDs and separate terms, never queries."""
        return [dict(group_id=g.group_id, primary_term=g.primary_term, aliases=list(g.aliases))
                for g in self.ready_groups]


def main(argv=None):
    parser = argparse.ArgumentParser(description="只读校验岗位词组；不生成同义词、不搜索、不改匹配配置")
    parser.add_argument("--input", required=True, help="助手填写的role-groups-2 JSON；兼容旧版")
    args = parser.parse_args(argv)
    try:
        expansion = RoleExpansion.model_validate(json.loads(Path(args.input).read_text(encoding="utf-8-sig")))
    except (ValueError, OSError) as exc:
        print(json.dumps({"status": "invalid", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps({"status": "structure_valid", "semantic_validation": "not_performed",
                      "role_context_hash":hashlib.sha256(json.dumps(expansion.model_dump(),ensure_ascii=False,sort_keys=True).encode()).hexdigest(),
                      "expansion": expansion.model_dump(), "search_groups": expansion.search_groups()},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
