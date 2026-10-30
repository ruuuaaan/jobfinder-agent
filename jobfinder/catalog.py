import csv
import hashlib
import json
from pathlib import Path
from openpyxl import load_workbook
from .models import Company, now


def read_rows(path: Path, header_row=1):
    if path.suffix.lower() == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as stream:
            return list(csv.DictReader(stream))
    if path.suffix.lower() == ".xlsx":
        book = load_workbook(path, read_only=True, data_only=True)
        try:
            rows = book.active.iter_rows(min_row=header_row, values_only=True)
            headers = [str(v).strip() if v is not None else "" for v in next(rows)]
            return [dict(zip(headers, row)) for row in rows if any(v is not None for v in row)]
        finally:
            book.close()
    raise ValueError("仅支持 UTF-8 CSV / XLSX；XLS、扫描件需用户有权转换，不假装已解析")


def parse_bool(value):
    if value in (True, False):
        return value
    if value is None or str(value).strip().lower() in ("", "null", "unknown", "未知"):
        return None
    if str(value).lower() in ("true", "1", "是"):
        return True
    if str(value).lower() in ("false", "0", "否"):
        return False
    raise ValueError("上市状态只能是 true/false/unknown")


def import_catalog(path: Path, source_url: str, version_date: str, mapping=None, header_row=1, listed_default=None, previous=None):
    mapping = mapping or {k:k for k in Company.model_fields}
    companies = []
    for row in read_rows(path, header_row):
        record = {key: row.get(column) for key, column in mapping.items() if row.get(column) not in (None, "")}
        if not record.get("legal_name"):
            continue
        record.setdefault("source_url", source_url)
        record.setdefault("version_date", version_date)
        record.setdefault("list_date",version_date)
        record.setdefault("acquired_at",now())
        record["listed_status"] = parse_bool(record.get("listed_status", listed_default))
        companies.append(Company.model_validate(record))
    old = {c["legal_name"]:c for c in (previous or {}).get("companies", [])}
    new = {c.legal_name:c.model_dump() for c in companies}
    return {"source_url":source_url, "version_date":version_date, "list_date":version_date, "acquired_at":now(), "imported_at":now(),
            "current_status_verified":False, "complete_national_list":False,
            "known_limitations":["名单日期不是获取日期；历史快照不能证明当前属性；未出现不代表非上市/非国企"],
            "file_sha256":hashlib.sha256(path.read_bytes()).hexdigest(), "source_file":str(path),
            "scope":"仅本文件包含的企业，不代表全国完整名单；注册地址不用于岗位筛选",
            "companies":list(new.values()), "changes":{"added":sorted(new.keys()-old.keys()),
            "missing_from_snapshot":sorted(old.keys()-new.keys()), "changed":[k for k in new.keys() & old.keys() if new[k] != old[k]]}}


def save_new_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, default=str)


def import_official_directory(path: Path, source_url: str, scope: str, list_date=None, limit=None):
    """Explicit original HTML import. This does not claim Python fetched the source."""
    from urllib.parse import urlsplit
    import re
    from .parsers import links
    if not urlsplit(source_url).hostname.endswith('.gov.cn'):
        raise ValueError('官方目录导入须提供政府原始来源URL；其他企业CSV/XLSX用import-companies')
    rows=[]
    for link in links(path.read_bytes(),source_url):
        name=link['title'] or ''
        if re.fullmatch(r'[\u4e00-\u9fff（）()A-Za-z0-9·]{4,70}(?:股份有限公司|有限责任公司|有限公司)',name) and name not in {r['legal_name'] for r in rows}:
            rows.append(dict(legal_name=name,listed_status=None,soe_status=None,source_url=source_url,version_date=list_date or 'unknown',list_date=list_date,acquired_at=now(),coverage_scope=scope,official_url=None,career_url=None,directory_detail_url=link['url'],notes='官方目录名称线索，不直接证明当前属性；目录详情不冒充企业官网'))
            if limit is not None and len(rows)>=limit:
                break
    return dict(companies=rows,source_url=source_url,scope=scope,list_date=list_date,acquired_at=now(),version=hashlib.sha256(path.read_bytes()).hexdigest(),source_file=str(path),source_kind='enterprise_directory',mode='official_html_import_not_live_read',complete_national_list=False,current_status_verified=False,known_limitations=['显式抽样，仅本页前N条' if limit is not None else '解析本次已提供页面的全部可识别企业，未遍历分页','名单日期未知不能用获取时间替代','企业属性和岗位所在地须另行核实'])
