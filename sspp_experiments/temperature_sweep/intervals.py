"""整轮扫频电压统计与探索性频点敏感性排名；不推断温度因果。"""
import csv
import json
import math
import statistics
from .metadata import calibrated_temperature


def summarize_round(index, start, end, samples, complete, calibration, data_lost=False):
    rows = [(t,v,seg) for t,v,seg in samples if start <= t <= end]
    values = [v for t,v,seg in rows]
    flags = []
    if not complete:flags.append('partial_sweep')
    if len(values)<2:flags.append('insufficient_samples')
    if data_lost:flags.append('preview_data_loss')
    if rows and (rows[0][0]-start>3 or end-rows[-1][0]>3 or any(b[0]-a[0]>3 for a,b in zip(rows,rows[1:]))):flags.append('sample_gap')
    if len({seg for t,v,seg in rows})>1:flags.append('mcu_reset')
    mean = statistics.fmean(values) if values else None
    temperature,status = calibrated_temperature(mean,calibration) if mean is not None else (None,'no_samples')
    if calibration and any(not calibration['voltage_min']<=v<=calibration['voltage_max'] for v in values):
        temperature,status=None,'outside_calibration_range'
    return {'repeat':index,'started_monotonic_s':start,'ended_monotonic_s':end,'duration_s':end-start,
            'complete':bool(complete),'sample_count':len(values),'voltage_mean_v':mean,
            'voltage_sd_v':statistics.stdev(values) if len(values)>1 else None,
            'voltage_min_v':min(values) if values else None,'voltage_max_v':max(values) if values else None,
            'voltage_span_v':max(values)-min(values) if values else None,
            'temperature_c':temperature,'calibration_status':status,'quality_flags':flags,
            'mean_definition':'arithmetic mean of received selected-channel voltages during full sweep; not hardware synchronized'}


def frequency_ranking(rounds, measurements, calibration):
    valid = {r['repeat']:r for r in rounds if r['complete'] and not (set(r['quality_flags'])-{'invalid_frequency_points'})}
    groups = {}
    for m in measurements:
        if m.get('measurement_status')=='invalid' or m.get('amplitude_dB') in ('',None):continue
        interval=valid.get(int(m['repeat']))
        if interval is None:continue
        x=interval['temperature_c'] if calibration else interval['voltage_mean_v']
        if x is None:continue
        f=float(m['carrier_hz']);tone=float(m['tone_hz'])
        groups.setdefault((f,tone),[]).append((x,float(m['amplitude_dB'])))
    result=[]
    for (f,tone),pairs in groups.items():
        if len(pairs)<3 or len({x for x,y in pairs})<3:continue
        mx=statistics.fmean(x for x,y in pairs);my=statistics.fmean(y for x,y in pairs)
        xx=sum((x-mx)**2 for x,y in pairs);yy=sum((y-my)**2 for x,y in pairs)
        if xx<=1e-18:continue
        xy=sum((x-mx)*(y-my) for x,y in pairs);slope=xy/xx
        result.append({'carrier_hz':f,'tone_hz':tone,'observations':len(pairs),
                       'slope':slope,'slope_unit':'dB/degC' if calibration else 'dB/V',
                       'r_squared':xy*xy/(xx*yy) if yy>1e-18 else 0.,
                       'x_span':max(x for x,y in pairs)-min(x for x,y in pairs),
                       'amplitude_span_db':max(y for x,y in pairs)-min(y for x,y in pairs)})
    result.sort(key=lambda r:abs(r['slope']),reverse=True)
    return [{'rank':i+1,**r} for i,r in enumerate(result)]


def save_analysis(output, rounds, calibration):
    with (output/'temperature_measurements.csv').open() as f:measurements=list(csv.DictReader(f))
    ranking=frequency_ranking(rounds,measurements,calibration)
    document={'schema':'sspp.temperature_sweep_intervals.v1','rounds':rounds,'ranking':ranking,
              'calibration':calibration,'limitations':[
                  'Each curve is sequentially swept while temperature may change; round mean is a representative label, not isothermal data',
                  'Frequency ranking is exploratory; time drift, heating direction, RF quality and within-round voltage span must be checked',
                  'No calibrated temperature without recorded coefficients; no calibrated S21 or temperature causality claim',
                  'Ascending sweep order is fixed; confirm candidate bands using slower ramps, reverse order or static platforms']}
    (output/'temperature_intervals.json').write_text(json.dumps(document,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
    for name,rows in (('temperature_rounds.csv',rounds),('frequency_sensitivity.csv',ranking)):
        fields=list(rows[0]) if rows else (['repeat','complete','voltage_mean_v','temperature_c'] if name.startswith('temperature') else ['rank','carrier_hz','slope','slope_unit'])
        with (output/name).open('w',newline='') as f:
            writer=csv.DictWriter(f,fieldnames=fields);writer.writeheader();writer.writerows(rows)
    return document
