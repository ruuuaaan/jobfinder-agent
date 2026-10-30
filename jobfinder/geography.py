"""Audited, deliberately incomplete aliases; unrecognized places stay UNKNOWN."""
import re
import json
from functools import lru_cache
from pathlib import Path
from .models import GeoDecision, Job

CITIES = {
    "深圳": ["shenzhen", "福田区", "罗湖区", "盐田区", "宝安区", "龙岗区", "坪山区", "光明区", "大鹏新区", "futian", "luohu", "bao'an"],
    "广州": ["guangzhou", "天河区", "越秀区", "荔湾区", "海珠区", "番禺区"],
    "北京": ["beijing", "peking", "海淀区"],
    "上海": ["shanghai", "浦东新区", "徐汇区", "闵行区"],
    "杭州": ["hangzhou", "滨江区", "余杭区"],
    "南京": ["nanjing", "建邺区"], "苏州": ["suzhou"],
    "成都": ["chengdu", "武侯区"], "重庆": ["chongqing"],
    "武汉": ["wuhan", "洪山区"], "西安": ["xi'an", "xian"],
    "东莞": ["dongguan"], "佛山": ["foshan"], "珠海": ["zhuhai"],
    "惠州": ["huizhou"], "长沙": ["changsha"], "合肥": ["hefei"],
    "厦门": ["xiamen"], "福州": ["fuzhou"], "天津": ["tianjin"],
    "郑州": ["zhengzhou"], "济南": ["jinan"], "青岛": ["qingdao"],
    "宁波": ["ningbo"], "无锡": ["wuxi"], "沈阳": ["shenyang"],
}
# 南山区(深圳/鹤岗)/龙华区/朝阳区/高新区 are ambiguous without a city.
OUTSIDE = r"香港|澳门|澳門|台湾|臺灣|台北|臺北|新北|高雄|香港特别行政区|海外|境外|国外|新加坡|东京|日本|美国|英国|德国|越南|印度|马来西亚|澳大利亚|迪拜|加拿大|泰国|印尼|韩国|法国|纽约|伦敦|旧金山|悉尼|hong\s*kong|\bhk\b|macao|macau|taiwan|taipei|singapore|tokyo|japan|united states|\busa?\b|london|new york|sydney|vietnam|germany|malaysia|overseas"


def contains(text: str, alias: str) -> bool:
    if alias.isascii():
        return bool(re.search(r"(?<![a-z])" + re.escape(alias) + r"(?![a-z])", text.lower()))
    return alias in text


REGISTRY = json.loads((Path(__file__).resolve().parents[1]/"data/geography/admin-2024.json").read_text(encoding="utf-8"))
REGISTRY = [r for r in REGISTRY if r["code"][:2] not in ("71","81","82")]
BY_CODE = {r["code"]:r["name"] for r in REGISTRY}
PARENTS = {}
ALIASES = {}
for row in REGISTRY:
    code, name = row["code"], row["name"]
    if name.endswith("市") and name not in ("市辖区",):
        city = name[:-1]
        CITIES.setdefault(city, [])
        if not code.endswith("00"):
            parent = BY_CODE.get(code[:4]+"00", "")
            if parent.endswith("市"):
                PARENTS[city] = parent[:-1]
    if code[:2] in ("11","12","31","50"):
        parent = BY_CODE[code[:2]+"0000"][:-1]
    else:
        parent = BY_CODE.get(code[:4]+"00", "")
        parent = parent[:-1] if parent.endswith("市") else None
    if not code.endswith("00") and parent:
        ALIASES.setdefault(name,set()).add(parent)
# Only unique district names are inferred; famous but ambiguous districts stay unknown.
for alias, parents in ALIASES.items():
    if len(parents)==1 and not alias.endswith("市"):
        CITIES[next(iter(parents))].append(alias)


def belongs(city, target):
    return city == target or PARENTS.get(city) == target


@lru_cache(maxsize=None)
def _city_pattern(city):
    # Only called for the fixed registry keys. Its size exceeds re's global
    # cache, so polling task contracts otherwise recompiles every city again.
    return re.compile(re.escape(city)+r"(?:市)?(?!市|路|街|大道|区|县)")


def city_hits(raw: str) -> list[str]:
    matches = [(city, m.start(), m.end()) for city in CITIES
               for m in _city_pattern(city).finditer(raw)]
    # 天津市 contains the text 津市 (a different county city in Hunan).
    # Keep a city only if at least one occurrence is not nested in a longer
    # explicitly named city; separate mentions of both cities remain ambiguous.
    direct = list(dict.fromkeys(city for city,start,end in matches if not any(
        other!=city and left<=start and end<=right and right-left>end-start
        for other,left,right in matches)))
    # A county city is more specific than its prefecture; keep the parent relation.
    direct = [c for c in direct if not any(PARENTS.get(other)==c for other in direct)]
    if direct:
        return direct
    return [city for city, aliases in CITIES.items() if any(contains(raw,a) for a in aliases)]


def normalize_city(raw: str) -> str | None:
    hits = city_hits(raw)
    return hits[0] if len(hits) == 1 and not re.search(OUTSIDE, raw, re.I) else None


def same_city_evidence(values) -> bool:
    """Different address granularity is not a conflict at city-filter level.

    This does not equate exact workplaces or infer a city for an unknown value.
    Overseas, remote and rotational qualifiers must retain their own review.
    """
    values = list(values)
    if not values or any(not isinstance(v, str) or not v.strip() for v in values):
        return False
    if any(re.search(OUTSIDE + r'|远程|居家|轮岗|\bremote\b', v, re.I) for v in values):
        return False
    cities = [normalize_city(v) for v in values]
    return all(cities) and len(set(cities)) == 1


def evaluate_location(job: Job, raw: str | None, targets: list[str]) -> GeoDecision:
    def result(state, reason, city=None):
        return GeoDecision(state=state, reason=reason, raw=raw, city=city,
                           evidence=f"岗位地点字段：{raw or '未披露'}；岗位履职要求：{job.requirements or ''}")
    duties = " ".join([job.description, job.requirements or ""])
    text = (raw or "")
    if job.work_mode == "remote" or re.search(r"远程(?:办公|工作)?|居家办公|\bremote\b|work from home", text, re.I):
        return result("FAIL", "本阶段排除远程岗位")
    if re.search(r"(?:需|必须|要求).{0,8}(?:远程办公|居家办公)|fully remote", duties, re.I):
        return result("FAIL", "岗位明确为远程办公")
    if re.search(r"长期.{0,4}(?:驻外|海外|境外)|常驻.{0,4}(?:海外|境外|国外)|(?:必须|需要|需).{0,8}跨境轮岗|海外常驻|长期外派", duties + text):
        return result("FAIL", "长期驻外或必须跨境轮岗")
    if re.search(r"(?:常驻|长期驻|长期派驻).{0,8}(?:" + OUTSIDE + ")", duties + text, re.I):
        return result("FAIL", "原文明示常驻境外地点")
    if job.location_mode == "rotation":
        if any(re.search(OUTSIDE, p, re.I) for p in job.locations + [text]):
            return result("FAIL", "必须跨境轮岗，不是可独立选择的大陆岗位")
        return result("UNKNOWN", "多地轮岗，无法确认只在目标城市履职")
    # Explicit short travel destinations are not work locations.
    text = re.sub(r"[（(，,；;].{0,12}短期.{0,12}出差.*$|[（(，,；;]短期出差.*$", "", text)
    if re.search(OUTSIDE, text, re.I):
        if city_hits(text):
            return result("UNKNOWN", "大陆与境外地点同时出现，但未证实可独立选择")
        return result("FAIL", "实际工作地点在本阶段大陆范围之外")
    if job.location_mode=='unknown' and len(job.locations)>1:
        single=job.model_copy(update={'location_mode':'single'})
        if all(evaluate_location(single,p,targets).state=='FAIL' for p in job.locations):
            return result('FAIL','所有列明工作地点均有证据不满足目标地域；地点选择关系未知不改变此结论')
    hits = city_hits(text)
    coded={e.field_quotes.get('city') for e in job.evidence if e.method=='moka_city_code' and e.field_quotes.get('location_raw')==raw and e.field_quotes.get('city') in CITIES}
    if coded:
        if len(coded)!=1 or hits and not any(h in coded or PARENTS.get(h) in coded for h in hits):
            return result('UNKNOWN','岗位地址与已核实的来源地点代码冲突')
        if not hits:
            hits=list(coded)
    if not hits:
        return result("UNKNOWN", "没有可可靠归属的具体大陆城市，不能用企业所在地、China或全国补全")
    if len(hits) != 1 or job.location_mode == "unknown":
        return result("UNKNOWN", "多个地点的选择/履职关系不明确")
    city = hits[0]
    if not any(belongs(city,normalize_city(t)) for t in targets):
        return result("FAIL", "大陆城市明确，但不满足目标城市", city)
    return result("PASS", "岗位地点字段及已核实代码明确属于目标大陆城市" if coded else "岗位地点字段明确属于目标大陆城市", city)


def split_locations(job: Job) -> list[str | None]:
    if job.location_mode == "alternatives" and job.locations:
        return job.locations
    return [job.location_raw]
