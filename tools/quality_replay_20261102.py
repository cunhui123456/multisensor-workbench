"""Read-only IQ replay and synthetic checks; never imports or opens USRP devices.

Example (baseline is the preserved pre-change logic.py):
  python -B tools/quality_replay_20261102.py --run /path/to/run \
      --baseline-logic /path/to/v4_logic.py --output /tmp/replay.json

Saved runs end at their original accepted measurement or timeout. Exhausting a
short accepted trace under another window schedule cannot establish live failure.
Sample-time deadlines and one original RX frame per fetch model neither USB
transport timing nor the application's concurrent queue drain schedule.
"""
from __future__ import annotations
import argparse
from collections import Counter
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import numpy as np


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_logic(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    # The replay clock is sample based; do not sleep for simulated queue waits.
    module.time = SimpleNamespace(monotonic=module.time.monotonic, sleep=lambda _: None)
    return module


def replay(module, x, frame_lengths, rate, tone, previous, budget=8.):
    """One point, unmodified runner state machine, original sequential frames."""
    cfg = module.SweepConfig(num_points=1, sweep_repeat=1, tone_start=tone,
                             freq_start=5e9, freq_stop=5e9)
    stop = threading.Event()
    original_quality = module.quality_measurement
    # A standalone point still receives its true predecessor from the full run.
    module.quality_measurement = lambda data, fs, target, unused, config: original_quality(
        data, fs, target, previous, config)
    limit = min(len(x), int(rate * budget))
    ends = np.cumsum(frame_lengths)
    starts = np.r_[0, ends[:-1]]

    class ReplayRunner(module.SweepRunner):
        position = 0
        received = 0
        consumed = 0
        next_required = max(int(np.ceil(cfg.verify_min_seconds*rate)),
                            int(np.ceil(cfg.verify_cycles*rate/tone)),
                            int(np.ceil(cfg.adaptive_window_seconds[0]*rate)))
        trace_exhausted = False
        quality = None

        def _set_freq_and_tone(self, *args):
            pass

        def _clear_queue(self):
            pass

        def _fetch_available_frames(self):
            if self.received >= int(rate*budget):
                raise module.PointQualityTimeout('sample-time 8-second deadline')
            if self.position < len(starts) and starts[self.position] < limit:
                start = int(starts[self.position])
                end = min(int(ends[self.position]), limit)
                self.position += 1
                self.received = end
                return [x[start:end]], x[start:end]
            # Drain already-delivered complete windows before declaring EOF.
            if self.received-self.consumed >= self.next_required:
                return [], None
            self.trace_exhausted = True
            stop.set()
            return [], None

        def point_deadline_expired(self):
            return self.received/rate >= budget

        def on_quality_attempt(self, q):
            self.quality.append(q)
            self.consumed = q['window_start'] + q['window_count'] - q.get('retained_prefix_count', 0)
            measuring = (q['passed'] and q['state']=='VERIFY'
                         and q['verified_windows_before']+1 >= cfg.verify_consecutive_windows)
            verify, measure = self._calc_samples(tone)
            self.next_required = max(measure if measuring else verify,
                                     int(np.ceil(cfg.adaptive_window_seconds[q['next_adaptive_level_index']]*rate)))

    runner = ReplayRunner(SimpleNamespace(rate=rate, rx_rate_actual=rate), cfg,
                          None, None, stop)
    runner.quality = []
    error = None
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            runner.run(enable_plot=False)
    except (TimeoutError, RuntimeError) as exc:
        error = str(exc)
    finally:
        module.quality_measurement = original_quality
    accepted = next((q for q in runner.quality if q['accepted']), None)
    return {'accepted': accepted is not None,
            'measurement': ({k: accepted.get(k) for k in
                             ('measured_tone_hz', 'amplitude_dB', 'target_amplitude_dB',
                              'window_start', 'window_count', 'window_duration_s',
                              'adaptive_level_index', 'target_snr_db', 'target_confirmation', 'amplitude_frequency_hz')}
                            if accepted else None),
            'accepted_windows_nonoverlapping': (all(a['window_start']+a['window_count'] <= b['window_start'] for a,b in zip(runner.quality[-3:-1], runner.quality[-2:])) if accepted else None),
            'quality_attempts': len(runner.quality),
            'passed_windows': sum(q['passed'] for q in runner.quality),
            'rejection_counts': dict(Counter(r for q in runner.quality for r in q['rejection_reasons'])),
            'delivered_seconds': runner.received/rate,
            'consumed_seconds': runner.consumed/rate,
            'trace_exhausted': runner.trace_exhausted,
            'sample_deadline_reached': runner.received/rate >= budget,
            'error': error}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, required=True)
    parser.add_argument('--baseline-logic', type=Path, required=True)
    parser.add_argument('--candidate-logic', type=Path,
                        default=Path(__file__).resolve().parents[1]/'sspp_experiments/material_sweep/logic.py')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--noise-seeds', type=int, default=100)
    parser.add_argument('--noise-rate', type=float, default=20000.)
    parser.add_argument('--full-rate-noise-seeds', type=int, default=32)
    opts = parser.parse_args()
    old = load_logic(opts.baseline_logic, 'quality_replay_baseline')
    new = load_logic(opts.candidate_logic, 'quality_replay_candidate')
    logs = [json.loads(line) for line in (opts.run/'sweep_quality.jsonl').read_text().splitlines()]
    accepted_files = {q['raw_iq_file'] for q in logs if q.get('accepted')}
    report = {'method': 'Offline, one original RX frame per fetch, 8 s sample-time deadline; original predecessor tone. Not a USB/timing/live RF test. Saved successful traces stop at their original acceptance, so candidate trace exhaustion does not prove live failure.',
              'baseline': {'path': str(opts.baseline_logic), 'sha256': sha(opts.baseline_logic), 'version': old.SWEEP_ALGORITHM_VERSION},
              'candidate': {'path': str(opts.candidate_logic), 'sha256': sha(opts.candidate_logic), 'version': new.SWEEP_ALGORITHM_VERSION},
              'run': str(opts.run), 'points': [], 'noise': []}
    for path in sorted(opts.run.glob('iq_point_*.npz')):
        with np.load(path) as archive:
            x = archive['iq']
            rate = float(archive['rx_rate'])
            lengths = archive['frame_lengths']
            info = json.loads(str(archive['point_info']))
        matching = [q for q in logs if q.get('raw_iq_file')==path.name]
        previous = matching[0]['previous_tone_hz'] if matching else None
        tone = float(info['tone_hz'])
        row = {'file': path.name, 'sha256': sha(path), 'sample_rate': rate,
               'samples': len(x), 'tone': tone, 'previous_tone': previous,
               'historically_accepted': path.name in accepted_files,
               'baseline': replay(old, x, lengths, rate, tone, previous),
               'candidate': replay(new, x, lengths, rate, tone, previous)}
        logged_accept = next((q for q in matching if q.get('accepted')), None)
        row['candidate_same_logged_window_pass'] = (new.quality_measurement(x[logged_accept['window_start']:logged_accept['window_start']+logged_accept['window_count']], rate, tone, previous, new.SweepConfig())['passed'] if logged_accept else None)
        report['points'].append(row)
        print(json.dumps({'file': path.name, 'historical': row['historically_accepted'],
                          'baseline': row['baseline']['accepted'], 'candidate': row['candidate']['accepted'],
                          'candidate_exhausted': row['candidate']['trace_exhausted']}), flush=True)
    for rate, kind, seeds in ((opts.noise_rate, 'white', opts.noise_seeds),
                               (opts.noise_rate, 'colored', opts.noise_seeds),
                               (250000., 'white', opts.full_rate_noise_seeds)):
        n = int(8*rate)
        frame_length = max(1, int(round(rate*4096/250000)))
        lens = np.full(n//frame_length, frame_length, dtype=int)
        if n % frame_length:
            lens = np.r_[lens, n % frame_length]
        accepted_old, accepted_new = [], []
        for seed in range(seeds):
            rng = np.random.default_rng(510200+seed)
            x = rng.normal(size=n)+1j*rng.normal(size=n)
            if kind=='colored':
                frequencies = np.fft.fftfreq(n, 1/rate)
                x = np.fft.ifft(np.fft.fft(x)/np.sqrt(100+abs(frequencies)))
            x = x.astype(np.complex64)
            a, b = replay(old,x,lens,rate,2000,None), replay(new,x,lens,rate,2000,None)
            if a['accepted']:
                accepted_old.append(seed)
            if b['accepted']:
                accepted_new.append(seed)
        result = {'kind': kind, 'sample_rate': rate, 'seconds_per_seed': 8,
                  'seeds': seeds, 'first_rng_seed': 510200,
                  'baseline_accepted_seeds': accepted_old, 'candidate_accepted_seeds': accepted_new}
        report['noise'].append(result)
        print(json.dumps(result), flush=True)
    report['synthetic_windows'] = []
    for duration in (.02, .1, 1.):
        fs = 250000.
        count = int(fs*duration)
        times = np.arange(count)/fs
        rng = np.random.default_rng(371)
        noise = .001*(rng.normal(size=count)+1j*rng.normal(size=count))
        for case in ('background_dominates', 'previous_persistent', 'previous_first_half', 'previous_second_half', 'wrong_tone_only'):
            samples = noise.copy()
            previous = None
            if case=='wrong_tone_only':
                samples += .01*np.exp(2j*np.pi*2500*times)
            else:
                samples += .01*np.exp(2j*np.pi*2000*times)
                if case=='background_dominates':
                    samples += .03*np.exp(2j*np.pi*1200*times)
                else:
                    previous = 1000.
                    residual = .03*np.exp(2j*np.pi*previous*times)
                    if case=='previous_first_half':
                        residual[count//2:] = 0
                    elif case=='previous_second_half':
                        residual[:count//2] = 0
                    samples += residual
            baseline = old.quality_measurement(samples,fs,2000,previous,old.SweepConfig())
            candidate = new.quality_measurement(samples,fs,2000,previous,new.SweepConfig())
            row = {'duration': duration, 'sample_rate': fs, 'case': case,
                   'expected_candidate_pass': case=='background_dominates',
                   'baseline_pass': baseline['passed'], 'candidate_pass': candidate['passed'],
                   'candidate_reasons': candidate['rejection_reasons']}
            row['expected_result_observed'] = row['candidate_pass']==row['expected_candidate_pass']
            report['synthetic_windows'].append(row)
    groups = {}
    for label, predicate in [('historically_invalid', lambda r:not r['historically_accepted']),
                             ('historically_valid', lambda r:r['historically_accepted'])]:
        rows = [r for r in report['points'] if predicate(r)]
        groups[label] = {'count': len(rows),
                         'baseline_accepted': sum(r['baseline']['accepted'] for r in rows),
                         'candidate_accepted': sum(r['candidate']['accepted'] for r in rows),
                         'candidate_same_logged_window_pass': sum(r['candidate_same_logged_window_pass'] is True for r in rows),
                         'candidate_trace_exhausted_unaccepted': sum(r['candidate']['trace_exhausted'] and not r['candidate']['accepted'] for r in rows)}
    report['summary'] = groups
    opts.output.parent.mkdir(parents=True, exist_ok=True)
    opts.output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps(groups, ensure_ascii=False), flush=True)


if __name__=='__main__':
    main()
