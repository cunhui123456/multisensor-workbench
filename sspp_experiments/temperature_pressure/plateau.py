"""连续采集中的标定区间；只统计实收样本和人工参考记录。"""
from collections import deque
from copy import deepcopy
import math
import statistics


def number(value, name, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{name}须在 {low}–{high} 之间")
    return float(value)


def slope(values):
    if len(values) < 2:
        return None
    mt = statistics.fmean(t for t, v in values)
    mv = statistics.fmean(v for t, v in values)
    denominator = sum((t-mt)**2 for t, v in values)
    return 60 * sum((t-mt)*(v-mv) for t, v in values) / denominator if denominator else None


def reference_stability(readings, end, settings):
    window = settings['window_s']
    selected = [(r['host_elapsed_s'], r['temperature_c']) for r in readings if end-window*1.1 <= r['host_elapsed_s'] <= end]
    count = len(selected)
    span = selected[-1][0]-selected[0][0] if count > 1 else 0
    # 30秒窗默认至少3次读数，覆盖90%窗口，末次距今不超过10%窗长，间隔不超过60%窗长，边界允许10%窗长的人工操作余量。
    sufficient = (count >= 3 and span >= .9*window and end-selected[-1][0] <= .1*window
                  and max(b[0]-a[0] for a,b in zip(selected,selected[1:])) <= .6*window)
    spread = max(v for t,v in selected)-min(v for t,v in selected) if count > 1 else None
    rate = slope(selected)
    status = ('insufficient_reference' if not sufficient else
              'within_limits' if spread <= settings['temperature_range_c'] and abs(rate) <= settings['temperature_rate_c_per_min'] else 'outside_limits')
    return {'status':status, 'count':count, 'span_s':span, 'range_c':spread, 'rate_c_per_min':rate,
            'mean_c':statistics.fmean(v for t,v in selected) if count else None}


def validate_settings(value):
    value = value or {}
    return {key:number(value.get(key,default),key,low,high) for key,default,low,high in (
        ('window_s',30,10,600), ('temperature_range_c',.2,.001,10), ('temperature_rate_c_per_min',.1,.001,10))} | {
        'voltage_rate_limit_v_per_min': None if value.get('voltage_rate_limit_v_per_min') is None else
        number(value['voltage_rate_limit_v_per_min'],'电压漂移门槛',.000001,10)}


class PlateauRecorder:
    def __init__(self):
        self.active = None
        self.points = []
        self.recent = deque(maxlen=30000)
        self.references = deque(maxlen=5000)
        self.live_settings = validate_settings({})

    def command(self, command):
        op, t = command['operation'], command['host_elapsed_s']
        if op == 'begin':
            if self.active is not None:
                raise ValueError('请先结束当前标定区间')
            if len(self.points) >= 100:
                raise ValueError('本次记录最多100个区间，请另起记录')
            settings = validate_settings(command.get('settings'))
            target = number(command['target_temperature_c'],'目标温度',-20,100)
            source = str(command.get('reference_source','')).strip()
            if not source or len(source)>160:
                raise ValueError('请填写参考仪器名称（最多160字）')
            self.live_settings = settings
            self.active = {'id':len(self.points)+1, 'start_s':t, 'start_utc':command['host_time_utc'],
                           'target_temperature_c':target, 'reference_source':source, 'settings':settings,
                           'references':[], 'channels':{}, 'quality_flags':[]}
        elif op == 'reference':
            if self.active is None:
                raise ValueError('请先开始标定区间')
            if len(self.active['references']) >= 3600:
                raise ValueError('单区间最多3600次参考读数')
            ref = {'temperature_c':number(command['temperature_c'],'实测参考温度',-20,100),
                   'host_elapsed_s':t, 'host_time_utc':command['host_time_utc'], 'source':'manual_observation_at_click'}
            self.active['references'].append(ref)
            self.references.append(ref)
        elif op in ('end','interrupt'):
            if self.active is None:
                raise ValueError('没有正在记录的标定区间')
            point = self.active
            point.update(end_s=t, end_utc=command['host_time_utc'], duration_s=t-point['start_s'],
                         status='saved' if op=='end' else 'interrupted')
            point['reference_stability'] = reference_stability(point['references'],t,point['settings'])
            point['reference_mean_c'] = statistics.fmean(r['temperature_c'] for r in point['references']) if point['references'] else None
            temperatures = [r['temperature_c'] for r in point['references']]
            point['reference_interval_range_c'] = max(temperatures)-min(temperatures) if len(temperatures)>1 else None
            if point['reference_interval_range_c'] is not None and point['reference_interval_range_c']>point['settings']['temperature_range_c']:
                point['quality_flags'].append('reference_variation_in_interval')
            if point['duration_s'] < point['settings']['window_s']:
                point['quality_flags'].append('short_interval')
            if not point['references']:
                point['quality_flags'].append('no_reference')
            if not point['channels']:
                point['quality_flags'].append('no_voltage_samples')
            for stat in point['channels'].values():
                m2 = stat.pop('m2')
                stat['sample_sd_v'] = math.sqrt(max(0,m2)/(stat['count']-1)) if stat['count']>1 else None
                denominator = stat.pop('sum_t2')-stat['sum_t']**2/stat['count']
                stat['rate_v_per_min'] = 60*(stat.pop('sum_tv')-stat['sum_t']*stat['mean_v'])/denominator if denominator>0 else None
                stat.pop('sum_t')
                limit = point['settings']['voltage_rate_limit_v_per_min']
                stat['drift_status'] = ('limit_not_set' if limit is None else 'insufficient_voltage' if stat['rate_v_per_min'] is None else
                                        'within_limits' if abs(stat['rate_v_per_min'])<=limit else 'outside_limits')
                if stat['drift_status']=='outside_limits':
                    point['quality_flags'].append('voltage_drift_above_limit')
                stat['sample_span_s'] = stat['last_s']-stat['first_s']
                stat['time_segments'] = sorted(stat['time_segments'])
                if len(stat['time_segments'])>1:
                    point['quality_flags'].append('mcu_reset_in_interval')
                if stat['max_gap_s']>3 or stat['first_s']-point['start_s']>3 or t-stat['last_s']>3:
                    point['quality_flags'].append('voltage_data_gap')
            point['quality_flags'] = sorted(set(point['quality_flags']))
            point['requires_manual_review'] = True
            point['automatic_calibration_activated'] = False
            self.points.append(point)
            self.active = None
        else:
            raise ValueError('未知标定区间命令')

    def add_rows(self, rows):
        for row in rows:
            t,v,ch = row['host_elapsed_s'],row['voltage_v'],row['channel']
            self.recent.append((t,v,ch,row['time_segment']))
            if self.active is None or t < self.active['start_s']:
                continue
            stats = self.active['channels']
            if ch not in stats:
                stats[ch] = {'count':0,'mean_v':0.,'m2':0.,'sum_t':0.,'sum_t2':0.,'sum_tv':0.,'first_s':t,'last_s':t,'max_gap_s':0.,'time_segments':set()}
            st = stats[ch]
            st['max_gap_s'] = max(st['max_gap_s'],t-st['last_s'])
            st['last_s'] = t
            st['time_segments'].add(row['time_segment'])
            dt = t-st['first_s']
            st['sum_t'] += dt;st['sum_t2'] += dt*dt;st['sum_tv'] += dt*v
            st['count'] += 1
            delta = v-st['mean_v'];st['mean_v'] += delta/st['count'];st['m2'] += delta*(v-st['mean_v'])
        if rows:
            cutoff = rows[-1]['host_elapsed_s']-600
            while self.recent and self.recent[0][0]<cutoff:
                self.recent.popleft()

    def snapshot(self, now):
        settings = self.active['settings'] if self.active else self.live_settings
        refs = self.active['references'] if self.active else list(self.references)
        reference = reference_stability(refs,now,settings)
        channels = {}
        for ch in ('S6','S7'):
            values = [(t,v,seg) for t,v,c,seg in self.recent if c==ch and now-settings['window_s']<=t<=now]
            if not values:
                continue
            pairs = [(t,v) for t,v,seg in values]
            rate = slope(pairs)
            covered = (len(values)>=2 and values[-1][0]-values[0][0]>=.9*settings['window_s']
                       and now-values[-1][0]<=3 and len({seg for t,v,seg in values})==1
                       and max(b[0]-a[0] for a,b in zip(values,values[1:]))<=3)
            limit = settings['voltage_rate_limit_v_per_min']
            channels[ch] = {'mean_v':statistics.fmean(v for t,v in pairs),
                            'sample_sd_v':statistics.stdev(v for t,v in pairs) if len(pairs)>1 else None,
                            'rate_v_per_min':rate,
                            'status':'insufficient_voltage' if not covered else 'limit_not_set' if limit is None else
                            'within_limits' if abs(rate)<=limit else 'outside_limits'}
        return {'active':None if self.active is None else {'id':self.active['id'],'start_s':self.active['start_s'],
                    'elapsed_s':now-self.active['start_s'],'reference_count':len(self.active['references'])},
                'reference':reference,'channels':channels,
                'points':[{'id':p['id'],'duration_s':p['duration_s'],'status':p['status'],
                           'reference_mean_c':p['reference_mean_c'],'channels':{ch:{'count':v['count'],'mean_v':v['mean_v'],'sample_sd_v':v['sample_sd_v']} for ch,v in p['channels'].items()},
                           'reference_status':p['reference_stability']['status'],'quality_flags':p['quality_flags']} for p in self.points]}

    def document(self):
        return deepcopy({'schema':'sspp.temperature_plateaus.v1','points':self.points})
