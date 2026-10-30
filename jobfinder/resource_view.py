"""Readable company library view; external text is rendered as inert table text."""
from html import escape
from urllib.parse import urlsplit, quote

LABELS = {'industry': '行业', 'main_business': '主营业务', 'company_intro': '企业简介', 'operating_locations': '实际经营机构', 'hiring_history': '历史招聘线索', 'official_url': '官网', 'career_url': '招聘入口'}
STATUS = {'unknown': '待补充', 'candidate': '待核实', 'source_supported': '原文支持', 'conflict': '冲突待复核'}


def cell(value):
    value = escape(str(value), quote=False).replace('\n', ' ').replace('\r', ' ')
    for old, new in [('|', '&#124;'), ('[', '&#91;'), (']', '&#93;'), ('`', '&#96;'), ('\\', '&#92;')]:
        value = value.replace(old, new)
    return value


def link(value, label):
    try:
        parts = urlsplit(value)
        if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
            return '链接待核实'
        return '[' + label + '](<' + quote(value, safe=':/?&=#%+@,;!-._~') + '>)'
    except ValueError:
        return '链接待核实'


def markdown(bundle):
    lines = ['# 企业资源库', '', f"更新：{bundle['exported_at']} · 共 {len(bundle['companies'])} 家", '',
             'SQLite 为运行库，companies.json 保存完整字段、发现记录与证据；本文件为自动生成的浏览视图。', '',
             '“原文支持”只表示对应来源明确写出该信息，可能来自历史公告；读取时间不是信息生效日期，也不证明企业当前有在招岗位。', '',
             '| 企业名称 | ' + ' | '.join(LABELS.values()) + ' |',
             '|' + '---|' * (len(LABELS) + 1)]
    for company in bundle['companies']:
        values = [cell(company['company_name'])]
        for field in LABELS:
            value, state = company.get(field), company['field_status'].get(field, 'unknown')
            display = link(value, LABELS[field]) if value and field.endswith('_url') else cell(value) if value else '—'
            values.append(display + '（' + STATUS[state] + '）')
        lines.append('| ' + ' | '.join(values) + ' |')
    lines += ['', '## 来源与时间', '']
    for company in bundle['companies']:
        lines += ['### ' + cell(company['company_name']), '',
                  f"首次发现：{company['first_seen']}；最近发现/读取：{company['last_seen']}；累计发现记录：{company['discovery_count']}。", '']
        seen = set()
        for item in reversed(company['observations']):
            key = (item['field'], item['value'], item['source_url'], item['status'])
            if key in seen:
                continue
            seen.add(key)
            lines.append('- ' + LABELS[item['field']] + '：' + STATUS[item['status']] + '；' +
                         link(item['source_url'], '来源') + '；读取/发现时间 ' + item['observed_at'] + '。')
        if not seen and company['discoveries']:
            lines.append('- 企业名称来源：' + link(company['discoveries'][-1]['source_url'], '发现页面') + '。')
        lines.append('')
    return '\n'.join(lines)
