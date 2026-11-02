"""Publication figures from saved temperature intervals; no fitting or calibration."""
from pathlib import Path
import json

import numpy as np

RUN_COLORS = ("#24445C", "#D47732", "#32866C", "#986BA0", "#A85B63", "#6988A5", "#807448", "#595959")
RUN_MARKERS = ("o", "s", "^", "D", "v", "P", "X", "h")
CALIBRATION_COLOR = "#24445C"
CALIBRATION_MARKER = "o"
PUBLICATION_STYLE = {"font.family": "sans-serif", "font.sans-serif": ["Arial", "Arial Unicode MS", "DejaVu Sans"],
                     "font.size": 8, "axes.labelsize": 8, "xtick.labelsize": 7, "ytick.labelsize": 7,
                     "pdf.fonttype": 42, "ps.fonttype": 42, "svg.fonttype": "none",
                     "axes.unicode_minus": True, "savefig.facecolor": "white"}
PUBLICATION_FILES = tuple(f"{stem}.{ext}" for stem in
                          ("publication_figure", "publication_S6", "publication_S7")
                          for ext in ("pdf", "svg", "png")) + ("caption.txt",)


def single_record_report(report):
    """Honor the analysis contract, with a source-count fallback for old reports."""
    mode = report.get("analysis_mode")
    return mode == "single" if mode is not None else len(report.get("sources", [])) == 1


def temperature_charts(report):
    """Select only saved response channels; never require or invent a missing channel."""
    import re
    charts = [chart for chart in report.get("charts", [])
              if chart.get("publication", {}).get("kind") == "calibration_response"]
    by_channel = {}
    for chart in charts:
        channel = chart.get("publication", {}).get("channel")
        if not isinstance(channel, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,31}", channel):
            raise ValueError("温标响应图缺少有效通道标识")
        if channel in by_channel:
            raise ValueError("同一温标通道存在重复响应图")
        by_channel[channel] = chart
    if not by_channel:
        raise ValueError("温标论文图需要至少一个已保存通道的响应图及来源")
    return [by_channel[channel] for channel in sorted(by_channel)]


def publication_files(report):
    """Enumerate complete-publication artifacts for the channels actually present."""
    stems = ["publication_figure"] + ["publication_" + chart["publication"]["channel"]
                                      for chart in temperature_charts(report)]
    return tuple(f"{stem}.{extension}" for stem in stems for extension in ("pdf", "svg", "png")) + ("caption.txt",)


def series_style(series, fallback_index=0):
    index = int(series.get("run_index", fallback_index + 1)) - 1
    return RUN_COLORS[index % len(RUN_COLORS)], RUN_MARKERS[index % len(RUN_MARKERS)]


def _data(series):
    data = np.asarray(series.get("data", []), dtype=float)
    if data.size == 0:
        return np.empty(0), np.empty(0), None
    if data.ndim != 2 or data.shape[1] != 2:
        raise ValueError("温标响应数据必须为温度、电压数值对")
    x, y = data[:, 0], data[:, 1]
    errors = series.get("errors", series.get("y_error"))
    if errors is not None:
        errors = np.asarray(errors, dtype=float)
        if errors.shape != x.shape or np.any(errors[np.isfinite(errors)] < 0):
            raise ValueError("温标样本 SD 必须与测量点对应且非负")
    valid = np.isfinite(x) & np.isfinite(y)
    # Temperature order is visual only; duplicate observations and SDs remain.
    order = np.argsort(x[valid], kind="stable")
    return x[valid][order], y[valid][order], errors[valid][order] if errors is not None else None


def _draw_response(ax, chart, panel):
    from matplotlib.ticker import MaxNLocator, ScalarFormatter
    for index, series in enumerate(chart.get("series", [])):
        x, y, sd = _data(series)
        if not len(x):
            continue
        ax.errorbar(x, y, yerr=sd, color=CALIBRATION_COLOR, marker=CALIBRATION_MARKER,
                    markersize=3.5, markerfacecolor="white", markeredgewidth=.85,
                    linewidth=.8, elinewidth=.7, capsize=2, capthick=.7,
                    linestyle="-", zorder=3)
    if panel is not None:
        ax.set_title(f"({panel}) {chart['publication']['channel']}", loc="left", fontsize=9, fontweight="bold", pad=7)
    ax.set_xlabel("Reference temperature (°C)", labelpad=5)
    ax.set_ylabel("Voltage (V)", labelpad=5)
    ax.xaxis.set_major_locator(MaxNLocator(nbins=7, steps=[1, 2, 5, 10], min_n_ticks=4))
    ax.yaxis.set_major_locator(MaxNLocator(nbins=5, min_n_ticks=4))
    ax.yaxis.set_major_formatter(ScalarFormatter(useOffset=False))
    ax.tick_params(direction="out", length=3, width=.65, pad=3)
    for edge in ("top", "right"):
        ax.spines[edge].set_visible(False)
    for edge in ("left", "bottom"):
        ax.spines[edge].set_linewidth(.65)
    ax.grid(False)
    ax.margins(x=.07, y=.15)


def build_temperature_figure(report, channels=None):
    """Build a 180 mm multi-panel figure or an 88 mm single-channel figure."""
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    charts = {chart["publication"]["channel"]: chart for chart in temperature_charts(report)}
    channels = tuple(charts) if channels is None else tuple(channels)
    if not channels or any(channel not in charts for channel in channels):
        raise ValueError("请选择已有温标通道")
    columns = min(2, len(channels))
    rows = (len(channels) + columns - 1) // columns
    width_mm = 180 if columns == 2 else 88
    fig = Figure(figsize=(width_mm / 25.4, 82 * rows / 25.4), layout="constrained")
    FigureCanvasAgg(fig)
    axes = np.asarray(fig.subplots(rows, columns, squeeze=False)).ravel()
    for index, (ax, channel) in enumerate(zip(axes, channels)):
        if not any(len(_data(series)[0]) for series in charts[channel].get("series", [])):
            raise ValueError("温标通道没有可导出的有效数据")
        _draw_response(ax, charts[channel], chr(ord("a") + index) if len(channels) > 1 else None)
    for ax in axes[len(channels):]:
        fig.delaxes(ax)
    fig.get_layout_engine().set(w_pad=.035, h_pad=.04, wspace=.12, hspace=.02)
    return fig


def publication_caption(report, charts=None):
    charts = temperature_charts(report) if charts is None else charts
    single = single_record_report(report)
    introduction = ("Temperature–voltage response from one saved experimental record. " if single else
                    f"Combined temperature–voltage response assembled from {len(report.get('sources', []))} independently saved records; "
                    "this is not a single acquisition. ")
    lines = [
        introduction +
        ("Panels " + ", ".join(f"({chr(ord('a')+index)}) {chart['publication']['channel']}" for index, chart in enumerate(charts)) + ". " if len(charts)>1 else
         f"The figure shows {charts[0]['publication']['channel']}. ") +
        "Markers indicate interval mean voltage; "
        "error bars show the sample standard deviation within each interval, not confidence intervals. "
        "Lines connect measured interval means in reference-temperature order as visual guides. "
        "Duplicate temperatures retain every observation and its SD; no averaging of duplicate points, "
        "interpolation or regression is performed. "
        "Reference temperatures are manual readings. No automatic calibration is activated.",
        "", "Source record:" if single else "Source records:",
    ]
    source_numbers = {}
    for index, source in enumerate(report.get("sources", []), 1):
        key = source["key"]
        source_numbers[key] = index
        lines.append(f"Source {index}: {key}")
        parameters = source.get("parameters", {})
        order = parameters.get("reference_temperature_sequence_c")
        if order:
            sequence = " → ".join(f"{float(value):g}" for value in order)
            direction = {"ascending": "increasing", "descending": "decreasing",
                         "mixed_or_single": "mixed order or a single reference"}.get(parameters.get("reference_direction"), "as recorded")
            lines.append(f"  Retained reference-temperature order (°C): {sequence} ({direction}).")
    lines.extend(["", "Point provenance in plotted temperature order:"])
    for chart in charts:
        labels = []
        for series in chart.get("series", []):
            for point, origin in zip(series.get("data", []), series.get("point_sources", [])):
                source = source_numbers.get(origin.get("key"), origin.get("key", "unknown"))
                labels.append(f"{float(point[0]):g} °C: source {source}, interval {origin.get('point_id', 'unknown')}")
        if labels:
            lines.append(chart["publication"]["channel"] + " — " + "; ".join(labels))
    lines.append("Full point identifiers and source hashes are retained in comparison.json and the exported data tables.")
    lines.extend(["", "误差棒为区间内实收电压的样本 SD，非置信区间；参考温度为人工读数；没有自动标定。",
                  ("本图分析一条已保存记录。" if single else "本图汇总多条独立记录，非一次连续采集。")
                  + "按参考温度排序连线只作视觉引导；同温点逐个保留，不平均、不插值、不拟合。",
                  "Nominal figure sizes: two-panel 180 × 82 mm; individual 88 × 82 mm. PNG resolution: 600 dpi. PDF/SVG preserve vector lines and text."])
    excluded = report.get("excluded_calibration_points", [])
    if excluded:
        lines.extend(["", "Excluded reference-temperature intervals (raw samples retained):"])
        for entry in excluded:
            reference = entry.get("reference_mean_c")
            temperature = f"{float(reference):g} °C" if reference is not None else "unavailable reference"
            lines.append(f"{entry.get('run_key', 'unknown source')}, point {entry.get('point_id')}, "
                         f"{temperature}, channels {', '.join(entry.get('channels', []))}: "
                         f"{entry.get('reason', '')} [recorded {entry.get('deleted_utc', 'unknown date')}].")
        lines.append("Exclusions apply to the derived temperature response; retained raw-time overviews are unchanged.")
    if report.get("warnings"):
        lines.extend(["", "Source and interpretation notes:"] + [str(value) for value in report["warnings"]])
    return "\n".join(lines) + "\n"


def export_temperature_publication(report, directory):
    from matplotlib import rc_context
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    caption = publication_caption(report)
    with rc_context(PUBLICATION_STYLE):
        available = tuple(chart["publication"]["channel"] for chart in temperature_charts(report))
        figures = [("publication_figure", available)] + [("publication_"+channel, (channel,)) for channel in available]
        for stem, channels in figures:
            fig = build_temperature_figure(report, channels)
            for extension in ("pdf", "svg", "png"):
                metadata = ({"Title": "Temperature–voltage response", "Subject": caption,
                             "Creator": "Multi-sensor workbench"} if extension == "pdf" else
                            {"Title": "Temperature–voltage response", "Description": caption})
                fig.savefig(directory / f"{stem}.{extension}", dpi=600, metadata=metadata)
            fig.clear()
    (directory / "caption.txt").write_text(caption, encoding="utf-8")
    return [directory / name for name in publication_files(report)]


AXIS_LABELS = {
    "载波 + tone（GHz）": "Carrier + tone frequency (GHz)",
    "共同实际载波 + tone（GHz）": "Common carrier + tone frequency (GHz)",
    "单音幅度（dB 数字估计）": "Tone amplitude (dB, digital estimate)",
    "样本 SD（dB）": "Sample SD (dB)", "数字幅度差（dB）": "Amplitude difference (dB)",
    "距各自基线结束（s）": "Time from baseline end (s)", "相对幅度（dB）": "Relative amplitude (dB)",
    "相对相位（°）": "Relative phase (°)", "相干占比": "Coherent fraction",
    "采集起点后的主机实收时间（s）": "Elapsed acquisition time (s)",
    "各自采集起点后的主机实收时间（s）": "Elapsed acquisition time (s)",
    "电压（V）": "Voltage (V)", "电压 (V)": "Voltage (V)",
}


def _font_options(text, size=8):
    font = Path('/System/Library/Fonts/Supplemental/Arial Unicode.ttf')
    if font.is_file() and any('\u3400' <= char <= '\u9fff' for char in text):
        from matplotlib.font_manager import FontProperties
        return {"fontproperties": FontProperties(fname=str(font), size=size)}
    return {"fontsize": size}


def selected_chart(report, chart_id):
    charts = [chart for chart in report.get("charts", []) if chart.get("id") == chart_id]
    if not isinstance(chart_id, str) or len(charts) != 1:
        raise ValueError("请选择报告中唯一且现有的分析图")
    return charts[0]


def analysis_figure_style(report, chart_id):
    """General scientific preset; individual journals may require other dimensions."""
    chart = selected_chart(report, chart_id)
    calibration = chart.get("publication", {}).get("kind") == "calibration_response"
    height_mm = max(82, min(180, 20 + 7 * len(chart.get("segments", [])))) if chart.get("kind") == "segments" else 82
    return {"style_version": "sspp-publication-v1", "width_mm": 88 if calibration else 180,
            "height_mm": height_mm, "dpi": 600, "axis_font_pt": 8, "tick_font_pt": 7,
            "vector_text": True}


def _series_coordinates(series):
    data = np.asarray(series.get("data", []), dtype=float)
    if not data.size:
        return np.empty(0), np.empty(0), None
    if data.ndim != 2 or data.shape[1] != 2:
        raise ValueError("分析图数据必须为横纵坐标数值对")
    x, y = data[:, 0], data[:, 1]
    errors = series.get("errors", series.get("y_error"))
    sd = None if errors is None else np.asarray(errors, dtype=float)
    if sd is not None and (sd.shape != x.shape or np.any(sd[np.isfinite(sd)] < 0)):
        raise ValueError("误差棒必须与测量点对应且非负")
    return x, y, sd


def _heatmap_coordinates(chart):
    x, y = np.asarray(chart.get("x", []), dtype=float), np.asarray(chart.get("y", []), dtype=float)
    z = np.asarray(chart.get("z", []), dtype=float)
    if (x.ndim != 1 or y.ndim != 1 or not x.size or not y.size or
            z.shape != (y.size, x.size) or not np.all(np.isfinite(x)) or not np.all(np.isfinite(y))):
        raise ValueError("热图坐标与二维测量网格不匹配")
    for coordinate in (x, y):
        if len(coordinate) > 1 and not (np.all(np.diff(coordinate) > 0) or np.all(np.diff(coordinate) < 0)):
            raise ValueError("热图坐标必须严格单调；不能隐式排序或合并测量格点")
    return x, y, z


def build_analysis_figure(report, chart_id):
    """Render a saved chart faithfully, without synthesizing or aggregating samples."""
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    from matplotlib.ticker import MaxNLocator, ScalarFormatter
    from matplotlib import colormaps
    chart = selected_chart(report, chart_id)
    calibration = chart.get("publication", {}).get("kind") == "calibration_response"
    kind = chart.get("kind", "line")
    style = analysis_figure_style(report, chart_id)
    fig = Figure(figsize=(style["width_mm"] / 25.4, style["height_mm"] / 25.4), layout="constrained")
    FigureCanvasAgg(fig)
    ax = fig.subplots()
    if calibration:
        if not any(len(_data(series)[0]) for series in chart.get("series", [])):
            raise ValueError("当前分析图没有可导出的有效数据")
        _draw_response(ax, chart, None)
    else:
        if kind not in ("line", "errorbar", "scatter", "bar", "heatmap", "segments"):
            raise ValueError("当前分析图类型尚不支持独立论文图导出")
        ax.xaxis.set_major_locator(MaxNLocator(nbins=7))
        ax.yaxis.set_major_locator(MaxNLocator(nbins=5))
        ax.yaxis.set_major_formatter(ScalarFormatter(useOffset=False))
        count = 0
        if kind == "heatmap":
            x, y, z = _heatmap_coordinates(chart)
            count = int(np.count_nonzero(np.isfinite(z)))
            cmap = colormaps["viridis"].with_extremes(bad="white")
            mesh = ax.pcolormesh(x, y, np.ma.masked_invalid(z), shading="nearest", cmap=cmap, rasterized=False)
            colorbar = fig.colorbar(mesh, ax=ax, pad=.025)
            if colorbar.solids is not None:
                colorbar.solids.set_rasterized(False)
            label = AXIS_LABELS.get(chart.get("colorbar", ""), chart.get("colorbar", ""))
            colorbar.set_label(label, **_font_options(label))
            colorbar.ax.tick_params(labelsize=7, width=.65, length=3)
            colorbar.outline.set_linewidth(.65)
        elif kind == "segments":
            labels = []
            for index, segment in enumerate(chart.get("segments", [])):
                start, end = float(segment["start"]), float(segment["end"])
                if not np.isfinite([start, end]).all() or end < start:
                    raise ValueError("阶段起止时间必须有效且按时间顺序排列")
                color = "#B7BEC5" if segment.get("transition") else segment.get("color", CALIBRATION_COLOR)
                if end == start:
                    ax.plot([start], [index], marker="|", color=color, markersize=7, linestyle="none")
                else:
                    ax.barh(index, end-start, left=start, height=.65, color=color)
                label = str(segment.get("label", f"Stage {index+1}"))
                if segment.get("contact") is not None:
                    label += " · contact" if segment["contact"] else " · no contact"
                labels.append(label)
                count += 1
            ax.set_yticks(range(len(labels)), labels, **_font_options(' '.join(labels), 7))
        else:
            multiple = len(chart.get("series", [])) > 1
            for index, series in enumerate(chart.get("series", [])):
                x, y, sd = _series_coordinates(series)
                valid = np.isfinite(x) & np.isfinite(y)
                count += int(np.count_nonzero(valid))
                if not valid.any():
                    continue
                color = series.get("color") or RUN_COLORS[index % len(RUN_COLORS)]
                label = (series.get("publication_label") or series.get("label") or f"Series {index + 1}") if multiple else None
                if kind == "bar":
                    values = np.unique(x[valid])
                    width = float(np.median(np.diff(values))) * .7 if len(values) > 1 else .6
                    ax.bar(x[valid], y[valid], width=width, color=color, label=label)
                else:
                    ax.plot(x, y, color=color, linewidth=.8, label=label,
                            linestyle="none" if kind == "scatter" else "-",
                            marker="o" if kind in ("scatter", "errorbar") else None, markersize=3)
                if sd is not None:
                    # NaN keeps unavailable errors absent, without dropping the measurement.
                    ax.errorbar(x, y, yerr=sd, fmt="none", ecolor=color, elinewidth=.7, capsize=2, capthick=.7)
            if multiple:
                handles, labels = ax.get_legend_handles_labels()
                if handles:
                    options = _font_options(' '.join(labels), 7)
                    if "fontproperties" in options:
                        options = {"prop": options["fontproperties"]}
                    ax.legend(handles, labels, frameon=False, loc="best", **options)
        if not count:
            raise ValueError("当前分析图没有可导出的有效数据")
        for reference in chart.get("horizontal", []):
            ax.axhline(reference['y'], color="#7C858C", linewidth=.65, linestyle="--")
        for reference in chart.get("vertical", []):
            ax.axvline(reference['x'], color="#7C858C", linewidth=.65, linestyle="--")
        if chart.get("baseline_interval") is not None:
            start, end = chart["baseline_interval"]
            if not np.isfinite([start, end]).all() or end < start:
                raise ValueError("基线区间必须为有效起止时间")
            ax.axvspan(start, end, color="#D8EADC", alpha=.55, zorder=0)
        for axis in ("x", "y"):
            label = chart.get("publication", {}).get(axis+"label") or chart.get(axis+"label", "")
            label = AXIS_LABELS.get(label, label)
            getattr(ax, 'set_'+axis+'label')(label, labelpad=5, **_font_options(label))
        if chart.get("xticks"):
            labels = [str(label) for label in chart["xticks"]]
            ax.set_xticks(range(len(labels)), labels, **_font_options(' '.join(labels), 7))
        if chart.get("equal_aspect"):
            ax.set_aspect("equal", adjustable="box")
        ax.tick_params(direction="out", length=3, width=.65, pad=3, labelsize=7)
        for edge in ("top", "right"):
            ax.spines[edge].set_visible(False)
        for edge in ("left", "bottom"):
            ax.spines[edge].set_linewidth(.65)
        ax.grid(False)
        if kind != "heatmap":
            ax.margins(x=.035, y=.1)
    fig.get_layout_engine().set(w_pad=.035, h_pad=.04)
    return fig


def analysis_figure_caption(report, chart_id):
    """Human-readable interpretation and machine-readable source mapping for one chart."""
    chart = selected_chart(report, chart_id)
    if chart.get("publication", {}).get("kind") == "calibration_response":
        description = publication_caption(report, [chart])
    else:
        introduction = ("Analysis of one saved experimental record. " if single_record_report(report) else
                        "Analysis of multiple independently saved records; this is not a single acquisition. ")
        data_semantics = ("Mathematical TX reference computed from saved transmitter settings; "
                          "this is not a measured TX waveform or RF output and cannot establish measured TX/RX loss. "
                          if chart.get("source_type") == "computed_setting" else
                          "Saved measured data; gaps and separate source time origins are retained. ")
        description = (introduction + data_semantics +
                       "No interpolation, averaging or fitting is added by the renderer. "
                       "Where supplied, error bars retain sample SD values, not confidence intervals.\n" +
                       str(chart.get("note", "")))
        if chart.get("kind") == "heatmap":
            description += "\nCells show the supplied grid values without interpolation; missing cells are white."
        if chart.get("kind") == "segments":
            description += "\nBars show recorded stage start and end times; gray indicates transition stages."
    description += "\n" + json.dumps({"chart_id": chart_id, "comparison_id": report.get("comparison_id"),
        "analysis_mode": report.get("analysis_mode"), "sources": report.get("sources", []),
        "style": analysis_figure_style(report, chart_id), "source_type": chart.get("source_type"),
        "series_labels": [series.get("label") for series in chart.get("series", [])],
        "horizontal": chart.get("horizontal", []), "vertical": chart.get("vertical", []),
        "baseline_interval": chart.get("baseline_interval"),
        "excluded_calibration_points": report.get("excluded_calibration_points", [])}, ensure_ascii=False)
    return description + "\n"


def analysis_figure_data(report, chart_id):
    """Return every selected stored value, including missing cells and point provenance."""
    chart = selected_chart(report, chart_id)
    kind = chart.get("kind", "line")
    rows = []
    sources = report.get("sources", [])
    source_key = chart.get("source_key") or (sources[0].get("key") if len(sources) == 1 else None)
    if kind == "heatmap":
        x, y, z = _heatmap_coordinates(chart)
        for row, yvalue in enumerate(y):
            for col, xvalue in enumerate(x):
                rows.append({"kind": kind, "source_key": source_key, "row_index": row, "column_index": col,
                             "x": float(xvalue), "y": float(yvalue),
                             "z": float(z[row, col]) if np.isfinite(z[row, col]) else None})
    elif kind == "segments":
        for index, segment in enumerate(chart.get("segments", [])):
            rows.append({"kind": kind, "source_key": source_key, "point_index": index, **segment})
    elif kind in ("line", "errorbar", "scatter", "bar"):
        for index, series in enumerate(chart.get("series", [])):
            x, y, sd = _series_coordinates(series)
            origins = series.get("point_sources", [])
            for point, (xvalue, yvalue) in enumerate(zip(x, y)):
                origin = origins[point] if point < len(origins) else {}
                rows.append({"kind": kind, "series_index": index, "label": series.get("label", ""),
                             "point_index": point, "x": float(xvalue) if np.isfinite(xvalue) else None,
                             "y": float(yvalue) if np.isfinite(yvalue) else None,
                             "error": float(sd[point]) if sd is not None and np.isfinite(sd[point]) else None,
                             "source_key": origin.get("key", series.get("key") or source_key),
                             "source_point_id": origin.get("point_id")})
    else:
        raise ValueError("当前分析图类型尚不支持独立论文图导出")
    return rows


def export_analysis_figure(report, chart_id, destination):
    """Export one scientific figure; the service owns checks and atomic replacement."""
    from matplotlib import rc_context
    destination = Path(destination)
    extension = destination.suffix.lower()
    if extension not in (".pdf", ".svg", ".png"):
        raise ValueError("分析图仅支持 PDF、SVG 或 PNG")
    chart = selected_chart(report, chart_id)
    description = analysis_figure_caption(report, chart_id)
    with rc_context(PUBLICATION_STYLE):
        fig = build_analysis_figure(report, chart_id)
        metadata = ({"Title": str(chart.get("title", chart_id)), "Subject": description,
                     "Creator": "Multi-sensor workbench"} if extension==".pdf" else
                    {"Title": str(chart.get("title", chart_id)), "Description": description})
        try:
            fig.savefig(destination, dpi=600, metadata=metadata)
        finally:
            fig.clear()
    return destination
