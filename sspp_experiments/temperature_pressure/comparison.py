"""Read-only serial overlays and combined manual calibration observations."""
from __future__ import annotations

import csv
from datetime import datetime
import hashlib
import io
import json
import math
import re
from ..comparison import COLORS, chart, read_json_source, read_source, table

ARTIFACTS = ("samples.csv", "raw_serial.bin", "serial_events.jsonl", "voltage_response.csv",
             "calibration_points.json", "calibration_events.jsonl")


def prepare_serial_record(record):
    manifest = record["_manifest"]
    if (manifest.get("status") != "complete" or manifest.get("errors", []) != []
            or not all(manifest.get(name) is True for name in
                       ("serial_closed", "workers_stopped", "data_files_closed"))):
        raise ValueError(f"记录 {record['key']} 未封存、存在错误或缺少串口完整停止证据，不能参与组合分析")
    points = read_json_source(record, "calibration_points.json", required=False)
    record["kind"] = ("pressure_calibration" if manifest.get("pressure_calibration") or points.get("schema") == "sspp.pressure_plateaus.v1" else
                      "temperature_calibration" if manifest.get("temperature_calibration")
                      or manifest.get("temperature_ramp") or points else "temperature_pressure")
    record["_calibration"] = points
    serial = manifest.get("serial") or {}
    record["parameters"] = {"protocol": manifest.get("protocol", serial.get("protocol")),
                            "baudrate": serial.get("baudrate"),
                            "channels": manifest.get("channels"),
                            "time_axis": "received_host_elapsed_s",
                            "calibration_activation": "none"}


def _number(value, name, minimum=None, maximum=None, optional=False):
    if optional and value in (None, ""):
        return None
    if isinstance(value, bool):
        raise ValueError(f"串口来源的 {name} 不是有限数值")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"串口来源的 {name} 不是有限数值") from exc
    if (not math.isfinite(number) or (minimum is not None and number < minimum)
            or (maximum is not None and number > maximum)):
        raise ValueError(f"串口来源的 {name} 超出有效范围")
    return number


def _channel(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,31}", value):
        raise ValueError("串口来源通道名无效")
    return value


def _bind_files(record, report):
    declared = record["_manifest"].get("files", [])
    if not isinstance(declared, list):
        raise ValueError("串口封存文件索引无效")
    expected = {}
    for entry in declared:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise ValueError("串口封存文件索引无效")
        if entry["path"] in expected:
            raise ValueError("串口封存文件索引含重复路径")
        expected[entry["path"]] = entry
    unbound = []
    for name in ARTIFACTS:
        content = read_source(record, name, required=(name=="samples.csv" or
                              (name=="calibration_points.json" and record["kind"] in ("temperature_calibration", "pressure_calibration"))))
        if content is None:
            continue
        saved = expected.get(name)
        if saved is not None:
            if saved.get("sha256") != hashlib.sha256(content).hexdigest() or saved.get("bytes") != len(content):
                raise ValueError(f"记录 {record['key']} 封存文件校验失败：{name}")
        else:
            unbound.append(name)
    if unbound:
        report["warnings"].append(f"{record['key']} 的部分旧文件没有原始封存哈希（{'、'.join(unbound)}）；本次已绑定读取内容哈希，导出时再次核验。")


def _samples(record):
    try:
        raw = list(csv.DictReader(io.StringIO(read_source(record, "samples.csv").decode("utf-8-sig"))))
    except (UnicodeDecodeError, csv.Error) as exc:
        raise ValueError("串口 samples.csv 无法解析") from exc
    if not raw:
        raise ValueError(f"记录 {record['key']} 没有串口实收样本")
    parsed, last = [], {}
    for index, row in enumerate(raw, 1):
        channel = _channel(row.get("channel"))
        elapsed = _number(row.get("host_elapsed_s"), "host_elapsed_s", 0)
        voltage = _number(row.get("voltage_v"), "voltage_v", 0, 5.1)
        segment = _number(row.get("time_segment"), "time_segment", 0)
        if not segment.is_integer() or elapsed < last.get(channel, -1):
            raise ValueError("串口样本时间段无效或主机实收时间倒退")
        last[channel] = elapsed
        parsed.append({"key":record["key"], "label":record["label"], "row_index":index,
                       "channel":channel, "host_elapsed_s":elapsed, "voltage_v":voltage,
                       "time_segment":int(segment), "mcu_time_ms":_number(row.get("mcu_time_ms"), "mcu_time_ms", 0),
                       "adc":_number(row.get("adc"), "adc", 0, 1023, optional=True)})
        if record["_manifest"].get("protocol") == "pressure_v2":
            parsed[-1].update(
                baseline_v=_number(row.get("baseline_v"), "baseline_v", 0, 5.1, optional=True),
                delta_voltage_v=_number(row.get("delta_voltage_v"), "delta_voltage_v", -5.1, 5.1, optional=True),
                zero_id=_number(row.get("zero_id"), "zero_id", 0),
                zero_count=_number(row.get("zero_count"), "zero_count", 0, 40),
                selection_token=_number(row.get("selection_token"), "selection_token", 1, 4294967295),
            )
    declared_count = record["_manifest"].get("rows")
    if declared_count is not None and declared_count != len(parsed):
        raise ValueError("串口实收样本行数与封存 manifest 不一致")
    return parsed


def _publication_series(record, index, data, errors=None, label=None):
    result = {"label": f"{record['label']} · {record['key'].split('/')[-1]}",
              "publication_label": label or f"Run {index}", "run_index":index,
              "key":record["key"], "color":record["color"], "data":data,
              "x":[point[0] for point in data], "y":[point[1] for point in data]}
    if errors is not None:
        result.update(errors=errors, y_error=errors)
    return result


def _deleted_points(record):
    """Bind the derived correction ledger, including its absence, to this report."""
    relative = 'corrections/' + record['key'] + '.json'
    path = record['_data_root'] / relative
    if path.exists() and not path.is_file():
        raise ValueError('温标更正账本不是普通文件')
    binding = {'relative_path':relative, 'present':False}
    record['correction'] = binding
    source = {'key':record['key'], '_path':record['_data_root'], '_contents':{}, 'files':[]}
    content = read_source(source, relative, required=False)
    if content is None:
        return {}
    binding.update(source['files'][0], present=True)
    try:
        ledger = json.loads(content)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError('温标更正账本不是有效 JSON') from exc
    expected_hash = hashlib.sha256(read_source(record, 'calibration_points.json')).hexdigest()
    if (not isinstance(ledger, dict) or type(ledger.get('version')) is not int or ledger['version'] != 1
            or ledger.get('run_key') != record['key'] or ledger.get('source_calibration_sha256') != expected_hash
            or not isinstance(ledger.get('deleted_points'), dict)):
        raise ValueError('温标更正账本版本、来源或标定原文件哈希不匹配')
    points = record['_calibration'].get('points', [])
    identifiers = {str(point.get('id')) for point in points if isinstance(point, dict) and point.get('status') == 'saved'}
    for ident, deletion in ledger['deleted_points'].items():
        if (not re.fullmatch(r'[1-9][0-9]*', ident) or ident not in identifiers or not isinstance(deletion, dict)
                or not isinstance(deletion.get('reason'), str) or not deletion['reason'].strip()
                or not isinstance(deletion.get('deleted_utc'), str)):
            raise ValueError('温标更正账本删除项无效或不属于已保存区间')
        try:
            timestamp = datetime.fromisoformat(deletion['deleted_utc'].replace('Z', '+00:00'))
            if timestamp.tzinfo is None:
                raise ValueError('missing timezone')
        except ValueError as exc:
            raise ValueError('温标更正账本删除时间无效') from exc
    return ledger['deleted_points']


def _calibration_rows(record, run_index, report):
    points = record["_calibration"].get("points")
    if not isinstance(points, list):
        raise ValueError("标定来源 calibration_points.json 缺少 points 列表")
    deleted = _deleted_points(record)
    rows, seen, excluded = [], set(), 0
    for point in points:
        if not isinstance(point, dict):
            raise ValueError("标定区间结构无效")
        if point.get("status") != "saved":
            excluded += 1
            continue
        ident = point.get("id")
        if not isinstance(ident, int) or isinstance(ident, bool) or ident < 1 or ident in seen:
            raise ValueError("标定区间编号无效或重复")
        seen.add(ident)
        if str(ident) in deleted:
            deletion = deleted[str(ident)]
            report['excluded_calibration_points'].append({
                'run_key':record['key'], 'run_index':run_index, 'point_id':ident,
                'reference_mean_c':_number(point.get('reference_mean_c'), 'reference_mean_c', optional=True),
                'channels':sorted(point.get('channels', {})), 'reason':deletion['reason'],
                'deleted_utc':deletion['deleted_utc'], 'correction_sha256':record['correction']['sha256']})
            continue
        reference = _number(point.get("reference_mean_c"), "reference_mean_c", optional=True)
        if reference is None:
            excluded += 1
            continue  # Never replace an absent observed reference by target temperature.
        channels = point.get("channels")
        if not isinstance(channels, dict):
            raise ValueError("标定区间通道统计无效")
        flags = point.get("quality_flags", [])
        if not isinstance(flags, list) or not all(isinstance(f, str) for f in flags):
            raise ValueError("标定区间质量标记无效")
        stability = point.get("reference_stability") or {}
        if not isinstance(stability, dict):
            raise ValueError("标定参考稳定性结构无效")
        for channel, values in channels.items():
            _channel(channel)
            if not isinstance(values, dict):
                raise ValueError("标定通道统计结构无效")
            count = _number(values.get("count"), "count", 0)
            if not count.is_integer():
                raise ValueError("标定样本数必须为整数")
            if count == 0 or values.get("mean_v") is None:
                continue
            voltage = _number(values.get("mean_v"), "mean_v", 0, 5.1)
            sd = _number(values.get("sample_sd_v"), "sample_sd_v", 0, optional=True)
            if count < 2 and sd is not None:
                raise ValueError("单样本标定区间不能声明样本标准差")
            rows.append({"key":record["key"], "label":record["label"], "run_index":run_index,
                         "point_id":ident, "channel":channel, "reference_mean_c":reference,
                         "target_temperature_c":_number(point.get("target_temperature_c"), "target_temperature_c", optional=True),
                         "reference_source":point.get("reference_source"), "sample_count":int(count),
                         "mean_v":voltage, "sample_sd_v":sd,
                         "reference_stability_status":stability.get("status", "unknown"),
                         "requires_manual_review":point.get("requires_manual_review", True),
                         "quality_flags":flags, "duration_s":_number(point.get("duration_s"), "duration_s", 0, optional=True),
                         "start_s":_number(point.get("start_s"), "start_s", 0, optional=True),
                         "end_s":_number(point.get("end_s"), "end_s", 0, optional=True)})
    if not rows:
        raise ValueError(f"记录 {record['key']} 没有带实测参考温度和电压统计的已保存标定区间")
    if excluded:
        report["warnings"].append(f"{record['key']} 有 {excluded} 个未保存或缺少实测参考温度的区间未入图；没有用目标温度补值。")
    if deleted:
        report['warnings'].append(f"{record['key']} 有 {len(deleted)} 个已保存标定区间按错误数据更正账本排除（整区间所有通道）；原始文件与实收时间图保留，可恢复。")
    reviewed = {r["point_id"] for r in rows if r["requires_manual_review"] or
                r["reference_stability_status"] not in ("within_limits", "stable") or r["quality_flags"]}
    if reviewed:
        report["warnings"].append(f"{record['key']} 的 {len(reviewed)} 个标定区间需要人工复核（参考稳定性不足/未知或存在质量标记）；保留实测点用于分析展示，不宣称已验证恒温或完成自动标定。")
    return rows


def compare_serial(records, report):
    """Combine temperature observations without averaging, fitting, or activation."""
    pressure = records[0]["kind"] == "pressure_calibration"
    calibration = records[0]["kind"] == "temperature_calibration"
    if pressure:
        _compare_pressure(records, report)
    single = len(records) == 1
    if calibration:
        report['excluded_calibration_points'] = []
    all_samples, calibration_rows = [], []
    for index, record in enumerate(records, 1):
        if not pressure:
            _bind_files(record, report)
        samples = _samples(record)
        all_samples.extend(samples)
        metric = {"key":record["key"], "label":record["label"], "run_index":index,
                  "raw_sample_count":len(samples), "channel_count":len({r['channel'] for r in samples}),
                  "elapsed_duration_s":max(r['host_elapsed_s'] for r in samples)-min(r['host_elapsed_s'] for r in samples)}
        if calibration:
            values = _calibration_rows(record, index, report)
            calibration_rows.extend(values)
            references = [r["reference_mean_c"] for r in values]
            order = list(dict.fromkeys((r['point_id'], r['reference_mean_c']) for r in values))
            temperatures = [reference for ident, reference in order]
            direction = ("ascending" if len(temperatures)>1 and all(a<=b for a,b in zip(temperatures,temperatures[1:]))
                         else "descending" if len(temperatures)>1 and all(a>=b for a,b in zip(temperatures,temperatures[1:])) else "mixed_or_single")
            record['parameters'].update(reference_temperature_sequence_c=temperatures, reference_direction=direction)
            metric.update(saved_calibration_points=len({r['point_id'] for r in values}),
                          reference_min_c=min(references), reference_max_c=max(references),
                          manual_review_points=len({r['point_id'] for r in values if r['requires_manual_review']}),
                          excluded_calibration_points=sum(r['run_key']==record['key'] for r in report['excluded_calibration_points']))
        report["metrics"].append(metric)
        report["warnings"].extend(f"{record['key']}：{value}" for value in record['_manifest'].get('warnings', []) if isinstance(value, str))
    for parameter, label in (("protocol", "串口协议"), ("baudrate", "记录波特率"), ("channels", "记录通道")):
        values=[r['parameters'][parameter] for r in records]
        status="unknown" if any(v is None for v in values) else "same" if all(v==values[0] for v in values) else "different"
        report['compatibility'].append({'parameter':parameter,'label':label,'values':values,'status':status,
                                        'blocking':False,'note':'仅按来源叠加，不能由相同通道名推断传感器或接线相同。'})
    report['quantitative_difference_allowed']=False
    report['automatic_calibration_activated']=False
    if calibration:
        report['presentation_mode']='single_temperature_response' if single else 'combined_temperature_response'
        report['warnings'].append(
            '本记录温标观测点按参考温度排序展示；重复温度点保留各自观测及样本 SD，不平均、不插值、不自动拟合或激活温度换算。连线仅引导视线，不表示采样顺序或温度连续轨迹；采集方向及更正账本保留。' if single else
            '温标观测点按参考温度统一排序展示；这是多记录组合展示，并非一次连续采集。重复温度点保留各自观测及样本 SD，不平均、不插值、不自动拟合或激活温度换算。连接观测点仅作视觉引导，不代表跨记录采集连续。来源、采集方向及更正账本独立保留；通道名相同不证明传感器/接线或参考仪器相同。')
        channels=sorted({r['channel'] for r in calibration_rows})
        for channel in channels:
            # Python's stable sort keeps equal-temperature observations distinct
            # and their source order unchanged; no point or uncertainty is pooled.
            values=sorted((r for r in calibration_rows if r['channel']==channel),
                          key=lambda r:r['reference_mean_c'])
            data=[[r['reference_mean_c'],r['mean_v']] for r in values]
            errors=[r['sample_sd_v'] for r in values]
            source_keys=[record['key'] for record in records if any(r['key']==record['key'] for r in values)]
            series_label = 'Measured intervals' if single else 'Combined measurements'
            series={'label':series_label,'publication_label':series_label,
                    'key':records[0]['key'] if single else None,'run_index':1,'color':'#24445C','data':data,
                    'x':[point[0] for point in data],'y':[point[1] for point in data],
                    'errors':errors,'y_error':errors,
                    'point_sources':[{'key':r['key'],'point_id':r['point_id']} for r in values]}
            item=chart('temperature_calibration_'+channel,channel+(' 本记录标定响应：' if single else ' 组合标定响应：')+'实测区间均值 ± 样本 SD',
                       '参考温度（°C）','电压（V）',[series],
                       ('本记录已保存标定区间。' if single else '多记录组合展示，并非一次连续采集。')+
                       '横轴为各区间保存的参考温度均值；重复温度点各自保留，不平均、不拟合、不插值。误差棒是区间内电压样本 SD，不是温度不确定度或均值置信区间；连接观测点仅作视觉引导。')
            item['publication']={'xlabel':'Reference temperature (°C)','ylabel':'Voltage (V)','channel':channel,
                                 'title':channel+(' calibration response' if single else ' combined calibration response'),'kind':'calibration_response',
                                 'combined_sources':not single,'source_keys':source_keys}
            report['charts'].append(item)
        columns=list(calibration_rows[0])
        report['tables']['calibration_points']=table(columns,calibration_rows)
        report['tables']['excluded_calibration_points'] = table(
            ['run_key', 'run_index', 'point_id', 'reference_mean_c', 'channels', 'reason', 'deleted_utc', 'correction_sha256'],
            report['excluded_calibration_points'])
        report['warnings'].append('人工参考温度与连续电压按保存区间关联；参考稳定性不足的点继续标注人工复核，误差棒不表示独立重复实验或温度准确度。')
    elif not pressure:
        report['warnings'].append('展示本记录实收电压，不插值、不自动拟合或激活温度换算。' if single else
                                  '各来源独立保留，不合并重叠点、不跨记录连线、不插值、不自动拟合或激活温度换算。串口通道名相同不证明传感器/接线或参考仪器相同。')
    for channel in sorted({r['channel'] for r in all_samples}):
        plots=[]
        for index,record in enumerate(records,1):
            values=[r for r in all_samples if r['key']==record['key'] and r['channel']==channel]
            if not values:continue
            data=[];previous=None
            for row in values:
                if previous is not None and (row['time_segment']!=previous['time_segment'] or row['host_elapsed_s']-previous['host_elapsed_s']>3 or row.get('selection_token') != previous.get('selection_token')):
                    data.append([row['host_elapsed_s'],None])
                data.append([row['host_elapsed_s'],row['voltage_v']]);previous=row
            plots.append(_publication_series(record,index,data))
        item=chart('serial_voltage_'+channel,channel+(' 本记录实收电压' if single else ' 各记录实收电压叠加'),
                   '采集起点后的主机实收时间（s）' if single else '各自采集起点后的主机实收时间（s）','电压（V）',plots,
                   ('保留本记录采集起点后的实收时间。' if single else '保留各记录采集起点后的实收时间，不作跨会话时间对齐。')+
                   'MCU 分段或超过 3 秒的实收间隙断线显示，没有补点。')
        # Publication channel identifiers select response charts only for calibration.
        if not calibration:
            item['publication']={'xlabel':'Elapsed acquisition time (s)','ylabel':'Voltage (V)','channel':channel,'title':channel+' voltage response'}
        report['charts'].append(item)
    report['tables']['serial_samples']=table(list(dict.fromkeys(key for row in all_samples for key in row)),all_samples)


def _pressure_rows(record, run_index, report):
    """Keep manual force observations distinct from planned targets and voltage."""
    points = record['_calibration'].get('points')
    if not isinstance(points, list):
        raise ValueError('压力标定来源缺少 points 列表')
    deleted = _deleted_points(record)
    rows, eligible, seen, excluded = [], [], set(), 0
    for point in points:
        if not isinstance(point, dict):
            raise ValueError('压力标定区间结构无效')
        ident = point.get('id')
        if type(ident) is not int or ident < 1 or ident in seen:
            raise ValueError('压力标定区间编号无效或重复')
        seen.add(ident)
        if point.get('status') != 'saved':
            excluded += 1
            continue
        if str(ident) in deleted:
            deletion = deleted[str(ident)]
            report['excluded_calibration_points'].append({
                'run_key':record['key'], 'run_index':run_index, 'point_id':ident,
                'reference_mean_n':_number(point.get('reference_mean_n'),'reference_mean_n',0,optional=True),
                'channels':sorted(point.get('channels',{})), 'reason':deletion['reason'],
                'deleted_utc':deletion['deleted_utc'], 'correction_sha256':record['correction']['sha256']})
            continue
        reference = _number(point.get('reference_mean_n'),'reference_mean_n',0,optional=True)
        if reference is None:
            excluded += 1
            continue
        area = _number(point.get('contact_area_mm2'),'contact_area_mm2',0,optional=True)
        if area == 0:
            raise ValueError('接触面积必须大于零；未知时应留空')
        pressure = _number(point.get('reference_mean_kpa'),'reference_mean_kpa',0,optional=True)
        if pressure is not None and (area is None or not math.isclose(pressure,reference*1000/area,rel_tol=1e-8,abs_tol=1e-8)):
            raise ValueError('保存的平均压强与实测参考力／接触面积不一致')
        direction = point.get('direction')
        cycle = point.get('cycle')
        channel = _channel(point.get('pressure_channel'))
        if direction not in ('loading','unloading','hold') or type(cycle) is not int or cycle < 1:
            raise ValueError('压力标定加载方向或循环编号无效')
        flags = point.get('quality_flags',[])
        if not isinstance(flags,list) or not all(isinstance(flag,str) for flag in flags):
            raise ValueError('压力标定质量标记无效')
        values = point.get('channels',{}).get(channel)
        if not isinstance(values,dict) or values.get('mean_v') is None:
            excluded += 1
            continue
        count = _number(values.get('count'),'count',1)
        if not count.is_integer():
            raise ValueError('压力标定样本数必须为整数')
        voltage = _number(values.get('mean_v'),'mean_v',0,5.1)
        sd = _number(values.get('sample_sd_v'),'sample_sd_v',0,optional=True)
        if count < 2 and sd is not None:
            raise ValueError('单样本标定区间不能声明样本标准差')
        stability = point.get('reference_stability') or {}
        if not isinstance(stability,dict):
            raise ValueError('压力参考稳定性结构无效')
        rows.append({'key':record['key'],'label':record['label'],'run_index':run_index,'point_id':ident,
                     'channel':channel,'pressure_channel':channel,'reference_mean_n':reference,
                     'reference_mean_kpa':pressure,'contact_area_mm2':area,
                     'target_force_n':_number(point.get('target_force_n'),'target_force_n',0,optional=True),
                     'direction':direction,'cycle':cycle,'reference_source':point.get('reference_source'),
                     'sample_count':int(count),'mean_v':voltage,'sample_sd_v':sd,
                     'reference_stability_status':stability.get('status','unknown'),
                     'quality_flags':flags,'requires_manual_review':point.get('requires_manual_review',True),
                     'duration_s':_number(point.get('duration_s'),'duration_s',0,optional=True)})
        eligible.append(point)
    if excluded:
        report['warnings'].append(f"{record['key']} 有 {excluded} 个中断、缺少实测参考力或所选通道电压的区间未入响应图；目标载荷不补作参考值。")
    if deleted:
        report['warnings'].append(f"{record['key']} 有 {len(deleted)} 个错误区间按更正账本排除；原始采样保留，可恢复。")
    return rows, eligible


def _compare_pressure(records, report):
    from .pressure import candidate_fits
    report['excluded_calibration_points'] = []
    report['presentation_mode'] = 'single_pressure_response' if len(records)==1 else 'separate_pressure_responses'
    report['automatic_calibration_activated'] = False
    report['quantitative_difference_allowed'] = False
    rows, fits = [], []
    for index, record in enumerate(records,1):
        # Verify sealed data before interpreting summaries or deriving candidates.
        _bind_files(record,report)
        values, points = _pressure_rows(record,index,report)
        rows.extend(values)
        record['parameters']['pressure_calibration'] = record['_manifest'].get('pressure_calibration',{})
        for group, fit in candidate_fits(points).items():
            fits.append({'key':record['key'],'run_index':index,'group':group,**fit})
    if not rows:
        report['warnings'].append('没有同时具备实测参考力和所选通道电压的已保存压力区间；仍显示原始串口电压。')
    for channel in sorted({row['channel'] for row in rows}):
        for variable, unit, english in (('reference_mean_n','N','Normal contact force (N)'),
                                        ('reference_mean_kpa','kPa','Mean contact pressure (kPa)')):
            groups = {}
            for row in rows:
                if row['channel'] != channel or row[variable] is None:
                    continue
                identity=(row['key'],row['direction'],row['cycle'],row['contact_area_mm2'],row['reference_source'])
                groups.setdefault(identity,[]).append(row)
            if not groups:
                continue
            plots=[]
            for group_index,(identity,values) in enumerate(groups.items()):
                values=sorted(values,key=lambda row:row[variable])
                key,direction,cycle,area,reference_source=identity
                index=values[0]['run_index'];record=records[index-1]
                label=f"Run {index} · {direction} · cycle {cycle} · A={area if area is not None else 'unknown'} mm² · {reference_source}"
                plot=_publication_series(record,index,[[row[variable],row['mean_v']] for row in values],
                                         [row['sample_sd_v'] for row in values],label=label)
                plot['label']=label
                plot['color']=COLORS[group_index%len(COLORS)]
                plot['point_sources']=[{'key':row['key'],'point_id':row['point_id']} for row in values]
                plots.append(plot)
            pressure=variable=='reference_mean_kpa'
            item=chart(('pressure_kpa_' if pressure else 'pressure_force_')+channel,
                       channel+(' 平均压强—电压响应' if pressure else '法向接触力—电压响应'),
                       '平均压强（kPa）' if pressure else '参考法向接触力（N）','电压（V）',plots,
                       '按来源、方向、循环、接触面积和参考仪器分组展示，每个实测区间保留均值和样本 SD；连线仅引导视线。'
                       +'压力只由已记录参考力和明确接触面积换算。质量不足的观测保留复核标记，不参与候选拟合；未激活标定。')
            item['kind']='errorbar'
            item['publication']={'kind':'pressure_calibration_response','channel':channel,
                                 'xlabel':english,'ylabel':'Voltage (V)','title':channel+' force calibration response'}
            report['charts'].append(item)
    report['tables']['pressure_calibration_points']=table(list(rows[0]) if rows else
            ['key','point_id','channel','reference_mean_n','reference_mean_kpa','mean_v','sample_sd_v'],rows)
    fit_columns=list(dict.fromkeys(key for row in fits for key in row))
    report['tables']['pressure_candidate_fits']=table(fit_columns or ['key','group','status'],fits)
    report['tables']['excluded_calibration_points']=table(
        ['run_key','run_index','point_id','reference_mean_n','channels','reason','deleted_utc','correction_sha256'],
        report['excluded_calibration_points'])
    report['warnings'].append('候选线性模型只使用同一记录、明确力敏通道、方向、循环、接触面积和参考仪器的一组有效区间。RMSE 与 R² 是拟合样本内残差统计，不是独立测力精度；禁止范围外外推，模型未自动激活。')
    report['warnings'].append('压力（kPa）要求法向接触力与可靠接触面积；未记录面积时只报告力（N）。不同记录、加载与卸载不合并拟合；不能由曲线叠加宣称消除迟滞或温漂。')
