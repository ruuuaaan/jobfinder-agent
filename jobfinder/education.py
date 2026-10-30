"""Deterministic education facts. No user qualification is inferred from a job."""
from enum import Enum
from dataclasses import dataclass
import re

RULES_VERSION = 'CN_MAINLAND-4.0'
LEGACY_RULES_VERSION = 'CN_MAINLAND-3.9'


class EducationLevel(str, Enum):
    UNKNOWN = 'UNKNOWN'
    NO_REQUIREMENT = 'NO_REQUIREMENT'
    PRIMARY = 'PRIMARY'
    MIDDLE_SCHOOL = 'MIDDLE_SCHOOL'
    HIGH_SCHOOL = 'HIGH_SCHOOL'
    ASSOCIATE = 'ASSOCIATE'
    BACHELOR = 'BACHELOR'
    MASTER = 'MASTER'
    DOCTORATE = 'DOCTORATE'


LABELS = dict(zip(EducationLevel, ['未设置','学历不限','小学','初中','高中 / 中专','大专','本科','硕士','博士']))
RANK = {k:i for i,k in enumerate(list(EducationLevel)[2:])}
CORE_LABELS = dict(zip(list(EducationLevel)[2:], ['小学','初中','高中','大专','本科','硕士','博士']))
PATTERNS = {
    EducationLevel.PRIMARY:r'小学', EducationLevel.MIDDLE_SCHOOL:r'初中',
    EducationLevel.HIGH_SCHOOL:r'高中|中专|中技|职高|high\s*school',
    EducationLevel.ASSOCIATE:r'大专|专科|associate(?:[’\']s)?',
    EducationLevel.BACHELOR:r'本科|学士|bachelor(?:[’\']s)?',
    EducationLevel.MASTER:r'硕士(?:研究生)?|master(?:[’\']s)?',
    EducationLevel.DOCTORATE:r'博士(?:研究生)?|doctorate|ph\.?d\.?',
}


@dataclass(frozen=True)
class EducationFact:
    minimum: EducationLevel
    reason: str


def normalize_education(raw):
    if not raw or re.fullmatch(r'\s*(?:未披露(?:学历)?|未说明|未知|unknown|n/?a)?\s*',raw,re.I):
        return EducationFact(EducationLevel.UNKNOWN,'学历未披露，不等于学历不限')
    text=re.sub(r'\s+',' ',raw).strip()
    if re.search(r'放宽|优秀者|或.{0,15}经验|经验.{0,15}或|不接受|不含|非全日制除外',text):
        return EducationFact(EducationLevel.UNKNOWN,'含条件性学历要求，最低门槛待确认')
    # Preferences are not minimums. Preserve the other mandatory clauses.
    degree='(?:小学|初中|高中|中专|大专|专科|本科|学士|硕士(?:研究生)?|博士(?:研究生)?)'
    mandatory=re.sub(degree+r'(?:学历|学位)?\s*(?:及以上)?\s*(?:优先|为佳|preferred)', '', text, flags=re.I)
    unrestricted=bool(re.search(r'学历不限|不限学历|无学历要求|不要求学历|no (?:degree|education) requirement',mandatory,re.I) or re.fullmatch(r'不限[。.!！]?',mandatory))
    levels=[k for k,p in PATTERNS.items() if re.search(p,mandatory,re.I)]
    if unrestricted:
        return EducationFact(EducationLevel.UNKNOWN,'不限与强制学历同时出现，需确认') if levels else EducationFact(EducationLevel.NO_REQUIREMENT,'原文明示学历不限')
    if not levels:
        return EducationFact(EducationLevel.UNKNOWN,'只有优先条件或无法确认最低学历')
    if len(levels)>1 and (re.search(r'同时|并|取得|必须|及其',mandatory) or not re.search(r'或|/|、|或者|\bor\b',mandatory,re.I)):
        return EducationFact(EducationLevel.UNKNOWN,'多重学历要求，不能猜测最低门槛')
    return EducationFact(min(levels,key=RANK.get),'按原文最低学历门槛规范化')


def candidate_level(value):
    if isinstance(value,EducationLevel):
        if value==EducationLevel.UNKNOWN:return None
        if value in RANK:return value
        raise ValueError('学历不限不是本人的学历等级')
    if value is None or value in ('','不限','未设置','UNKNOWN'):
        return None
    try:level=EducationLevel(str(value).upper())
    except ValueError:
        if re.search(r'以上|以下|优先|或者|或|至少|不高于|不低于',str(value)):
            raise ValueError('我的学历须填写明确等级，不能填写岗位门槛范围')
        level=normalize_education(str(value)).minimum
    if level not in RANK:
        raise ValueError('我的学历必须是明确学历等级；未设置请用 null')
    return level


def compare_candidate(raw, candidate):
    if candidate is None:
        return 'PASS','未设置我的学历，不以学历排除；岗位原文仍保留'
    fact=normalize_education(raw)
    if fact.minimum==EducationLevel.UNKNOWN:return 'UNKNOWN',fact.reason
    if fact.minimum==EducationLevel.NO_REQUIREMENT:return 'PASS',fact.reason
    ok=RANK[candidate]>=RANK[fact.minimum]
    return ('PASS' if ok else 'FAIL'),f'我的学历 {LABELS[candidate]}；岗位最低门槛 {LABELS[fact.minimum]}'
