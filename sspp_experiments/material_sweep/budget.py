"""扫频完成条件与单点保护说明；所有扫频均无总时限。"""


def budget_info(config):
    if config.get('mode') != 'sweep':
        return None
    continuous = (config.get('temperature_sweep') or {}).get('mode') == 'continuous'
    point_timeout = float(config['point_timeout'])
    info = {'continuous': continuous, 'max_seconds': None,
            'point_timeout_seconds': point_timeout, 'warning': None}
    if continuous:
        info['summary'] = f'连续变温：无总时限，持续记录至手动停止；单点时限 {point_timeout:g} 秒。'
    else:
        points, repeats = int(config['points']), int(config['repeat'])
        info.update(points=points, repeat=repeats, total_positions=points*repeats)
        info['summary'] = (f'扫频计划：{points}点×{repeats}轮={points*repeats}个位置；'
                           f'无总时限，按设定轮数完成；单点时限 {point_timeout:g} 秒。')
    return info
