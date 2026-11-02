"""Read-only validation of saved sweep IQ; never opens USRP or changes a run.

This independent implementation reports spectral evidence and available window
coverage. A saved window passing new gates is NOT a new acquisition or a
retroactive complete/valid status for the historical experiment.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
import hashlib
import json
from pathlib import Path
import sys

import numpy as np


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def spectrum(samples, rate, *, legacy_tail=False):
    samples = np.asarray(samples, dtype=np.complex64)
    if legacy_tail and len(samples) > 2048:
        samples = samples[-2048:]
    fft_length = max(2048, len(samples))
    weights = np.hanning(len(samples))
    amplitude = np.abs(np.fft.fftshift(np.fft.fft(samples * weights, fft_length))) / weights.sum()
    return np.fft.fftshift(np.fft.fftfreq(fft_length, 1 / rate)), 20 * np.log10(amplitude + 1e-12)


def evaluate(samples, rate, tone, previous_tone=None, *, legacy_tail=False):
    frequency, amplitude = spectrum(samples, rate, legacy_tail=legacy_tail)
    local = np.abs(frequency - tone) <= 1000
    target = int(np.flatnonzero(local)[np.argmax(amplitude[local])])
    noise_bins = local & (np.abs(frequency - tone) > max(200, 3 * rate / len(samples)))
    distinct_previous = previous_tone is not None and abs(previous_tone - tone) > 400
    if distinct_previous:
        noise_bins &= np.abs(frequency - previous_tone) > max(200, 3 * rate / len(samples))
    linear_amplitude = 10 ** (amplitude / 20) - 1e-12
    signal = np.asarray(samples, dtype=np.complex64)
    if legacy_tail and len(signal) > 2048:
        signal = signal[-2048:]
    weights = np.hanning(len(signal))
    coherent_target = float(abs(np.sum(signal * weights * np.exp(-2j * np.pi * tone * np.arange(len(signal)) / rate)) / weights.sum()))
    floor = float(np.median(linear_amplitude[noise_bins])) if np.count_nonzero(noise_bins) >= 5 else None
    prominence = float(20 * np.log10(max(coherent_target, 1e-15) / max(floor, 1e-15))) if floor is not None else None
    previous_peak, separation = None, None
    if distinct_previous:
        old_bins = np.abs(frequency - previous_tone) < 200
        old_peak_linear = float(np.max(linear_amplitude[old_bins]))
        previous_peak = float(20 * np.log10(max(old_peak_linear, 1e-15)))
        separation = float(20 * np.log10(max(coherent_target, 1e-15) / max(old_peak_linear, 1e-15)))
    error = float(abs(frequency[target] - tone))
    return {"sample_count": len(samples), "duration_ms": 1000 * len(samples) / rate,
            "peak_hz": float(frequency[target]), "amplitude_dB": float(amplitude[target]),
            "global_peak_hz": float(frequency[int(np.argmax(amplitude))]),
            "frequency_error_hz": error, "local_prominence_dB": prominence,
            "coherent_target_amplitude_dB": float(20 * np.log10(max(coherent_target, 1e-15))),
            "previous_tone_peak_dB": previous_peak, "target_over_previous_dB": separation,
            "passes_frequency": error < 200,
            "passes_quality": bool(error < 200 and prominence is not None and prominence >= 6 and (separation is None or separation >= 6))}


def coverage(samples, rate, tone, previous_tone):
    verify = int(np.ceil(.020 * rate))
    measure = int(np.ceil(.040 * rate))
    report = {"verify_samples": verify, "measure_samples": measure,
              "has_40ms_recorded": len(samples) >= measure,
              "tail_40ms": evaluate(samples[-measure:], rate, tone, previous_tone) if len(samples) >= measure else None,
              "first_20ms": evaluate(samples[:verify], rate, tone, previous_tone) if len(samples) >= verify else None,
              "passing_40ms_window": None, "passing_20ms_20ms_40ms_sequence": None}
    # This is a coverage search, not runtime replay: use recorded sample order,
    # do not splice buffers, extend short windows, or borrow another RF point.
    step = max(1, int(.004 * rate))
    for total, key in ((measure, "passing_40ms_window"), (2 * verify + measure, "passing_20ms_20ms_40ms_sequence")):
        if len(samples) < total:
            continue
        starts = sorted(set(range(0, len(samples) - total + 1, step)) | {len(samples) - total})
        for start in starts:
            windows = [(start, measure)] if total == measure else [
                (start, verify), (start + verify, verify), (start + 2 * verify, measure)]
            results = [evaluate(samples[left:left + size], rate, tone, previous_tone) for left, size in windows]
            if all(result["passes_quality"] for result in results):
                report[key] = {"start_sample": start, "end_sample_exclusive": start + total, "windows": results}
                break
    return report


def synthetic_checks():
    rate, count, tone = 250000., 10000, 2000.
    time = np.arange(count) / rate
    rng = np.random.default_rng(20261031)
    noise = rng.normal(size=count) + 1j * rng.normal(size=count)
    cases = {
        "strong_target": (.1 * np.exp(2j * np.pi * tone * time) + .001 * noise, True),
        "previous_21k_only": (.1 * np.exp(2j * np.pi * 21000 * time) + .001 * noise, False),
        "wrong_tone_3k": (.1 * np.exp(2j * np.pi * 3000 * time) + .001 * noise, False),
        "seeded_noise_only": (noise, False),
        "target_with_stronger_previous": (.01 * np.exp(2j * np.pi * tone * time)
                                            + .1 * np.exp(2j * np.pi * 21000 * time) + .001 * noise, False),
    }
    result = {}
    for name, (samples, expected) in cases.items():
        metrics = evaluate(samples, rate, tone, 21000.)
        result[name] = {"expected_pass": expected, "matches_expectation": metrics["passes_quality"] == expected, **metrics}
    return result


def inspect_run(run):
    manifest = json.loads((run / "manifest.json").read_text())
    with (run / "measurements.csv").open(newline="") as handle:
        measurements = list(csv.DictReader(handle))
    points = []
    previous = None
    for index, source in enumerate(sorted(run.glob("iq_point_*.npz"))):
        with np.load(source, allow_pickle=False) as data:
            iq = data["iq"]
            rate = float(data["rx_rate"])
            info = json.loads(str(data["point_info"]))
            lengths = data["frame_lengths"]
            starts = data["frame_start_indices"]
            continuous = bool(np.all(starts[1:] == starts[:-1] + lengths[:-1]))
            tone = float(info["tone_hz"])
            original = measurements[index]
            old_count = int(80 * rate / tone)
            matches = []
            for start in np.concatenate(([0], np.cumsum(lengths)[:-1])):
                start = int(start)
                if start + old_count > len(iq):
                    continue
                legacy = evaluate(iq[start:start + old_count], rate, tone, previous, legacy_tail=True)
                if (abs(legacy["peak_hz"] - float(original["measured_tone_Hz"])) < .01
                        and abs(legacy["amplitude_dB"] - float(original["peak_amplitude_dBFS_est"])) < 1e-7):
                    matches.append({"start_sample": start, "end_sample_exclusive": start + old_count,
                                    "legacy_tail": legacy, "full_recorded_window": evaluate(iq[start:start + old_count], rate, tone, previous)})
            row = {"file": source.name, "sha256": sha(source), "point_number": info["point_number"],
                   "tone_hz": tone, "previous_tone_hz": previous, "samples": len(iq),
                   "frame_lengths": lengths.tolist(), "software_frame_indices_contiguous": continuous,
                   "legacy_measurement_samples": old_count,
                   "legacy_recorded_peak_hz": float(original["measured_tone_Hz"]),
                   "matching_legacy_measurement_windows": matches,
                   **coverage(iq, rate, tone, previous)}
            points.append(row)
            previous = tone
    report = {"schema": "sspp.offline_sweep_quality_audit.v1", "run_id": run.name,
              "run_status_unchanged": manifest["status"],
              "source_hashes": {name: sha(run / name) for name in ("manifest.json", "measurements.csv")},
              "policy": {"version": "sspp.sweep.full_window_quality.v2", "verify_ms": 20, "measure_ms": 40, "consecutive_verifications": 2,
                         "frequency_error_strictly_less_hz": 200, "minimum_local_prominence_dB": 6,
                         "minimum_target_over_previous_dB": 6, "coverage_start_step_ms": 4,
                         "prominence_definition": "Exact expected-tone Hann coherent amplitude / median local FFT amplitude; exclude target and previous-tone mainlobes."},
              "limitations": ["Read-only retrospective analysis, no hardware and no historical status changes.",
                              "Continuous software frame indices do not prove hardware timestamp continuity.",
                              "Window coverage search does not reproduce undocumented legacy fetch batching.",
                              "Passing saved windows do not certify a new run or prove calibrated S21.",
                              "Legacy measurement windows shorter than40ms cannot validate the new40ms measurement rule."],
              "points": points, "synthetic_checks": synthetic_checks()}
    report["summary"] = {"point_count": len(points), "recorded_sample_counts": dict(Counter(p["samples"] for p in points)),
                         "points_with_passing_40ms_window": [p["point_number"] for p in points if p["passing_40ms_window"]],
                         "points_with_passing_80ms_sequence": [p["point_number"] for p in points if p["passing_20ms_20ms_40ms_sequence"]],
                         "legacy_measurement_shorter_than40ms": [p["point_number"] for p in points if p["legacy_measurement_samples"] < p["measure_samples"]],
                         "legacy_window_exactly_reproduced": [p["point_number"] for p in points if p["matching_legacy_measurement_windows"]]}
    return report


def compare_implementation(run):
    """Compare full saved IQ and first20ms/last40ms windows against independent gates."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from sspp_experiments.material_sweep.logic import quality_measurement, SweepConfig, SWEEP_ALGORITHM_VERSION
    if SWEEP_ALGORITHM_VERSION != "sspp.sweep.full_window_quality.v2":
        raise SystemExit("This archived audit compares v2 only; use replay_adaptive_sweep.py for the current adaptive algorithm")
    cfg = SweepConfig()
    count, previous, disagreements = 0, None, []
    maximum = {key: 0. for key in ("peak_hz", "amplitude_dB", "local_prominence_dB", "target_over_previous_dB")}
    fields = {"peak_hz": "measured_tone_hz", "amplitude_dB": "amplitude_dB",
              "local_prominence_dB": "target_snr_db", "target_over_previous_dB": "previous_tone_margin_db"}
    for file in sorted(run.glob("iq_point_*.npz")):
        with np.load(file, allow_pickle=False) as data:
            samples, rate = data["iq"], float(data["rx_rate"])
            tone = json.loads(str(data["point_info"]))["tone_hz"]
        for label, window in (("full_recorded_point_IQ", samples),
                              ("first20ms", samples[:int(.020 * rate)]),
                              ("last40ms", samples[-int(.040 * rate):])):
            independent = evaluate(window, rate, tone, previous)
            implementation = quality_measurement(window, rate, tone, previous, cfg)
            mismatch = independent["passes_quality"] != implementation["passed"]
            for independent_key, implementation_key in fields.items():
                if independent[independent_key] is None and implementation[implementation_key] is None:
                    continue
                difference = abs(independent[independent_key] - implementation[implementation_key])
                maximum[independent_key] = max(maximum[independent_key], difference)
                mismatch |= difference > 1e-6
            if mismatch:
                disagreements.append({"point": file.name, "window": label,
                                      "independent": independent, "implementation": implementation})
            count += 1
        previous = tone
    module = Path(__file__).resolve().parents[1] / "sspp_experiments/material_sweep/logic.py"
    return {"source_sha256": sha(module), "real_windows_compared": count,
            "maximum_absolute_metric_difference": maximum, "disagreements": disagreements}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--compare-implementation", action="store_true")
    args = parser.parse_args()
    run, output = args.run.resolve(), args.output.resolve()
    if output == run or run in output.parents:
        parser.error("Write reports outside the raw run directory")
    report = inspect_run(run)
    if args.compare_implementation:
        report["implementation_comparison"] = compare_implementation(run)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    print(json.dumps(report["summary"], ensure_ascii=False))
    if not all(case["matches_expectation"] for case in report["synthetic_checks"].values()):
        raise SystemExit("Synthetic negative/positive control failed")
    if report.get("implementation_comparison", {}).get("disagreements"):
        raise SystemExit("Implementation differs from independent calculation")


if __name__ == "__main__":
    main()
