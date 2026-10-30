"""Saved per-city channel allocation; failure allowances never reset usage."""
from collections import Counter
from jobfinder.models import now
from jobfinder.search_budget import PRIORITY, OFFICIAL, BASE_OTHER, FAILURE_ALLOWANCE, validate_allocation, base_allocation


def budget_policy(task):
    return task.get('input', {}).get('budget_policy', 'shared')


def counts(task):
    # Imported legacy queries did not have attempt arrays; preserve that count.
    city_counts, channel_counts, pairs = Counter(), Counter(), Counter()
    for q in task.get('queries', []):
        n = len(q['attempts']) if 'attempts' in q else int(q['status'] != 'pending')
        city_counts[q['city']] += n
        channel_counts[q['channel']] += n
        pairs[q['city'], q['channel']] += n
    return city_counts, channel_counts, pairs


def grants(task):
    # Count once per saved city/channel, even if a damaged log contains duplicates.
    return {(g['city'], g['channel']) for g in task.get('channel_budget_grants', [])
            if g.get('amount') == FAILURE_ALLOWANCE and g.get('channel') != OFFICIAL}


def limits(task):
    inp = task['input']
    allocation = base_allocation(inp['cities'], inp['channels'], inp['city_budgets'])
    awarded = grants(task)
    pairs = {(city, channel): base + (FAILURE_ALLOWANCE if (city, channel) in awarded else 0)
             for city, channels in allocation.items() for channel, base in channels.items()}
    cities = {city: sum(pairs[city, c] for c in inp['channels']) for city in inp['cities']}
    return cities, pairs


def within_budget(task, query, used=None):
    city_counts, channel_counts, pairs = used or counts(task)
    inp = task['input']; city = query['city']; channel = query['channel']
    if budget_policy(task) == PRIORITY:
        city_limits, pair_limits = limits(task)
        return (city_counts[city] < city_limits.get(city, 0)
                and pairs[city, channel] < pair_limits.get((city, channel), 0))
    return (city_counts[city] < inp['city_budgets'][city]
            and channel_counts[channel] < inp['channel_budgets'].get(channel, 5000))


def grant_failure_allowance(task, query, attempt):
    if budget_policy(task) != PRIORITY or attempt.get('status') != 'failed':
        return False
    city, channel = query['city'], query['channel']
    inp = task['input']
    if channel == OFFICIAL or channel not in inp['channels'] or city not in inp['cities'] or (city, channel) in grants(task):
        return False
    grant = {'city': city, 'channel': channel, 'amount': FAILURE_ALLOWANCE,
             'query_id': query['id'], 'attempt_number': attempt['attempt_number'],
             'failure_type': attempt.get('failure_type'), 'granted_at': now(),
             'reason': '非官网渠道首次搜索失败，一次性补充1次；后续失败不再追加'}
    task.setdefault('channel_budget_grants', []).append(grant)
    task.setdefault('events', []).append({'at': grant['granted_at'], 'kind': 'info',
        'message': f'{city} / {channel} 搜索失败，额外补充1次额度；本渠道本城市不再自动追加，原用量保留'})
    return True


def summary(task):
    inp = task['input']; policy = budget_policy(task)
    city_used, channel_used, pair_used = counts(task)
    awarded = grants(task) if policy == PRIORITY else set()
    allocation = base_allocation(inp['cities'], inp['channels'], inp['city_budgets']) if policy == PRIORITY else {}
    per_city = {}
    for city in inp['cities']:
        base = inp['city_budgets'][city]
        assigned = sum(allocation[city].values()) if policy == PRIORITY else base
        bonus = sum((city, ch) in awarded for ch in inp['channels'])
        possible = sum(ch != OFFICIAL for ch in inp['channels']) if policy == PRIORITY else 0
        channel_rows = {}
        for channel in inp['channels']:
            b = allocation[city][channel] if policy == PRIORITY else min(base, inp['channel_budgets'].get(channel, base))
            plus = int((city, channel) in awarded)
            used = pair_used[city, channel]
            channel_rows[channel] = {'base': b, 'bonus': plus, 'limit': b+plus, 'used': used,
                                     'remaining': max(0, b+plus-used), 'granted': bool(plus)}
        per_city[city] = {'base': base, 'allocated_base': assigned, 'unallocated': base-assigned,
                          'bonus': bonus, 'limit': assigned+bonus, 'maximum': assigned+possible,
                          'used': city_used[city], 'remaining': max(0, assigned+bonus-city_used[city]),
                          'channels': channel_rows}
    return {'policy': policy, 'base_total': sum(x['base'] for x in per_city.values()),
            'allocated_base_total': sum(x['allocated_base'] for x in per_city.values()),
            'unallocated_total': sum(x['unallocated'] for x in per_city.values()),
            'bonus_total': sum(x['bonus'] for x in per_city.values()),
            'effective_total': sum(x['limit'] for x in per_city.values()),
            'maximum_total': sum(x['maximum'] for x in per_city.values()),
            'used': task.get('usage', {}).get('searches', 0),
            'remaining': sum(x['remaining'] for x in per_city.values()), 'per_city': per_city}


def channel_progress(task):
    """Search allowance progress, independent of the number of planned queries."""
    inp = task['input']; priority = budget_policy(task) == PRIORITY
    city_used, channel_used, pair_used = counts(task)
    city_limits, pair_limits = limits(task) if priority else (inp['city_budgets'], {})
    rows = {}
    for channel in inp['channels']:
        used = channel_used[channel]
        city_remaining = {c: max(0, city_limits[c]-city_used[c]) for c in inp['cities']}
        if priority:
            allocated = sum(pair_limits[c, channel] for c in inp['cities'])
            remaining = sum(min(city_remaining[c], max(0, pair_limits[c, channel]-pair_used[c, channel])) for c in inp['cities'])
        else:
            allocated = min(sum(city_limits.values()), inp['channel_budgets'].get(channel, 5000))
            remaining = min(sum(city_remaining.values()), max(0, inp['channel_budgets'].get(channel, 5000)-used))
        unallocated = allocated == 0 and used == 0
        exhausted = not unallocated and remaining == 0
        percent = 0 if unallocated else (100 if exhausted else min(100, used/max(1, used+remaining)*100))
        rows[channel] = {'used': used, 'remaining': remaining, 'allocated': allocated,
            'percent': percent, 'exhausted': exhausted, 'unallocated': unallocated, 'shared': not priority}
    return rows
