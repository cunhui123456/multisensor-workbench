"""液体温度实验：明确记录条件，后台分析保存结果，不自动操作硬件。"""
from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path
import uuid

from PySide6 import QtCore, QtGui, QtWidgets

from .plots import DiagnosticPlot
from .qt_cleanup import close_plot_widgets
from .workers import TaskRunner


class LiquidTemperatureWidget(QtWidgets.QWidget):
    prepare_requested = QtCore.Signal(dict)
    error = QtCore.Signal(str)
    busy_changed = QtCore.Signal(bool)

    def __init__(self, service, parent=None):
        super().__init__(parent)
        self.service = service
        self.runner = TaskRunner(self)
        self.runner.idle.connect(self._idle)
        self.current_key = None
        self.current_mode = None
        self.annotations = []
        self.report_path = None
        self._publication_report = None
        self._figure_directory = None
        self._external_busy = False
        self._closing = False
        self._dirty = False
        self._build_ui()
        self._update_enabled()

    @property
    def is_busy(self):
        return bool(self.runner.pending)

    def _build_ui(self):
        layout = QtWidgets.QVBoxLayout(self)
        explanation = QtWidgets.QLabel(
            "固定位置扫频：观察温度与幅度曲线变化。固定载频：分别估计温度平台内的相位—位移斜率，"
            "以及同次连续记录中固定距离的相位漂移。温度、距离均需实际测量；不同记录的绝对相位不能相减。")
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        self.status = QtWidgets.QLabel("准备条件后回到采集区检查参数和接线；本页不会自动开始收发。")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.tabs = QtWidgets.QTabWidget()
        layout.addWidget(self.tabs, 1)
        self._build_setup()
        self._build_records()
        self._build_report()

    @staticmethod
    def _number(text, name, low=None, high=None):
        try:
            value = float(text)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"请填写{name}的实际数值") from exc
        if not math.isfinite(value) or (low is not None and value < low) or (high is not None and value > high):
            raise ValueError(f"{name}超出有效范围")
        return value

    def _build_setup(self):
        self.settings_widget = QtWidgets.QWidget()
        self.settings_widget.setObjectName("liquidTemperatureSettings")
        page = self.settings_widget
        layout = QtWidgets.QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        hint = QtWidgets.QLabel(
            "每次扫频对应一个稳定温度和固定位置；开始前记录温度，结束后补录温度。"
            "相位实验建议每个稳定温度单独连续采集：先记录参考位置，再记录至少两个不同位置，最后回位。各次记录比较斜率和各自参考下的响应。"
            "温度平台、往返周期分别使用不同的平台编号。")
        hint.setWordWrap(True)
        hint.setToolTip(hint.text())
        hint.setText("填写本次实测条件，再应用到采集参数。扫频结束补录温度；相位实验每个稳定温度分别记录参考与位移位置。")
        layout.addWidget(hint)
        self.setup_group = QtWidgets.QGroupBox("本次样品与条件")
        form = QtWidgets.QFormLayout(self.setup_group)
        form.setRowWrapPolicy(QtWidgets.QFormLayout.RowWrapPolicy.WrapAllRows)
        form.setFieldGrowthPolicy(QtWidgets.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.mode = QtWidgets.QComboBox()
        self.mode.setMinimumContentsLength(16)
        self.mode.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.mode.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)
        self.mode.addItem("固定位置 · 温度扫频", "sweep")
        self.mode.addItem("固定载频 · 温度与位移相位", "phase")
        form.addRow("实验模式", self.mode)
        self.fields = {}
        for key, title, placeholder in (
            ("liquid", "液体名称", "例如水；记录成分或浓度"),
            ("sample_id", "样品编号", "同一份液体样品的编号"),
            ("container", "容器", "容器编号、材质及尺寸"),
            ("volume_ml", "液体体积 (mL)", "实际记录值"),
            ("geometry", "几何与接触条件", "测距基准、姿态、夹持/接触情况、线缆固定"),
            ("thermometer", "参考温度计", "仪器、探头位置及读数方法"),
            ("target_temperature_c", "目标温度 (℃)", "计划温度，不作为实际温度"),
            ("temperature_c", "开始参考温度 (℃)", "实际参考温度计读数"),
            ("distance_mm", "开始参考距离 (mm)", "传感器至容器的实际测距"),
            ("repeat_id", "独立重复编号", "每次重新放置/独立测量的编号"),
        ):
            field = QtWidgets.QLineEdit()
            field.setPlaceholderText(placeholder)
            field.setToolTip(placeholder)
            field.setMinimumWidth(0)
            self.fields[key] = field
            form.addRow(title, field)
        layout.addWidget(self.setup_group)
        self.prepare_button = QtWidgets.QPushButton("应用条件到采集参数")
        self.prepare_button.clicked.connect(self.prepare)
        layout.addWidget(self.prepare_button)
        layout.addStretch()
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(page)
        self._settings_scroll = scroll
        self.tabs.addTab(scroll, "实验条件")

    def take_settings_widget(self):
        """把条件表单交给全局侧栏；独立使用时仍保留完整设置页。"""
        if self._settings_scroll is not None:
            scroll = self._settings_scroll
            self._settings_scroll = None
            scroll.takeWidget()
            index = self.tabs.indexOf(scroll)
            if index >= 0:
                self.tabs.removeTab(index)
            scroll.deleteLater()
            self.settings_widget.setParent(None)
        return self.settings_widget

    def _build_records(self):
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)
        top = QtWidgets.QHBoxLayout()
        self.refresh_button = QtWidgets.QPushButton("刷新温度实验记录")
        self.refresh_button.clicked.connect(self.refresh_history)
        self.load_button = QtWidgets.QPushButton("载入选中记录")
        self.load_button.clicked.connect(self.load_selected)
        self.compare_button = QtWidgets.QPushButton("比较勾选的同类记录")
        self.compare_button.clicked.connect(self.compare_records)
        for button in (self.refresh_button, self.load_button, self.compare_button):
            top.addWidget(button)
        layout.addLayout(top)
        self.history = QtWidgets.QListWidget()
        self.history.setMaximumHeight(145)
        self.history.itemDoubleClicked.connect(lambda *_: self.load_selected())
        layout.addWidget(self.history)
        self.loaded_label = QtWidgets.QLabel("未载入记录。勾选 2–8 条同类记录比较；扫频以第一条为参考，跨相位记录只比较各自平台斜率和相对响应。")
        self.loaded_label.setWordWrap(True)
        layout.addWidget(self.loaded_label)
        action_row = QtWidgets.QHBoxLayout()
        action_row.addWidget(QtWidgets.QLabel("结束参考温度 (℃)"))
        self.end_temperature = QtWidgets.QLineEdit()
        self.end_temperature.setPlaceholderText("扫频结束时实际读数")
        self.end_temperature.setMaximumWidth(160)
        self.end_temperature.textEdited.connect(self._mark_dirty)
        action_row.addWidget(self.end_temperature)
        self.save_button = QtWidgets.QPushButton("保存标注 / 结束温度")
        self.save_button.clicked.connect(self.save_annotations)
        action_row.addWidget(self.save_button)
        action_row.addWidget(QtWidgets.QLabel("温度容差 (℃)"))
        self.tolerance = QtWidgets.QDoubleSpinBox()
        self.tolerance.setDecimals(2)
        self.tolerance.setRange(0.01, 20)
        self.tolerance.setValue(0.5)
        self.tolerance.setToolTip("分析采用的温差门槛；首尾温度在门槛内不证明整个区间恒温。")
        action_row.addWidget(self.tolerance)
        layout.addLayout(action_row)
        self.phase_group = QtWidgets.QGroupBox("相位稳定区间 · 只填写当时实际记录")
        phase_layout = QtWidgets.QVBoxLayout(self.phase_group)
        grid = QtWidgets.QGridLayout()
        self.window_fields = {}
        fields = (("start_s", "开始 (s)"), ("end_s", "结束 (s)"), ("temperature_c", "开始温度 (℃)"),
                  ("temperature_end_c", "结束温度 (℃)"), ("distance_mm", "参考距离 (mm)"), ("block", "温度平台/周期编号"))
        for index, (key, title) in enumerate(fields):
            field = QtWidgets.QLineEdit()
            self.window_fields[key] = field
            row, col = divmod(index, 3)
            grid.addWidget(QtWidgets.QLabel(title), row*2, col)
            grid.addWidget(field, row*2+1, col)
        phase_layout.addLayout(grid)
        row = QtWidgets.QHBoxLayout()
        self.role = QtWidgets.QComboBox()
        self.role.addItem("参考位置（含回位）", "reference")
        self.role.addItem("位移位置", "displacement")
        row.addWidget(self.role)
        self.recorded = QtWidgets.QCheckBox("温度与距离来自当时实际记录")
        row.addWidget(self.recorded)
        self.add_button = QtWidgets.QPushButton("添加区间")
        self.add_button.clicked.connect(self.add_window)
        row.addWidget(self.add_button)
        self.remove_button = QtWidgets.QPushButton("删除选中标注")
        self.remove_button.clicked.connect(self.remove_window)
        row.addWidget(self.remove_button)
        phase_layout.addLayout(row)
        self.window_table = QtWidgets.QTableWidget(0, 7)
        self.window_table.setHorizontalHeaderLabels(["平台", "用途", "开始 s", "结束 s", "开始 ℃", "结束 ℃", "距离 mm"])
        self.window_table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.window_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.window_table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.window_table.setMinimumHeight(130)
        phase_layout.addWidget(self.window_table)
        self.analyze_button = QtWidgets.QPushButton("分析同次记录的温度与位移相位")
        self.analyze_button.clicked.connect(self.analyze_phase)
        phase_layout.addWidget(self.analyze_button)
        layout.addWidget(self.phase_group)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(page)
        self.records_page = scroll
        self.tabs.addTab(scroll, "记录与稳定区间")

    def _build_report(self):
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)
        self.report_summary = QtWidgets.QLabel("尚未分析。拟合斜率描述信号响应，不等于位移测量精度或温度补偿已经成功。")
        self.report_summary.setWordWrap(True)
        layout.addWidget(self.report_summary)
        top = QtWidgets.QHBoxLayout()
        self.chart_choice = QtWidgets.QComboBox()
        self.chart_choice.currentIndexChanged.connect(self._chart_changed)
        top.addWidget(self.chart_choice, 1)
        self.export_button = QtWidgets.QPushButton("打包导出报告…")
        self.export_button.clicked.connect(self.export_report)
        top.addWidget(self.export_button)
        layout.addLayout(top)
        figure_actions = QtWidgets.QHBoxLayout()
        self.figure_export_button = QtWidgets.QPushButton("导出分析图")
        self.figure_export_button.setToolTip("保存当前图的 PDF、SVG、600 dpi PNG、绘图数据和图注到项目 Figures/liquid_temperature")
        self.figure_export_button.clicked.connect(self.export_figure)
        self.figure_save_as_button = QtWidgets.QPushButton("另存为…")
        self.figure_save_as_button.clicked.connect(self.save_figure_as)
        self.figure_folder_button = QtWidgets.QPushButton("打开图片文件夹")
        self.figure_folder_button.clicked.connect(self.open_figure_directory)
        for button in (self.figure_export_button, self.figure_save_as_button, self.figure_folder_button):
            figure_actions.addWidget(button)
        figure_actions.addStretch(1)
        layout.addLayout(figure_actions)
        results = QtWidgets.QTabWidget()
        self.plot = DiagnosticPlot()
        results.addTab(self.plot, "图形")
        self.report_text = QtWidgets.QPlainTextEdit()
        self.report_text.setReadOnly(True)
        results.addTab(self.report_text, "结果与质量明细")
        layout.addWidget(results, 1)
        self.report_page = page
        self.tabs.addTab(page, "分析报告")

    def _fail(self, error):
        self.status.setText(str(error))
        self.error.emit(str(error))

    def _submit(self, name, operation, callback):
        if self.is_busy or self._closing or self._external_busy:
            self._fail("请等待当前采集或后台任务完成。")
            return False
        submitted = self.runner.submit(name, operation, callback, self._fail)
        if submitted:
            self.status.setText("正在处理，请稍候…")
            self.busy_changed.emit(True)
            self._update_enabled()
        return submitted

    def _idle(self):
        self.busy_changed.emit(False)
        self._update_enabled()

    def set_external_busy(self, busy):
        self._external_busy = bool(busy)
        self._update_enabled()

    def _update_enabled(self):
        idle = not self.is_busy and not self._external_busy and not self._closing
        for widget in (self.setup_group, self.prepare_button, self.refresh_button, self.load_button,
                       self.compare_button, self.history, self.tolerance):
            widget.setEnabled(idle)
        self.save_button.setEnabled(idle and bool(self.current_key))
        self.end_temperature.setEnabled(idle and self.current_mode == "sweep")
        self.phase_group.setEnabled(idle and self.current_mode == "phase")
        self.export_button.setEnabled(idle and bool(self.report_path))
        self.chart_choice.setEnabled(idle)
        chart = self.chart_choice.currentData() or {}
        figure_ready = bool(self._publication_report and chart.get("id"))
        self.figure_export_button.setEnabled(idle and figure_ready)
        self.figure_save_as_button.setEnabled(idle and figure_ready)
        self.figure_folder_button.setEnabled(idle and bool(self._figure_directory))

    def prepare(self):
        if self.is_busy or self._external_busy or self._closing:
            return
        try:
            metadata = {key: field.text().strip() for key, field in self.fields.items()}
            for key in ("liquid", "sample_id", "container", "geometry", "thermometer", "repeat_id"):
                if not metadata[key]:
                    raise ValueError("请完整填写液体、样品、容器、几何条件、温度计和独立重复编号。")
            for key, title, low, high in (
                ("volume_ml", "体积", 0.001, 1e6),
                ("target_temperature_c", "目标温度", -100, 200),
                ("temperature_c", "开始参考温度", -100, 200),
                ("distance_mm", "开始参考距离", 0, 1e6),
            ):
                metadata[key] = self._number(metadata[key], title, low, high)
            self.prepare_requested.emit({"mode": self.mode.currentData(), "liquid_temperature": metadata})
            self.status.setText("实验条件已应用。请回采集区检查参数、确认本次接线和现场条件后开始。")
        except ValueError as exc:
            self._fail(exc)

    def refresh_history(self):
        self._submit("liquid_history", self.service.liquid_history, self._show_history)

    def _show_history(self, records):
        checked = {self.history.item(i).data(QtCore.Qt.ItemDataRole.UserRole)["key"]
                   for i in range(self.history.count())
                   if self.history.item(i).checkState() == QtCore.Qt.CheckState.Checked}
        self.history.clear()
        for record in records:
            item = QtWidgets.QListWidgetItem(
                f"{'扫频' if record['mode'] == 'sweep' else '相位'} · {record.get('label') or record['key']}"
                f" · {record.get('temperature_c', '未记录')} ℃ · {record['key']}")
            item.setData(QtCore.Qt.ItemDataRole.UserRole, dict(record))
            item.setFlags(item.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(QtCore.Qt.CheckState.Checked if record["key"] in checked else QtCore.Qt.CheckState.Unchecked)
            self.history.addItem(item)
        self.status.setText(f"已找到 {len(records)} 条液体温度实验记录。")

    def load_selected(self):
        item = self.history.currentItem()
        if item is None:
            self._fail("请先选中一条记录。")
            return
        self.set_current(item.data(QtCore.Qt.ItemDataRole.UserRole)["key"])

    def set_current(self, key):
        if self._dirty and key != self.current_key:
            self._fail("当前区间标注尚未保存，请先保存再切换记录。")
            return
        if self._dirty:
            self._fail("当前区间标注尚未保存，请先保存后重新载入。")
            return
        self._submit("liquid_load", lambda: self.service.liquid_load(key), self._show_record)

    def _show_record(self, result):
        self.current_key, self.current_mode = result["key"], result["mode"]
        self.annotations = deepcopy(result.get("annotations", []))
        self._dirty = False
        self.report_path = None
        self._publication_report = None
        self._figure_directory = None
        self.loaded_label.setText("当前记录：" + self.current_key + "；相位区间单位为本次记录的硬件时间秒。")
        end = result.get("temperature_end_c", result.get("metadata", {}).get("temperature_end_c"))
        self.end_temperature.setText("" if end is None else str(end))
        self._render_windows()
        if result.get("analysis"):
            self._show_report(result["analysis"], show_tab=False)
        else:
            self.report_text.clear()
            self.chart_choice.clear()
            self.report_summary.setText("当前记录尚无分析。请补全实际读数和稳定区间。")
        self.tabs.setCurrentWidget(self.records_page)
        self.status.setText("已载入 " + self.current_key)
        self._update_enabled()

    def _render_windows(self):
        self.window_table.setRowCount(len(self.annotations))
        for index, row in enumerate(self.annotations):
            for col, key in enumerate(("block", "role", "start_s", "end_s", "temperature_c", "temperature_end_c", "distance_mm")):
                value = row.get(key, "")
                if key == "role":
                    value = {"reference": "参考/回位", "displacement": "位移"}.get(value, value)
                self.window_table.setItem(index, col, QtWidgets.QTableWidgetItem(str(value)))

    def add_window(self):
        if self.current_mode != "phase" or self.is_busy or self._external_busy:
            return
        try:
            if not self.recorded.isChecked():
                raise ValueError("请确认温度与距离来自当时实际记录，不能根据曲线猜测标签。")
            row = {key: field.text().strip() for key, field in self.window_fields.items()}
            for key in ("start_s", "end_s", "distance_mm"):
                row[key] = self._number(row[key], key, 0, 1e6)
            for key in ("temperature_c", "temperature_end_c"):
                row[key] = self._number(row[key], "参考温度", -100, 200)
            if not row["block"]:
                raise ValueError("请填写本次温度平台/往返周期编号。")
            if row["end_s"] <= row["start_s"]:
                raise ValueError("区间结束时间必须大于开始时间。")
            if any(row["start_s"] < old["end_s"] and old["start_s"] < row["end_s"] for old in self.annotations):
                raise ValueError("稳定区间不能重叠。")
            row.update(id=uuid.uuid4().hex[:12], role=self.role.currentData())
            self.annotations.append(row)
            self._dirty = True
            self.recorded.setChecked(False)
            self._render_windows()
            self.status.setText("区间已添加，尚未保存。保存后分析；质量判定由保存序列完成。")
        except ValueError as exc:
            self._fail(exc)

    def remove_window(self):
        if self.is_busy or self._external_busy:
            return
        selected = sorted({item.row() for item in self.window_table.selectedItems()}, reverse=True)
        for index in selected:
            self.annotations.pop(index)
        if selected:
            self._dirty = True
        self._render_windows()

    def save_annotations(self):
        if not self.current_key:
            return
        try:
            end = (self._number(self.end_temperature.text(), "结束参考温度", -100, 200)
                   if self.current_mode == "sweep" else None)
        except ValueError as exc:
            self._fail(exc)
            return
        key, annotations = self.current_key, deepcopy(self.annotations)
        self._submit("liquid_save", lambda: self.service.liquid_save_annotations(
            key, annotations, temperature_end_c=end), self._saved)

    def _saved(self, result):
        self._dirty = False
        # The visible result may have used the previous interval/temperature
        # annotations. Keep its preview, but require a new analysis for export.
        self.report_path = None
        self._publication_report = None
        self._figure_directory = None
        self.status.setText("标注已保存；请重新分析后导出。原始采集数据保持不变。")
        if self.chart_choice.count():
            self.report_summary.setText("标注已更新；当前图为上次分析预览，请重新分析后导出。")
        self._update_enabled()

    def analyze_phase(self):
        if not self.current_key or self.current_mode != "phase":
            return
        key, annotations, tolerance = self.current_key, deepcopy(self.annotations), self.tolerance.value()
        self._submit("liquid_phase", lambda: self.service.liquid_analyze_phase(
            key, annotations, temperature_tolerance_c=tolerance), self._show_report)

    def compare_records(self):
        records = [self.history.item(i).data(QtCore.Qt.ItemDataRole.UserRole)
                   for i in range(self.history.count())
                   if self.history.item(i).checkState() == QtCore.Qt.CheckState.Checked]
        if not 2 <= len(records) <= 8:
            self._fail("请勾选 2–8 条已完成的同类液体温度实验记录。")
            return
        if len({record["mode"] for record in records}) != 1:
            self._fail("扫频与相位记录不能混选比较。")
            return
        keys = [record["key"] for record in records]
        if self._dirty and self.current_key in keys:
            self._fail("当前记录存在尚未保存的读数或区间，请先保存后比较。")
            return
        tolerance = self.tolerance.value()
        if records[0]["mode"] == "sweep":
            self._submit("liquid_sweeps", lambda: self.service.liquid_compare_sweeps(
                keys, reference_key=keys[0], temperature_tolerance_c=tolerance), self._show_report)
        else:
            self._submit("liquid_phase_runs", lambda: self.service.liquid_compare_phase_runs(
                keys, temperature_tolerance_c=tolerance), self._show_report)

    def _mark_dirty(self):
        if self.current_key:
            self._dirty = True

    def _show_report(self, report, show_tab=True):
        self.report_path = report.get("export_path")
        self._publication_report = deepcopy(report) if report.get("comparison_id") else None
        self._figure_directory = None
        status = {"ready": "分析完成", "partial": "部分区间有效", "rejected": "未通过质量或设计检查"}.get(report.get("status"), "报告已生成")
        errors, warnings = report.get("errors", []), report.get("warnings", [])
        self.report_summary.setText("\n".join([status, *map(str, errors), *map(str, warnings)]))
        self.report_text.setPlainText(json.dumps(report, ensure_ascii=False, indent=2))
        charts = list(report.get("charts", []))
        if not charts and report.get("blocks"):
            series = []
            blocks = report["blocks"]
            if isinstance(blocks, dict):
                blocks = list(blocks.values())
            for block in blocks:
                points = block.get("points", [])
                data = [[p.get("displacement_mm"), p.get("delta_phase_deg")] for p in points]
                data = [pair for pair in data if all(isinstance(value, (int, float)) and math.isfinite(value) for value in pair)]
                if data:
                    series.append({"label": str(block.get("block", "温度平台")), "data": data})
            charts.append({"title": "各温度平台内相位—位移响应", "xlabel": "相对参考位置的位移 (mm)",
                           "ylabel": "相对参考位置的相位 (°)", "kind": "scatter", "series": series,
                           "note": "每个平台以自身参考位置为零点；此图不提供毫米精度验证。"})
        self.chart_choice.clear()
        for chart in charts:
            self.chart_choice.addItem(chart.get("title", "结果图"), chart)
        self._chart_changed()
        if show_tab:
            self.tabs.setCurrentWidget(self.report_page)
        self.status.setText(status + (" · " + str(self.report_path) if self.report_path else ""))
        self._update_enabled()

    def _chart_changed(self):
        self.plot.display(self.chart_choice.currentData() or {})
        self._update_enabled()

    def _figure_selection(self):
        if self.is_busy or self._external_busy or self._closing or not self._publication_report:
            return None
        chart_id = (self.chart_choice.currentData() or {}).get("id")
        if not chart_id or not any(chart.get("id") == chart_id for chart in self._publication_report.get("charts", [])):
            return None
        return deepcopy(self._publication_report), chart_id

    def _figure_saved(self, report_id, result):
        if self._closing or not self._publication_report or self._publication_report.get("comparison_id") != report_id:
            return
        self._figure_directory = result.get("directory") or str(Path(result["path"]).parent)
        self.status.setText("分析图已保存：" + str(result["path"]))

    def export_figure(self):
        selection = self._figure_selection()
        if selection is None:
            return
        report, chart_id = selection
        self._submit("liquid_figure", lambda: self.service.export_publication_figure(report, chart_id),
                     lambda result: self._figure_saved(report["comparison_id"], result))

    def save_figure_as(self):
        selection = self._figure_selection()
        if selection is None:
            return
        report, chart_id = selection
        destination, chosen_filter = QtWidgets.QFileDialog.getSaveFileName(
            self, "另存为分析图", chart_id + ".pdf", "矢量 PDF (*.pdf);;矢量 SVG (*.svg);;高清 PNG (*.png)")
        if not destination:
            return
        current = self._figure_selection()
        if current is None or current[0]["comparison_id"] != report["comparison_id"] or current[1] != chart_id:
            return
        suffix = Path(destination).suffix.lower()
        if not suffix:
            destination += ".svg" if "*.svg" in chosen_filter else ".png" if "*.png" in chosen_filter else ".pdf"
        elif suffix not in (".pdf", ".svg", ".png"):
            self._fail("分析图仅支持 PDF、SVG 或 PNG 格式。")
            return
        self._submit("liquid_figure", lambda: self.service.export_analysis_figure(report, chart_id, destination),
                     lambda result: self._figure_saved(report["comparison_id"], result))

    def open_figure_directory(self):
        if self._figure_directory and not self.is_busy and not self._external_busy and not self._closing:
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(self._figure_directory))

    def export_report(self):
        if not self.report_path:
            return
        destination, _ = QtWidgets.QFileDialog.getSaveFileName(self, "保存液体温度实验报告", "liquid_temperature_report.zip", "ZIP (*.zip)")
        if not destination:
            return
        if not destination.lower().endswith(".zip"):
            destination += ".zip"
        source = str(self.report_path)
        self._submit("liquid_export", lambda: self.service.liquid_export(source, destination),
                     lambda result: self.status.setText("已导出：" + str(destination)))

    def request_close(self):
        self._closing = True
        self.runner.closing = True
        self._update_enabled()
        return not self.is_busy

    def closeEvent(self, event):
        if not self.request_close():
            event.ignore()
            return
        close_plot_widgets(self)
        super().closeEvent(event)
