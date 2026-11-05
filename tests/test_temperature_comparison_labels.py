"""Saved reference temperatures remain identifiable from report to GUI and export."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from copy import deepcopy
from html import unescape
import json
import unittest
from unittest.mock import patch

from PySide6 import QtCore, QtWidgets

from sspp_experiments.comparison import build_comparison, _temperature_comparison_label
from sspp_desktop.comparison_widget import ComparisonDialog
from sspp_desktop.comparison_export import export_figures
from sspp_desktop.publication import (
    analysis_figure_caption, analysis_figure_data, build_analysis_figure,
)
import test_comparison_domain as fixtures


_MISSING = object()


class TemperatureComparisonLabelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.fixture = fixtures.ComparisonTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def sweep(self, key, reference, *, mode="manual", condition="水", index=0):
        manifest = self.fixture.sweep(
            key, values=((-40. + index, -30. + index), (-42. + index, -32. + index)),
            started=f"2026-11-05T06:{50-index:02d}:00Z",
        )
        manifest["label"] = "水"
        metadata = {"condition_label": condition, "channel": "S6", "calibration": None}
        if mode is not _MISSING:
            metadata["mode"] = mode
        if reference is not _MISSING:
            metadata["reference_temperature_c"] = reference
        self.fixture.save(key, "desktop_context.json", {
            "experiment": "temperature_sweep", "parameters": {"temperature_sweep": metadata},
        })
        if mode != "manual":
            thermal = {"status": "complete", "serial_workers_stopped": True,
                       "serial_closed": True, "data_files_closed": True}
            manifest["temperature_sweep"] = {**thermal, "config": metadata}
            self.fixture.save(key, "temperature/manifest.json", thermal)
            run = self.fixture.root / "Data" / key
            (run / "temperature/samples.csv").write_text("host_elapsed_s,voltage_v\n0,0.5\n")
            (run / "temperature_measurements.csv").write_text("repeat,point,voltage_v\n1,1,0.5\n")
        else:
            manifest["temperature_sweep"] = {"status": "complete", "config": metadata}
        self.fixture.save(key, "manifest.json", manifest)
        return manifest

    def water_report(self):
        temperatures = (35, 42, 45, 49, 52)
        keys = [f"runs/water_{temperature}" for temperature in temperatures]
        for index, (key, temperature) in enumerate(zip(keys, temperatures)):
            self.sweep(key, temperature, index=index)
        return build_comparison(self.fixture.root, keys)

    def test_five_manual_temperatures_identify_sources_charts_metrics_and_tables(self):
        report = self.water_report()
        labels = {f"runs/water_{t}": f"水 · 手填参考 {t} °C" for t in (35, 42, 45, 49, 52)}
        self.assertEqual({source["key"]: source["label"] for source in report["sources"]}, labels)
        self.assertEqual(report["reference_key"], "runs/water_52")
        self.assertTrue(report["quantitative_difference_allowed"])
        for chart in report["charts"]:
            with self.subTest(chart=chart["id"]):
                expected_keys = [key for key in labels
                                 if chart["id"] != "sweep_difference" or key != report["reference_key"]]
                self.assertEqual([series["label"] for series in chart["series"]],
                                 [labels[key] + " · " + key.split("/")[-1] for key in expected_keys])
        self.assertEqual({row["key"]: row["label"] for row in report["metrics"]}, labels)
        for name in ("sweep_summary", "sweep_measurements", "sweep_difference"):
            with self.subTest(table=name):
                rows = report["tables"][name]["rows"]
                self.assertTrue(rows)
                for row in rows:
                    self.assertEqual(row["label"], labels[row["key"]])
        self.assertEqual([row["reference_temperature_c"] for row in report["tables"]["temperature_conditions"]],
                         [35, 42, 45, 49, 52])
        # Labels do not change aggregation, reference choice, or difference direction.
        self.assertEqual([row["mean_amplitude_dB"] for row in report["tables"]["sweep_summary"]["rows"]],
                         [-41., -31., -40., -30., -39., -29., -38., -28., -37., -27.])
        self.assertEqual([row["difference_dB"] for row in report["tables"]["sweep_difference"]["rows"]],
                         [-4., -4., -3., -3., -2., -2., -1., -1.])

    def test_manual_platform_and_legacy_mode_preserve_conditions_and_finite_values(self):
        for mode in ("manual", "platform", _MISSING):
            with self.subTest(mode=mode if mode is not _MISSING else "legacy missing mode"):
                keys = []
                for index, reference in enumerate((0, -2.5, 21.125)):
                    key = f"runs/finite_{index}"
                    self.sweep(key, reference, mode=mode, condition="升温平台", index=index)
                    keys.append(key)
                report = build_comparison(self.fixture.root, keys)
                prefix = "手填参考" if mode == "manual" else "参考"
                self.assertEqual([source["label"] for source in report["sources"]],
                                 [f"水 · 升温平台 · {prefix} {value} °C" for value in ("0", "-2.5", "21.125")])

    def test_missing_invalid_and_nonfinite_references_are_never_displayed_as_temperature(self):
        for reference in (_MISSING, None, True, False, "35", float("nan"), float("inf"), -float("inf")):
            with self.subTest(reference=repr(reference)):
                metadata = {"mode": "manual", "condition_label": "水"}
                if reference is not _MISSING:
                    metadata["reference_temperature_c"] = reference
                self.assertEqual(_temperature_comparison_label("水", metadata), "水 · 参考温度未记录")
                manifest = self.sweep("runs/invalid", reference)
                if isinstance(reference, float):
                    # Existing strict report JSON rejects nonfinite source metadata.
                    with self.assertRaises(ValueError):
                        build_comparison(self.fixture.root, ["runs/invalid"])
                else:
                    # A value elsewhere in the manifest must not fill missing context metadata.
                    manifest["temperature_sweep"]["config"] = {"mode": "manual", "reference_temperature_c": 99}
                    self.fixture.save("runs/invalid", "manifest.json", manifest)
                    report = build_comparison(self.fixture.root, ["runs/invalid"])
                    self.assertEqual(report["sources"][0]["label"], "水 · 参考温度未记录")

    def test_continuous_temperature_remains_ineligible_for_cross_record_comparison(self):
        for index, key in enumerate(("runs/continuous_a", "runs/continuous_b")):
            self.sweep(key, 35, mode="continuous", index=index)
        with self.assertRaisesRegex(ValueError, "连续变温"):
            build_comparison(self.fixture.root, ["runs/continuous_a", "runs/continuous_b"])

    def test_real_report_shows_temperatures_in_reference_picker_and_visible_legends(self):
        report = self.water_report()
        original = deepcopy(report)
        dialog = ComparisonDialog()
        try:
            dialog.apply_report(report)
            dialog.set_busy(False)
            dialog.show()
            self.app.processEvents()
            self.assertTrue(dialog.reference.isVisible())
            self.assertEqual(dialog.reference.currentData(), report["reference_key"])
            self.assertEqual([dialog.reference.itemText(i) for i in range(dialog.reference.count())],
                             [source["label"] + " · " + source["key"] for source in report["sources"]])
            requested = []
            dialog.reference_requested.connect(requested.append)
            dialog.reference.setCurrentIndex(0)
            self.assertEqual(requested, [report["sources"][0]["key"]])
            for index, chart in enumerate(report["charts"]):
                with self.subTest(chart=chart["id"]):
                    dialog.chart_choice.setCurrentIndex(index)
                    self.app.processEvents()
                    self.assertTrue(dialog.legend.isVisible())
                    for series in chart["series"]:
                        self.assertIn(series["label"], unescape(dialog.legend.text()))
            self.assertEqual(report, original)
        finally:
            # Dispose the hidden PyQtGraph legend while its graphics scene is live.
            dialog.plot.legend.clear()
            dialog.close()
            QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
            self.app.processEvents()

    def test_publication_legends_caption_and_data_keep_temperature_labels(self):
        report = self.water_report()
        for chart in report["charts"]:
            with self.subTest(chart=chart["id"]):
                expected = [series["label"] for series in chart["series"]]
                figure = build_analysis_figure(report, chart["id"])
                try:
                    self.assertEqual([text.get_text() for text in figure.axes[0].get_legend().get_texts()], expected)
                    mapping = json.loads(analysis_figure_caption(report, chart["id"]).strip().splitlines()[-1])
                    self.assertEqual(mapping["series_labels"], expected)
                    self.assertEqual(mapping["sources"], report["sources"])
                    self.assertEqual({row["label"] for row in analysis_figure_data(report, chart["id"])}, set(expected))
                finally:
                    figure.clear()

    def test_pdf_export_uses_temperature_sweep_reference_heading(self):
        from matplotlib.figure import Figure
        report = self.water_report()
        headings = []
        original_suptitle = Figure.suptitle

        def capture_title(figure, text, *args, **kwargs):
            headings.append(text)
            return original_suptitle(figure, text, *args, **kwargs)

        output = self.fixture.root / "comparison_export"
        with patch.object(Figure, "suptitle", capture_title):
            export_figures(report, output)
        self.assertEqual(headings, ["参考：水 · 手填参考 52 °C · runs/water_52  |  差值 = 目标 − 参考"] * len(report["charts"]))
        self.assertTrue((output / "comparison_report.pdf").read_bytes().startswith(b"%PDF-"))


if __name__ == "__main__":
    unittest.main()
