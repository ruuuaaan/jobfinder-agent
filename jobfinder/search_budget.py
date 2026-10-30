"""Shared deterministic allocation policy for saved effective search specs."""
PRIORITY = 'official_priority_once'
OFFICIAL = 'official_career'
BASE_OTHER = 2
FAILURE_ALLOWANCE = 1


def validate_allocation(cities, channels, per_city, per_channel, policy):
    if policy != PRIORITY:
        return
    if per_channel:
        raise ValueError('官网优先分配使用每城其它渠道各2次；手动渠道上限请改用共享模式')
    minimum = BASE_OTHER * sum(c != OFFICIAL for c in channels)
    for city in cities:
        if per_city[city] < minimum:
            raise ValueError(f'{city}基础预算至少需要{minimum}次，才能给其它已选渠道各2次；请提高预算或减少渠道')


def base_allocation(cities, channels, per_city):
    other_total = BASE_OTHER * sum(c != OFFICIAL for c in channels)
    return {city: {channel: max(0, per_city[city] - other_total) if channel == OFFICIAL else BASE_OTHER
                   for channel in channels} for city in cities}
