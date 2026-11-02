"""Fresh saved-run diagnostics with complete file dependencies for publication."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from sspp_experiments.comparison import history_category
from sspp_lab.controller import LabError
from sspp_lab.diagnostics import build_diagnostics


DIAGNOSTIC_INPUTS = ('manifest.json', 'desktop_context.json', 'web_context.json',
                     'measurements.csv', 'baseline.json', 'amplitude_phase.jsonl', 'rx_frames.jsonl')


def _safe_file(run, name):
    relative = Path(name)
    if relative.is_absolute() or '..' in relative.parts:
        raise LabError('诊断来源路径无效')
    path = run / relative
    if (path.is_symlink() or any(parent.is_symlink() for parent in path.parents if parent.is_relative_to(run))
            or not path.resolve().is_relative_to(run.resolve())):
        raise LabError('诊断来源不允许符号链接或路径越界')
    if path.exists() and not path.is_file():
        raise LabError('诊断来源不是普通文件')
    return path


def diagnostic_dependency_snapshot(run, mode):
    if mode not in ('sweep', 'phase'):
        raise LabError('诊断采集模式无效')
    names = [name for name in DIAGNOSTIC_INPUTS if _safe_file(run, name).is_file()]
    if mode == 'phase':
        if _safe_file(run, 'rx_iq.cf32').is_file():
            names.append('rx_iq.cf32')
    else:
        files = list(run.glob('iq_point_[0-9]*.npz'))
        for path in files:
            _safe_file(run, path.name)
        try:
            files = sorted((path for path in files if path.is_file()), key=lambda path:int(path.stem.rsplit('_', 1)[-1]))
        except ValueError as exc:
            raise LabError('扫频 IQ 文件编号无效') from exc
        if files:
            names.append(files[-1].name)
    return {'mode':mode, 'files':sorted(names)}


def stream_file_binding(run, name):
    path = _safe_file(run, name)
    if not path.is_file():
        raise LabError('诊断来源文件已消失，请重新分析')
    before = path.stat()
    with path.open('rb') as content:
        digest = hashlib.file_digest(content, 'sha256').hexdigest()
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
        raise LabError('诊断来源正在改变，请等待记录封存')
    return {'relative_path':name, 'sha256':digest, 'bytes':after.st_size}


def _json(run, name, required=False):
    path = _safe_file(run, name)
    if not path.is_file():
        if required:
            raise LabError('诊断来源缺少 ' + name)
        return {}
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, ValueError) as exc:
        raise LabError('诊断来源 JSON 无效：' + name) from exc
    if not isinstance(value, dict):
        raise LabError('诊断来源 JSON 结构无效：' + name)
    return value


def _sealed(manifest, context, kind):
    telemetry = manifest.get('telemetry') or {}
    if (manifest.get('status') not in ('complete', 'stopped') or manifest.get('errors') != []
            or telemetry.get('tx_end_of_burst_sent') is not True or telemetry.get('workers_stopped') is not True
            or not (telemetry.get('rx_stop_sent') is True or telemetry.get('rx_stop_command_sent') is True)
            or context.get('desktop_status', context.get('web_status')) in ('failed', 'stop_unconfirmed', 'initialization_failed')):
        raise LabError('请先完成实验并确认清洁停机；错误或缺少收发停止证据的记录不能导出论文图')
    if kind == 'temperature_sweep':
        settings = (context.get('parameters') or {}).get('temperature_sweep') or {}
        thermal = manifest.get('temperature_sweep') or {}
        if settings.get('mode') != 'manual' and (thermal.get('status') != 'complete' or
                not all(thermal.get(name) is True for name in ('serial_workers_stopped', 'serial_closed', 'data_files_closed'))):
            raise LabError('温度联测缺少串口完整封存与三项停止证据，不能导出论文图')


class PublicationAnalysisMixin:
    def publication_analysis(self, key):
        """Build a source-bound report without using the live diagnostic cache."""
        if isinstance(key, str) and key.startswith('temperature_pressure_runs/'):
            return self.compare_runs([key])
        run = self.run_path(key)
        if key.startswith('teacher_runs/'):
            raise LabError('老师历史记录未保存可重建的完整诊断数据；可查看已有图，不能生成新的论文分析图')
        if not key.startswith(('runs/', 'phase_runs/')):
            raise LabError('此记录类型没有可用的论文图诊断入口')
        with self._comparison_operation():
            mode = 'phase' if key.startswith('phase_runs/') else 'sweep'
            dependency = diagnostic_dependency_snapshot(run, mode)
            bindings = [stream_file_binding(run, name) for name in dependency['files']]
            manifest = _json(run, 'manifest.json', required=True)
            context = _json(run, 'desktop_context.json') or _json(run, 'web_context.json')
            kind = history_category({'key':key}, context)
            _sealed(manifest, context, kind)
            try:
                diagnostic = build_diagnostics(run, mode)
            except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
                raise LabError('已保存诊断数据无法完整重建：' + self.public_log(str(exc))) from exc
            if diagnostic_dependency_snapshot(run, mode) != dependency:
                raise LabError('诊断依赖文件在分析中发生变化，请重新分析')
            if [stream_file_binding(run, name) for name in dependency['files']] != bindings:
                raise LabError('诊断来源在分析中发生变化，请重新分析')
            charts = []
            groups = {}
            for group, items in diagnostic.get('groups', {}).items():
                groups[group] = []
                for original in items:
                    chart = deepcopy(original)
                    chart.setdefault('source_key', key)
                    if any(existing['id'] == chart.get('id') for existing in charts):
                        raise LabError('诊断图编号重复，无法绑定单图导出')
                    for series in chart.get('series', []):
                        series.setdefault('key', key)
                    charts.append(chart); groups[group].append(chart['id'])
            if not charts:
                raise LabError('此记录没有可重建的实测诊断图；原始数据缺失时不生成替代图')
            label = str(manifest.get('label') or manifest.get('initial_label') or run.name)
            source = {'key':key, 'label':label, 'started_utc':str(manifest.get('started_utc', '')),
                      'parameters':{'mode':mode}, 'files':bindings, 'diagnostic_dependencies':dependency}
            facts = deepcopy(diagnostic.get('facts', []))
            report = {'schema_version':1, 'status':'ready', 'kind':kind, 'sources':[source],
                      'charts':charts, 'diagnostic_groups':groups,
                      'metrics':[{'key':key, 'label':label, 'chart_count':len(charts)}],
                      'tables':{'diagnostic_facts':{'columns':['name', 'value'], 'rows':facts}},
                      'warnings':['本图由本记录的已保存数据重新计算，未使用实时诊断缓存。数字幅度不是校准 S21/dBm，连续窗口不等同于独立重复实验。'],
                      'compatibility':[], 'quantitative_difference_allowed':False}
            return self.register_analysis_report(report, code_files=(
                'sspp_desktop/publication_analysis.py', 'sspp_desktop/services.py',
                'sspp_lab/diagnostics.py', 'sspp_experiments/comparison.py'))
