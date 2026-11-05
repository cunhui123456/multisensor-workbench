"""独立 Agg 子进程绘制比较报告，避免与 Qt 绘图线程争用。"""
import json
import argparse
from pathlib import Path
import sys


def export_figures(report, directory):
    from matplotlib import font_manager
    from matplotlib.backends.backend_pdf import PdfPages
    if __package__:
        from .publication import single_record_report, build_analysis_figure
    else:
        from publication import single_record_report, build_analysis_figure
    single = single_record_report(report)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    font = Path('/System/Library/Fonts/Supplemental/Arial Unicode.ttf')
    font_properties = font_manager.FontProperties(fname=str(font)) if font.is_file() else None
    charts = report.get("charts", [])
    if not charts:
        raise ValueError("没有可绘制的对比数据")
    if report.get("kind") == "temperature_calibration":
        if __package__:
            from .publication import export_temperature_publication
        else:
            from publication import export_temperature_publication
        export_temperature_publication(report, directory)
    with PdfPages(directory / "comparison_report.pdf") as pdf:
        for index, chart in enumerate(charts, 1):
            fig = build_analysis_figure(report, chart["id"])
            fig.set_size_inches(12, 7.5)
            ax = fig.axes[0]
            ax.set_title(chart["title"], fontproperties=font_properties, loc="left")
            source = next((s for s in report.get("sources", []) if s["key"] == report.get("reference_key")), {})
            if report.get("kind") == "pressure_calibration":
                heading = "压力标定观测分析 · 参考力与明确接触面积分开保存 · 候选拟合未激活"
            elif single and report.get("kind") == "temperature_calibration":
                heading = "温标记录分析 · 一条已保存记录的区间均值与样本 SD · 无自动标定"
            elif single and report.get("kind") == "temperature_pressure":
                heading = "串口记录分析 · 时间为本记录采集起点后的实收时间"
            elif single:
                heading = "实验记录分析 · 仅展示本条记录的测量结果"
            elif report.get("kind") in ("material_sweep", "liquid_temperature_sweep", "temperature_sweep"):
                heading = f"参考：{source.get('label', '')} · {report.get('reference_key', '')}  |  差值 = 目标 − 参考"
            elif report.get("kind") == "liquid_temperature_phase":
                heading = "各温度平台使用各自run内的参考位置；只比较相对响应，远离为正"
            elif report.get("kind") == "temperature_calibration":
                heading = "温度区间组合分析 · 多记录汇总，保留全部测量点与样本 SD · 无自动标定"
            elif report.get("kind") == "temperature_pressure":
                heading = "串口记录组合分析 · 各记录使用自己的开始时间，不代表硬件同步"
            else:
                heading = "各实验使用自己的静止参考；参考结束为时间零点，不代表动作同步"
            fig.suptitle(heading, fontproperties=font_properties, fontsize=9, color="#586c79")
            ax.set_xlabel(chart["xlabel"], fontproperties=font_properties)
            ax.set_ylabel(chart["ylabel"], fontproperties=font_properties)
            handles, labels = ax.get_legend_handles_labels()
            if handles:
                ax.legend(handles, labels, prop=font_properties, fontsize=8, ncols=2)
            import textwrap
            note = chart.get("note", "")
            fig.text(.02, .015, "\n".join(textwrap.wrap(note, 78)), fontproperties=font_properties,
                     fontsize=8, color="#586c79", va="bottom")
            fig.get_layout_engine().set(rect=(0, .09, 1, .91))
            fig.savefig(directory / f"comparison_{index:02d}_{chart['id']}.png", dpi=170)
            pdf.savefig(fig)
            fig.clear()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report")
    parser.add_argument("directory")
    parser.add_argument("--chart-id")
    parser.add_argument("--output")
    opts = parser.parse_args()
    report = json.loads(Path(opts.report).read_text())
    if opts.chart_id is not None:
        if not opts.output:
            parser.error("--chart-id requires --output")
        from publication import export_analysis_figure
        export_analysis_figure(report, opts.chart_id, opts.output)
    else:
        if opts.output:
            parser.error("--output requires --chart-id")
        export_figures(report, opts.directory)
