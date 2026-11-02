"""扫频完成条件与单点保护说明；所有扫频均无总时限。"""


def budget_info(config):
    if config.get('mode') != 'sweep':
        return None
    continuous = (config.get('temperature_sweep') or {}).get('mode') == 'continuous'
    point_timeout = float(config['point_timeout'])
    extra = float(config.get('weak_point_extra_seconds', 8.))
    info = {'continuous': continuous, 'max_seconds': None,
            'point_timeout_seconds': point_timeout, 'weak_point_extra_seconds':extra,
            'point_max_timeout_seconds':point_timeout+extra, 'warning': None}
    point_summary = (f'单点基础等待 {point_timeout:g} 秒；已确认目标的弱点可额外等待 {extra:g} 秒，'
                     f'每点最多 {point_timeout+extra:g} 秒，最多启用一次。' if extra else
                     f'单点时限 {point_timeout:g} 秒；弱点额外等待已关闭。')
    if continuous:
        info['summary'] = '连续变温：无总时限，持续记录至手动停止；'+point_summary
    else:
        points, repeats = int(config['points']), int(config['repeat'])
        info.update(points=points, repeat=repeats, total_positions=points*repeats)
        info['summary'] = (f'扫频计划：{points}点×{repeats}轮={points*repeats}个位置；'
                           '无总时限，按设定轮数完成；'+point_summary)
    return info
