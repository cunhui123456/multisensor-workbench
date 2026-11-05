"""Manual force-reference intervals; preserve voltage observations and candidate fits.

No instrument control, automatic calibration activation, or inferred contact area.
"""
from collections import defaultdict
from copy import deepcopy
import math
import statistics

from .plateau import PlateauRecorder, number, slope

SCHEMA = 'sspp.pressure_plateaus.v1'


def validate_settings(value=None):
    value = value or {}
    if not isinstance(value, dict):
        raise ValueError('压力判稳设置必须为对象')
    return {key: number(value.get(key, default), key, low, high)
            for key, default, low, high in (
                ('window_s', 30, 10, 600),
                ('force_range_n', .1, .000001, 1000000),
                ('force_rate_n_per_min', .1, .000001, 1000000))} | {
        'voltage_rate_limit_v_per_min': None if value.get('voltage_rate_limit_v_per_min') is None else
        number(value['voltage_rate_limit_v_per_min'], '电压漂移门槛', .000001, 10)}


def reference_stability(readings, end, settings):
    window = settings['window_s']
    selected = [(r['host_elapsed_s'], r['force_n']) for r in readings
                if end-window*1.1 <= r['host_elapsed_s'] <= end]
    count = len(selected)
    span = selected[-1][0]-selected[0][0] if count > 1 else 0
    sufficient = (count >= 3 and span >= .9*window and end-selected[-1][0] <= .1*window
                  and max(b[0]-a[0] for a, b in zip(selected, selected[1:])) <= .6*window)
    spread = max(v for _, v in selected)-min(v for _, v in selected) if count > 1 else None
    rate = slope(selected)
    status = ('insufficient_reference' if not sufficient else
              'within_limits' if spread <= settings['force_range_n'] and rate is not None
              and abs(rate) <= settings['force_rate_n_per_min'] else 'outside_limits')
    return {'status': status, 'count': count, 'span_s': span, 'range_n': spread,
            'rate_n_per_min': rate, 'mean_n': statistics.fmean(v for _, v in selected) if count else None}


def candidate_fits(points):
    """Descriptive inverse linear fits, separated by channel and loading conditions.

Quality eligibility is not independent validation. No fit is activated; consumers
must retain the source identity and must not extrapolate beyond the saved ranges.
"""
    groups = defaultdict(list)
    for point in points:
        channel = point.get('pressure_channel')
        if not channel:
            continue
        key = (channel, point.get('direction'), point.get('cycle'),
               point.get('contact_area_mm2'), point.get('reference_source'))
        groups[key].append(point)
    results = {}
    for index, (key, group) in enumerate(groups.items(), 1):
        channel, direction, cycle, area, source = key
        result = {'status': 'insufficient_points', 'channel': channel, 'direction': direction,
                  'cycle': cycle, 'contact_area_mm2': area, 'reference_source': source,
                  'automatic_calibration_activated': False, 'independent_validation': False,
                  'model': 'force_n = slope_n_per_v * voltage_v + intercept_n',
                  'limitations': ['仅候选线性拟合；误差为样本内残差，不代表独立精度',
                                  '仅适用于所列通道、方向、循环、面积及参考来源；不得外推'],
                  'point_ids': [], 'excluded_point_ids': [], 'point_count': 0}
        pairs = []
        for point in group:
            stat = point.get('channels', {}).get(channel, {})
            x, y = stat.get('mean_v'), point.get('reference_mean_n')
            valid = (point.get('status') == 'saved' and not point.get('quality_flags')
                     and not any(point.get(k) for k in ('deleted', 'excluded', 'soft_deleted', 'deleted_by_correction'))
                     and point.get('reference_stability', {}).get('status') == 'within_limits'
                     and stat.get('count', 0) >= 2
                     and all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in (x, y)))
            if valid:
                pairs.append((x, y))
                result['point_ids'].append(point.get('id'))
            else:
                result['excluded_point_ids'].append(point.get('id'))
        result['point_count'] = len(pairs)
        if len(pairs) >= 3 and len({y for _, y in pairs}) >= 3:
            mx, my = statistics.fmean(x for x, _ in pairs), statistics.fmean(y for _, y in pairs)
            denominator = sum((x-mx)**2 for x, _ in pairs)
            if len({x for x, _ in pairs}) < 3 or denominator <= max(1., mx*mx) * 1e-20:
                result['status'] = 'degenerate_voltage'
            else:
                coefficient = sum((x-mx)*(y-my) for x, y in pairs)/denominator
                intercept = my-coefficient*mx
                residual_sum = sum((y-(coefficient*x+intercept))**2 for x, y in pairs)
                total = sum((y-my)**2 for _, y in pairs)
                result.update(status='candidate', slope_n_per_v=coefficient, intercept_n=intercept,
                              rmse_n=math.sqrt(residual_sum/len(pairs)), r_squared=1-residual_sum/total,
                              voltage_min_v=min(x for x, _ in pairs), voltage_max_v=max(x for x, _ in pairs),
                              force_min_n=min(y for _, y in pairs), force_max_n=max(y for _, y in pairs))
                if area is not None:
                    result.update(slope_kpa_per_v=coefficient*1000/area, intercept_kpa=intercept*1000/area)
        results[f'group_{index}'] = result
    return results


class PressureRecorder(PlateauRecorder):
    """Reuse voltage-only streaming statistics; force metadata has its own schema."""

    def __init__(self, channels=('A2',)):
        super().__init__()
        self.available_channels = tuple(channels)
        self.live_settings = validate_settings()
        self.last_command_s = None

    def command(self, command):
        op = command['operation']
        t = number(command['host_elapsed_s'], '操作时间', 0, 1e12)
        if self.last_command_s is not None and t < self.last_command_s:
            raise ValueError('标定操作时间不能倒退')
        stamp = command.get('host_time_utc')
        if not isinstance(stamp, str) or not stamp:
            raise ValueError('缺少操作时间戳')
        if op == 'begin':
            if self.active is not None:
                raise ValueError('请先结束当前标定区间')
            if len(self.points) >= 100:
                raise ValueError('本次记录最多100个区间，请另起记录')
            settings = validate_settings(command.get('settings'))
            target = number(command['target_force_n'], '目标法向接触力（N）', 0, 1e6)
            source = command.get('reference_source', '')
            if not isinstance(source, str) or not source.strip() or len(source.strip()) > 160:
                raise ValueError('请填写参考测力仪器名称（最多160字）')
            channel = command.get('pressure_channel')
            if channel not in self.available_channels:
                raise ValueError('请明确选择实际接入压力传感器的通道')
            direction = command.get('direction')
            if direction not in ('loading', 'unloading', 'hold'):
                raise ValueError('请选择加载、卸载或保持方向')
            cycle = command.get('cycle')
            if isinstance(cycle, bool) or not isinstance(cycle, int) or not 1 <= cycle <= 10000:
                raise ValueError('循环编号须为1–10000的整数')
            area = command.get('contact_area_mm2')
            if area is not None:
                area = number(area, '已知接触面积（mm²）', 1e-9, 1e9)
            self.live_settings = settings
            self.active = {'id': len(self.points)+1, 'start_s': t, 'start_utc': stamp,
                           'target_force_n': target, 'reference_source': source.strip(),
                           'pressure_channel': channel, 'direction': direction, 'cycle': cycle,
                           'contact_area_mm2': area, 'settings': settings,
                           'references': [], 'channels': {}, 'quality_flags': []}
        elif op == 'reference':
            if self.active is None:
                raise ValueError('请先开始标定区间')
            if len(self.active['references']) >= 3600:
                raise ValueError('单区间最多3600次参考读数')
            value = number(command['force_n'], '实测法向接触力（N）', 0, 1e6)
            ref = {'force_n': value, 'host_elapsed_s': t, 'host_time_utc': stamp,
                   'source': 'manual_observation_at_click'}
            self.active['references'].append(ref)
            self.references.append(ref)
        elif op in ('end', 'interrupt'):
            if self.active is None:
                raise ValueError('没有正在记录的标定区间')
            point = deepcopy(self.active)
            point.update(end_s=t, end_utc=stamp, duration_s=t-point['start_s'],
                         status='saved' if op == 'end' else 'interrupted')
            self._finish_point(point, t)
            self.points.append(point)
            self.active = None
        else:
            raise ValueError('未知压力标定命令')
        self.last_command_s = t

    def _finish_point(self, point, end):
        flags = point['quality_flags']
        refs = point['references']
        point['reference_stability'] = reference_stability(refs, end, point['settings'])
        forces = [r['force_n'] for r in refs]
        point['reference_mean_n'] = statistics.fmean(forces) if forces else None
        area = point['contact_area_mm2']
        point['reference_mean_kpa'] = point['reference_mean_n']*1000/area if area is not None and forces else None
        point['reference_interval_range_n'] = max(forces)-min(forces) if len(forces) > 1 else None
        if point['reference_interval_range_n'] is not None and point['reference_interval_range_n'] > point['settings']['force_range_n']:
            flags.append('reference_variation_in_interval')
        if point['duration_s'] < point['settings']['window_s']:
            flags.append('short_interval')
        if not refs:
            flags.append('no_reference')
        if not point['channels']:
            flags.append('no_voltage_samples')
        if point['pressure_channel'] not in point['channels']:
            flags.append('missing_pressure_channel')
        for channel, stat in point['channels'].items():
            m2 = stat.pop('m2')
            stat['sample_sd_v'] = math.sqrt(max(0, m2)/(stat['count']-1)) if stat['count'] > 1 else None
            denominator = stat.pop('sum_t2')-stat['sum_t']**2/stat['count']
            stat['rate_v_per_min'] = 60*(stat.pop('sum_tv')-stat['sum_t']*stat['mean_v'])/denominator if denominator > 0 else None
            stat.pop('sum_t')
            limit = point['settings']['voltage_rate_limit_v_per_min']
            stat['drift_status'] = ('limit_not_set' if limit is None else 'insufficient_voltage' if stat['rate_v_per_min'] is None else
                                   'within_limits' if abs(stat['rate_v_per_min']) <= limit else 'outside_limits')
            stat['sample_span_s'] = stat['last_s']-stat['first_s']
            stat['time_segments'] = sorted(stat['time_segments'])
            if len(stat['time_segments']) > 1:
                flags.append('mcu_reset_in_interval')
            # Raw auxiliary channels remain visible; only the selected channel controls fit eligibility.
            if channel == point['pressure_channel']:
                if stat['count'] < 2:
                    flags.append('insufficient_voltage_samples')
                if stat['drift_status'] == 'outside_limits':
                    flags.append('voltage_drift_above_limit')
                if stat['max_gap_s'] > 3 or stat['first_s']-point['start_s'] > 3 or end-stat['last_s'] > 3:
                    flags.append('voltage_data_gap')
        point['quality_flags'] = sorted(set(flags))
        point['requires_manual_review'] = True
        point['automatic_calibration_activated'] = False

    def snapshot(self, now):
        settings = self.active['settings'] if self.active else self.live_settings
        refs = self.active['references'] if self.active else []
        channels = {}
        for channel in self.available_channels:
            values = [(t, v, seg) for t, v, ch, seg in self.recent
                      if ch == channel and now-settings['window_s'] <= t <= now]
            if not values:
                continue
            pairs = [(t, v) for t, v, _ in values]
            rate = slope(pairs)
            covered = (len(values) >= 2 and values[-1][0]-values[0][0] >= .9*settings['window_s']
                       and now-values[-1][0] <= 3 and len({seg for _, _, seg in values}) == 1
                       and max(b[0]-a[0] for a, b in zip(values, values[1:])) <= 3)
            limit = settings['voltage_rate_limit_v_per_min']
            channels[channel] = {'mean_v': statistics.fmean(v for _, v in pairs),
                                 'sample_sd_v': statistics.stdev(v for _, v in pairs) if len(pairs) > 1 else None,
                                 'rate_v_per_min': rate,
                                 'status': 'insufficient_voltage' if not covered else 'limit_not_set' if limit is None else
                                 'within_limits' if abs(rate) <= limit else 'outside_limits'}
        point_keys = ('id', 'duration_s', 'status', 'pressure_channel', 'direction', 'cycle',
                      'contact_area_mm2', 'reference_mean_n', 'reference_mean_kpa', 'quality_flags')
        points = [{**{key: p[key] for key in point_keys},
                   'channels': {ch: {k: stat[k] for k in ('count', 'mean_v', 'sample_sd_v')}
                                for ch, stat in p['channels'].items()},
                   'reference_status': p['reference_stability']['status']} for p in self.points]
        return {'schema': SCHEMA, 'active': None if self.active is None else {
                    'id': self.active['id'], 'start_s': self.active['start_s'],
                    'elapsed_s': now-self.active['start_s'], 'reference_count': len(self.active['references'])},
                'reference': reference_stability(refs, now, settings), 'channels': channels,
                'points': points, 'candidate_fits': candidate_fits(self.points)}

    def document(self):
        return deepcopy({'schema': SCHEMA, 'points': self.points,
                         'candidate_fits': candidate_fits(self.points), 'automatic_calibration_activated': False})
