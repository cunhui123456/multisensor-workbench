"""PyQtGraph 实时曲线与完整离线诊断模型渲染。"""
from __future__ import annotations

import numpy as np
from PySide6 import QtCore, QtWidgets
import pyqtgraph as pg
from .qt_cleanup import close_plot_widgets

COLORS = ["#087f85", "#c17b26", "#7771bb", "#b5567a", "#408767", "#487da3"]
pg.setConfigOptions(antialias=False, background="#ffffff", foreground="#344655")


def numbers(values):
    """传输缺口用 NaN 显式断线，绝不画成连续实测。"""
    return np.array([np.nan if value is None else value for value in values], dtype=float)


def make_plot(title, xlabel, ylabel):
    plot = pg.PlotWidget(title=title)
    plot.setLabel("bottom", xlabel)
    plot.setLabel("left", ylabel)
    plot.showGrid(x=True, y=True, alpha=0.15)
    plot.setMinimumHeight(170)
    plot.getAxis("bottom").enableAutoSIPrefix(False)
    plot.getAxis("left").enableAutoSIPrefix(False)
    plot.setMenuEnabled(True)
    return plot


class LivePlots(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(420)
        self.mode = None
        self.sweep_items = {}
        self.amp = make_plot("实时幅度", "时间（s）", "相对幅度（dB）")
        self.phase = make_plot("实时相位", "时间（s）", "相对相位（°）")
        self.phase.setXLink(self.amp)
        self.amplitude_line = self.amp.plot(pen=pg.mkPen(COLORS[0], width=1.5), connect="finite")
        self.phase_line = self.phase.plot(pen=pg.mkPen(COLORS[1], width=1.5), connect="finite")
        self.legend = self.amp.addLegend(offset=(12, 12))
        self.follow = QtWidgets.QCheckBox("自动跟随最新数据")
        self.follow.setChecked(True)
        self.follow.toggled.connect(self._auto_range)
        self.full_range = QtWidgets.QPushButton("恢复全图")
        self.full_range.clicked.connect(self._reset_range)
        bar = QtWidgets.QHBoxLayout()
        bar.addWidget(self.follow)
        bar.addStretch()
        bar.addWidget(self.full_range)
        self.hint = QtWidgets.QLabel("等待实验或选择历史回放。拖动平移，滚轮缩放；右键可导出图像。")
        self.hint.setWordWrap(True)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(bar)
        layout.addWidget(self.amp, 1)
        layout.addWidget(self.phase, 1)
        layout.addWidget(self.hint)

    def _auto_range(self, checked):
        self.amp.enableAutoRange(x=checked, y=checked)
        self.phase.enableAutoRange(x=checked, y=checked)

    def closeEvent(self, event):
        close_plot_widgets(self)
        super().closeEvent(event)

    def _reset_range(self):
        self.follow.setChecked(True)
        self._auto_range(True)

    def clear(self):
        self.amplitude_line.setData([], [])
        self.phase_line.setData([], [])
        for item in self.sweep_items.values():
            self.amp.removeItem(item)
        self.sweep_items.clear()
        self.legend.clear()

    def update_snapshot(self, snapshot):
        mode = snapshot.get("mode")
        if mode != self.mode:
            self.clear()
            self.mode = mode
            self.phase.setVisible(mode == "phase")
            self.amp.setTitle("连续幅相 · 相对幅度" if mode == "phase" else "逐点扫频 · 各轮响应")
            self.amp.setLabel("bottom", "时间（s）" if mode == "phase" else "载频（GHz）")
            self.amp.setLabel("left", "相对幅度（dB）" if mode == "phase" else "幅度（dBFS 估计）")
            self._reset_range()
        points = snapshot.get("points") or []
        if mode == "phase":
            x = numbers([row.get("x") for row in points])
            self.amplitude_line.setData(x, numbers([row.get("amplitude") for row in points]), connect="finite")
            self.phase_line.setData(x, numbers([row.get("phase") for row in points]), connect="finite")
            self.hint.setText("相对本次静止参考；毫米估算需同次标定与独立验证。" +
                              (f" 显示传输缺口 {snapshot['display_gaps']} 次，断线保留。" if snapshot.get("display_gaps") else ""))
        elif mode == "sweep":
            groups = {}
            for row in points:
                groups.setdefault(row.get("repeat", 1), []).append(row)
            for repeat in set(self.sweep_items) - set(groups):
                self.legend.removeItem(f"第 {repeat} 轮")
                self.amp.removeItem(self.sweep_items.pop(repeat))
            for repeat, rows in groups.items():
                if repeat not in self.sweep_items:
                    self.sweep_items[repeat] = self.amp.plot(
                        pen=pg.mkPen(COLORS[(int(repeat)-1) % len(COLORS)], width=1.7),
                        symbol="o", symbolSize=4, name=f"第 {repeat} 轮", connect="finite")
                self.sweep_items[repeat].setData(
                    numbers([row.get("carrier_hz") for row in rows]) / 1e9,
                    numbers([row.get("amplitude_dB") for row in rows]), connect="finite")
            invalid=sum(r.get('measurement_status')=='invalid' or r.get('amplitude_dB') is None for r in points)
            self.hint.setText(f"有效测量 {len(points)-invalid} 点；无效 {invalid} 点保持空缺。数字幅度未经绝对功率或 S21 标定。")


class PreviewPlots(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(200)
        self.wave = make_plot("RX 实时时域", "窗口内时间（ms）", "数字幅值")
        self.spectrum = make_plot("RX 实时频谱", "载频偏移（kHz）", "dBFS 估计")
        self.iq = make_plot("RX 原始 IQ", "I", "Q")
        self.iq.setAspectLocked(True)
        self.wave.addLegend(offset=(8, 8))
        self.i_line = self.wave.plot(pen=COLORS[0], name="I", connect="finite")
        self.q_line = self.wave.plot(pen=COLORS[1], name="Q", connect="finite")
        self.spectrum_line = self.spectrum.plot(pen=COLORS[0], connect="finite")
        self.iq_line = self.iq.plot(pen=None, symbol="o", symbolSize=3, symbolBrush=COLORS[0], symbolPen=None)
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        for widget in (self.wave, self.spectrum, self.iq):
            layout.addWidget(widget, 1)

    def update_preview(self, preview):
        if not preview:
            for item in (self.i_line, self.q_line, self.spectrum_line, self.iq_line):
                item.setData([], [])
            return
        t, i, q = numbers(preview.get("time_ms", [])), numbers(preview.get("I", [])), numbers(preview.get("Q", []))
        self.i_line.setData(t, i, connect="finite")
        self.q_line.setData(t, q, connect="finite")
        self.iq_line.setData(i, q)
        self.spectrum_line.setData(numbers(preview.get("frequency_khz", [])), numbers(preview.get("spectrum_db", [])), connect="finite")

    def closeEvent(self, event):
        close_plot_widgets(self)
        super().closeEvent(event)


class DiagnosticPlot(QtWidgets.QWidget):
    """支持现有全部诊断类型：线、散点、误差棒、柱、热图和阶段。"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.plot = make_plot("选择诊断图", "", "")
        self.plot.setMinimumHeight(320)
        self.legend = self.plot.addLegend(offset=(10, 10))
        self.note = QtWidgets.QLabel()
        self.note.setWordWrap(True)
        self.readout = QtWidgets.QLabel("移动指针查看坐标；拖动平移，滚轮缩放，右键导出。")
        self.colorbar = None
        self.model = {}
        self.plot.scene().sigMouseMoved.connect(self._readout)
        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(self.plot, 1)
        layout.addWidget(self.note)
        layout.addWidget(self.readout)

    def closeEvent(self, event):
        close_plot_widgets(self)
        super().closeEvent(event)

    def _readout(self, position):
        if not self.plot.sceneBoundingRect().contains(position):
            return
        p = self.plot.plotItem.vb.mapSceneToView(position)
        self.readout.setText(f"{self.model.get('xlabel', 'x')}: {p.x():.6g}    {self.model.get('ylabel', 'y')}: {p.y():.6g}")

    def display(self, model):
        self.model = model or {}
        model = self.model
        self.plot.clear()
        self.legend.clear()
        if self.colorbar is not None:
            self.plot.plotItem.layout.removeItem(self.colorbar)
            self.colorbar.scene().removeItem(self.colorbar)
            self.colorbar = None
        self.plot.setTitle(model.get("title", "暂无诊断图"))
        self.plot.setLabel("bottom", model.get("xlabel", ""))
        self.plot.setLabel("left", model.get("ylabel", ""))
        self.plot.setAspectLocked(bool(model.get("equal_aspect")))
        self.plot.getAxis("bottom").setTicks(None)
        self.plot.getAxis("left").setTicks(None)
        self.note.setText(model.get("note", "选择历史记录后加载完整分析。"))
        kind = model.get("kind", "line")
        if kind == "heatmap":
            x, y = numbers(model.get("x", [])), numbers(model.get("y", []))
            z = np.asarray(model.get("z", []), dtype=float)
            if x.size and y.size and z.size:
                image = pg.ImageItem(axisOrder="row-major")
                image.setImage(z)
                dx = float(np.median(np.diff(x))) if len(x) > 1 else 1
                dy = float(np.median(np.diff(y))) if len(y) > 1 else 1
                image.setRect(QtCore.QRectF(float(x[0]-dx/2), float(y[0]-dy/2),
                                           float(x[-1]-x[0]+dx), float(y[-1]-y[0]+dy)))
                self.plot.addItem(image)
                finite = z[np.isfinite(z)]
                lo, hi = (float(finite.min()), float(finite.max())) if finite.size else (0, 1)
                if hi == lo:
                    hi = lo + 1
                self.colorbar = pg.ColorBarItem(values=(lo, hi), colorMap=pg.colormap.get("viridis"),
                                                label=model.get("colorbar", ""), interactive=False)
                self.colorbar.setImageItem(image, insert_in=self.plot.plotItem)
        elif kind == "segments":
            ticks = []
            for index, segment in enumerate(model.get("segments", [])):
                start, end = segment["start"], segment["end"]
                color = "#b7bec5" if segment.get("transition") else segment.get("color", COLORS[0])
                bar = pg.BarGraphItem(x=[(start+end)/2], y=[index+.5], width=max(end-start, .001), height=.7, brush=color, pen=None)
                self.plot.addItem(bar)
                label = segment.get("label", "")
                if segment.get("contact") is not None:
                    label += " · 接触" if segment["contact"] else " · 未接触"
                ticks.append((index+.5, label))
            self.plot.getAxis("left").setTicks([ticks])
        else:
            for index, series in enumerate(model.get("series", [])):
                data = np.asarray(series.get("data", []), dtype=float)
                if not data.size:
                    continue
                x, y = data[:, 0], data[:, 1]
                color = series.get("color", COLORS[index % len(COLORS)])
                label = series.get("label", "")
                if kind == "bar":
                    width = float(np.median(np.diff(x))) * .7 if len(x) > 1 else .6
                    self.plot.addItem(pg.BarGraphItem(x=x, height=y, width=width, brush=color, pen=None))
                else:
                    self.plot.plot(x, y, name=label,
                                   pen=None if kind == "scatter" else pg.mkPen(color, width=1.5),
                                   symbol=series.get("symbol", "o" if kind in ("scatter", "errorbar") else None),
                                   symbolSize=5 if series.get("symbol") else 4,
                                   symbolBrush=color, symbolPen=None, connect="finite")
                if series.get("errors") is not None:
                    errors = numbers(series["errors"])
                    if len(errors) == len(x):
                        valid = np.isfinite(x) & np.isfinite(y) & np.isfinite(errors)
                        self.plot.addItem(pg.ErrorBarItem(x=x[valid], y=y[valid], top=errors[valid], bottom=errors[valid], pen=color))
        if model.get("xticks"):
            self.plot.getAxis("bottom").setTicks([list(enumerate(model["xticks"]))])
        for axis, angle in (("horizontal", 0), ("vertical", 90)):
            for reference in model.get(axis, []):
                self.plot.addItem(pg.InfiniteLine(reference["y" if angle == 0 else "x"], angle=angle,
                                  pen=pg.mkPen("#a85f65", style=QtCore.Qt.PenStyle.DashLine), label=reference.get("label", "")))
        for interval, color in ((model.get("baseline_interval"), "#d8eadc"),):
            if interval:
                self.plot.addItem(pg.LinearRegionItem(interval, brush=color, movable=False))
        self.plot.enableAutoRange()
