"""Shared, source-verified publication exports; acquisition Data is read-only."""
from copy import deepcopy
import csv
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import uuid

from sspp_lab.controller import LabError


EXPERIMENT_FOLDERS = {
    'pressure_calibration':'pressure_calibration',
    'material_sweep':'material_sweep', 'teacher_sweep':'material_sweep',
    'finger_displacement':'finger_displacement', 'phase_history':'finger_displacement',
    'liquid_temperature':'liquid_temperature', 'liquid_temperature_sweep':'liquid_temperature',
    'liquid_temperature_phase':'liquid_temperature', 'temperature_pressure':'temperature_pressure',
    'temperature_calibration':'temperature_calibration', 'temperature_sweep':'temperature_sweep',
}
FIGURE_CODE_FILES = ('sspp_desktop/figure_archive.py', 'sspp_desktop/comparison_service.py',
                     'sspp_desktop/publication.py', 'sspp_desktop/comparison_export.py')


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + '\n').encode('utf-8')


def report_digest(report):
    return hashlib.sha256(json_bytes(report)).hexdigest()


def _slug(value):
    return re.sub(r'[^A-Za-z0-9_.-]', '_', str(value)).strip('._')[:100] or 'figure'


class FigureArchiveMixin:
    def register_analysis_report(self, report, code_files=()):
        """Register a finished saved-data report; callers supply adapter code paths.

        This may run inside an existing analysis operation. It never opens hardware
        or creates files, and preserves source bindings supplied by that analysis.
        """
        report = deepcopy(report)
        sources = report.get('sources')
        if (not isinstance(sources, list) or not 1 <= len(sources) <= 8
                or any(not isinstance(source, dict) or not isinstance(source.get('key'), str)
                       or not isinstance(source.get('files'), list) or not source['files'] for source in sources)
                or len({source['key'] for source in sources}) != len(sources)):
            raise LabError('分析报告需要 1–8 条独立来源及每条来源的文件哈希')
        self._comparison_source_files(report)
        report['keys'] = [source['key'] for source in sources]
        report['analysis_mode'] = 'single' if len(sources) == 1 else 'combined'
        if len(sources) == 1:
            report['reference_key'] = None
            report['quantitative_difference_allowed'] = False
            report['compatibility'] = []
        report.setdefault('warnings', [])
        report['created_utc'] = datetime.now(timezone.utc).isoformat()
        report['comparison_id'] = uuid.uuid4().hex
        report['analysis_code'] = []
        for name in dict.fromkeys((*FIGURE_CODE_FILES, *code_files)):
            relative = Path(name)
            if relative.is_absolute() or '..' in relative.parts:
                raise LabError('分析源码路径无效')
            path = self.root / relative
            if path.is_symlink() or not path.resolve().is_relative_to(self.root.resolve()):
                raise LabError('分析源码路径无效')
            if path.is_file():
                report['analysis_code'].append({'path':relative.as_posix(), 'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
        digest = report_digest(report)
        with self.lock:
            digests = getattr(self, '_comparison_digests', {})
            digests[report['comparison_id']] = digest
            while len(digests) > 16:
                digests.pop(next(iter(digests)))
            self._comparison_digests = digests
        return report

    def _verify_analysis_report(self, report, chart_id=None):
        expected = getattr(self, '_comparison_digests', {}).get(report.get('comparison_id'))
        if not expected or report_digest(report) != expected:
            raise LabError('对比报告已更改或已过期，请重新分析再导出')
        chart = None
        if chart_id is not None:
            matches = [item for item in report.get('charts', []) if item.get('id') == chart_id]
            if not isinstance(chart_id, str) or len(matches) != 1:
                raise LabError('所选分析图不存在，请重新选择图表')
            chart = matches[0]
        self._verify_comparison_code(report)
        files = self._comparison_source_files(report)
        return chart, files

    def _render_analysis_figure(self, report, chart_id, extension):
        """One renderer for default archives and explicit Save As destinations."""
        with tempfile.TemporaryDirectory(prefix='sspp-figure-render-') as scratch_name:
            scratch = Path(scratch_name)
            report_path = scratch / 'comparison.json'
            report_path.write_bytes(json_bytes(report))
            rendered = scratch / ('figure' + extension)
            script = Path(__file__).with_name('comparison_export.py')
            process = subprocess.run(
                [sys.executable, '-B', str(script), str(report_path), str(scratch),
                 '--chart-id', chart_id, '--output', str(rendered)],
                capture_output=True, text=True, timeout=90, env=self.child_env())
            if process.returncode:
                raise LabError('分析图生成失败，原目标文件未覆盖：' + self.public_log(process.stderr[-1200:]))
            if not rendered.is_file() or rendered.is_symlink() or rendered.stat().st_size == 0:
                raise LabError('分析图生成不完整，原目标文件未覆盖')
            return rendered.read_bytes()

    def _validate_figure_target(self, target):
        if (not target.parent.is_dir() or target.is_symlink()
                or any(parent.is_symlink() for parent in target.parents)
                or (target.exists() and not target.is_file())):
            raise LabError('单图保存位置不是可用的普通文件路径，不允许符号链接')
        if target.resolve().is_relative_to((self.root / 'Data').resolve()):
            raise LabError('请选择项目 Data 以外的位置保存分析图')

    def export_analysis_figure(self, report, chart_id, destination):
        report = deepcopy(report)
        if not isinstance(chart_id, str) or not chart_id:
            raise LabError('所选分析图不存在，请重新选择图表')
        target = Path(destination).expanduser()
        if not target.is_absolute() or target.suffix.lower() not in ('.pdf', '.svg', '.png'):
            raise LabError('请选择绝对路径的 PDF、SVG 或 PNG 图文件保存位置')
        self._validate_figure_target(target)
        temporary = target.parent / ('.' + target.name + '.' + uuid.uuid4().hex + '.tmp')
        with self._comparison_operation():
            try:
                self._verify_analysis_report(report, chart_id)
                data = self._render_analysis_figure(report, chart_id, target.suffix.lower())
                self._validate_figure_target(target)
                with temporary.open('xb') as content:
                    content.write(data)
                self._verify_analysis_report(report, chart_id)
                self._validate_figure_target(target)
                os.replace(temporary, target)
                return {'path':str(target), 'bytes':len(data), 'sha256':hashlib.sha256(data).hexdigest(),
                        'warnings':list(report.get('warnings', []))}
            finally:
                if temporary.exists():
                    temporary.unlink()

    def _publication_parent(self, report):
        kind = EXPERIMENT_FOLDERS.get(report.get('kind'))
        if kind is None:
            raise LabError('本实验类型尚未登记论文图归档目录')
        keys = [source['key'] for source in report['sources']]
        record = (_slug(Path(keys[0]).name) if len(keys) == 1 else
                  'combined_' + hashlib.sha256(json_bytes(sorted(keys))).hexdigest()[:16])
        root = self.root.resolve()
        parent = root / 'Figures' / kind / record
        self._validate_publication_parent(parent)
        return parent, kind

    def _validate_publication_parent(self, parent):
        root = self.root.resolve()
        if (not parent.resolve().is_relative_to(root / 'Figures')
                or any(p.is_symlink() for p in (parent, *parent.parents) if p.is_relative_to(root))
                or parent.resolve().is_relative_to((root / 'Data').resolve())):
            raise LabError('论文图归档路径无效，不允许符号链接或 Data 目录')
        if any(p.exists() and not p.is_dir() for p in (parent, *parent.parents) if p.is_relative_to(root)):
            raise LabError('论文图归档路径被普通文件占用')

    def export_publication_figure(self, report, chart_id):
        """Archive all three figure formats, data and provenance as a new version."""
        from .publication import analysis_figure_caption, analysis_figure_data, analysis_figure_style

        report = deepcopy(report)
        if not isinstance(chart_id, str) or not chart_id:
            raise LabError('所选分析图不存在，请重新选择图表')
        with self._comparison_operation():
            chart, _ = self._verify_analysis_report(report, chart_id)
            parent, experiment = self._publication_parent(report)
            parent.mkdir(parents=True, exist_ok=True)
            self._validate_publication_parent(parent)
            created = datetime.now(timezone.utc)
            version = created.strftime('%Y%m%dT%H%M%S_%fZ') + '_' + uuid.uuid4().hex[:8]
            destination = parent / version
            stem = _slug(chart_id)
            with tempfile.TemporaryDirectory(prefix='.' + version + '-', dir=parent) as temporary_name:
                temporary = Path(temporary_name)
                artifacts = []
                def put(name, data):
                    (temporary / name).write_bytes(data)
                    artifacts.append({'path':name, 'bytes':len(data), 'sha256':hashlib.sha256(data).hexdigest()})

                for extension in ('.pdf', '.svg', '.png'):
                    put(stem + extension, self._render_analysis_figure(report, chart_id, extension))
                rows = analysis_figure_data(report, chart_id)
                columns = list(dict.fromkeys(key for row in rows for key in row))
                if not columns:
                    columns = ['series_index', 'label', 'point_index', 'x', 'y', 'error', 'source_key', 'source_point_id']
                stream = io.StringIO(newline='')
                writer = csv.DictWriter(stream, fieldnames=columns)
                writer.writeheader(); writer.writerows(rows)
                put('data.csv', stream.getvalue().encode('utf-8-sig'))
                put('caption.txt', analysis_figure_caption(report, chart_id).encode('utf-8'))
                manifest = {
                    'schema':'sspp.figure_archive.v1', 'created_utc':created.isoformat(),
                    'experiment':experiment, 'report_kind':report.get('kind'),
                    'analysis_mode':report.get('analysis_mode'), 'comparison_id':report['comparison_id'],
                    'chart_id':chart_id, 'source_keys':[source['key'] for source in report['sources']],
                    'sources':report['sources'], 'analysis_code':report.get('analysis_code', []),
                    'plot':{key:chart[key] for key in ('id', 'title', 'kind', 'xlabel', 'ylabel', 'note', 'publication', 'source_key', 'source_type') if key in chart},
                    'style':analysis_figure_style(report, chart_id),
                    'point_sources':[{'series_index':index, 'source_key':series.get('key'),
                                      'points':series.get('point_sources', [])}
                                     for index, series in enumerate(chart.get('series', []))],
                    'statistical_semantics':chart.get('note', ''),
                    'excluded_calibration_points':report.get('excluded_calibration_points', []),
                    'warnings':report.get('warnings', []), 'png_dpi':600, 'files':artifacts,
                }
                (temporary / 'figure_manifest.json').write_bytes(json_bytes(manifest))
                self._verify_analysis_report(report, chart_id)
                self._validate_publication_parent(parent)
                if destination.exists():
                    raise LabError('论文图版本目录已存在，请重试；现有版本未覆盖')
                temporary.rename(destination)
            return {'directory':str(destination), 'path':str(destination),
                    'files':{extension[1:]:str(destination / (stem + extension)) for extension in ('.pdf', '.svg', '.png')},
                    'manifest':str(destination / 'figure_manifest.json'),
                    'bytes':sum(path.stat().st_size for path in destination.iterdir()),
                    'sha256':hashlib.sha256((destination / 'figure_manifest.json').read_bytes()).hexdigest(),
                    'warnings':list(report.get('warnings', []))}
