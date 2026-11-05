"""Pressure calibration invariants and fake transport; never opens hardware."""
import hashlib
import json
from pathlib import Path
from queue import Empty, Queue
import tempfile
import time
import unittest

from sspp_experiments.temperature_pressure import CaptureConfig, SerialCaptureSession
from sspp_experiments.temperature_pressure.pressure import PressureRecorder, candidate_fits, validate_settings


def command(op, t, **values):
    return dict(operation=op, host_elapsed_s=t, host_time_utc='2026-11-04T00:00:00Z', **values)


def row(t, v, channel='A2', segment=0):
    return dict(host_elapsed_s=t, voltage_v=v, channel=channel, time_segment=segment)


def begin(recorder, t=0, force=1, **values):
    fields = dict(target_force_n=force, reference_source='reference-force-meter',
                  pressure_channel='A2', direction='loading', cycle=1)
    fields.update(values)
    recorder.command(command('begin', t, **fields))


def point(recorder, t, force, voltage, **values):
    begin(recorder, t, force, **values)
    for dt in range(31):
        recorder.add_rows([row(t+dt, voltage)])
        if dt in (0, 15, 30):
            recorder.command(command('reference', t+dt, force_n=force))
    recorder.command(command('end', t+30))
    return recorder.points[-1]


def wait_until(condition, timeout=3):
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(.01)
    raise AssertionError('fake capture did not reach expected state')


class PressureDomainTests(unittest.TestCase):
    def test_force_reference_area_and_raw_channel_are_distinct(self):
        recorder = PressureRecorder(('S6', 'S7'))
        begin(recorder, pressure_channel='S7', contact_area_mm2=50, target_force_n=99)
        for dt in range(31):
            recorder.add_rows([row(dt, 1, 'S6'), row(dt, 2, 'S7')])
            if dt in (0, 15, 30):
                recorder.command(command('reference', dt, force_n=2))
        recorder.command(command('end', 30))
        saved = recorder.document()['points'][0]
        self.assertEqual(saved['reference_mean_n'], 2)
        self.assertEqual(saved['reference_mean_kpa'], 40)
        self.assertEqual(set(saved['channels']), {'S6', 'S7'})
        self.assertEqual(saved['target_force_n'], 99)
        self.assertEqual(saved['reference_stability']['status'], 'within_limits')
        self.assertEqual(saved['quality_flags'], [])
        self.assertFalse(saved['automatic_calibration_activated'])
        self.assertNotIn('temperature_c', json.dumps(saved))

    def test_no_area_never_invents_pressure_no_reference_never_uses_target(self):
        recorder = PressureRecorder()
        p = point(recorder, 0, 2, 3)
        self.assertIsNone(p['reference_mean_kpa'])
        begin(recorder, 31, force=100)
        recorder.command(command('end', 61))
        p = recorder.points[-1]
        self.assertIsNone(p['reference_mean_n'])
        self.assertIsNone(p['reference_mean_kpa'])
        self.assertIn('no_reference', p['quality_flags'])
        self.assertEqual(p['reference_stability']['status'], 'insufficient_reference')

    def test_missing_selected_channel_and_short_reference_are_ineligible(self):
        recorder = PressureRecorder(('S6', 'S7'))
        begin(recorder, pressure_channel='S7')
        recorder.add_rows([row(0, 1, 'S6'), row(30, 2, 'S6')])
        recorder.command(command('reference', 30, force_n=1))
        recorder.command(command('end', 30))
        p = recorder.points[0]
        self.assertIn('missing_pressure_channel', p['quality_flags'])
        self.assertEqual(p['reference_stability']['status'], 'insufficient_reference')
        self.assertEqual(candidate_fits(recorder.points)['group_1']['point_count'], 0)

    def test_valid_inverse_fit_supports_decreasing_voltage_and_only_candidate(self):
        recorder = PressureRecorder()
        for n, force in enumerate((0, 1, 2)):
            point(recorder, n*31, force, 3-force*.5)
        fit = candidate_fits(recorder.points)['group_1']
        self.assertEqual(fit['status'], 'candidate')
        self.assertAlmostEqual(fit['slope_n_per_v'], -2)
        self.assertAlmostEqual(fit['intercept_n'], 6)
        self.assertAlmostEqual(fit['rmse_n'], 0)
        self.assertEqual(fit['voltage_min_v'], 2)
        self.assertFalse(fit['independent_validation'])
        self.assertFalse(fit['automatic_calibration_activated'])
        self.assertEqual(recorder.snapshot(93)['candidate_fits'], candidate_fits(recorder.points))

    def test_fit_needs_distinct_levels_and_voltage(self):
        recorder = PressureRecorder()
        for n, force in enumerate((1, 2, 3)):
            point(recorder, n*31, force, 2)
        self.assertEqual(candidate_fits(recorder.points)['group_1']['status'], 'degenerate_voltage')
        recorder.points[-1]['reference_mean_n'] = 2
        self.assertEqual(candidate_fits(recorder.points)['group_1']['status'], 'insufficient_points')

    def test_conditions_and_deleted_or_bad_points_never_merge_into_fit(self):
        recorder = PressureRecorder()
        for n, force in enumerate((0, 1, 2)):
            point(recorder, n*31, force, 1+force)
        for field, value in [('direction', 'unloading'), ('cycle', 2), ('contact_area_mm2', 50),
                             ('reference_source', 'other'), ('pressure_channel', 'S6')]:
            changed = dict(recorder.points[-1], **{field: value})
            fits = candidate_fits([*recorder.points[:2], changed])
            self.assertTrue(all(f['status'] != 'candidate' for f in fits.values()))
        for change in ({'deleted': True}, {'status': 'interrupted'}, {'quality_flags': ['serial_parse_event']},
                       {'reference_stability': {'status': 'outside_limits'}}):
            changed = dict(recorder.points[-1], **change)
            self.assertEqual(candidate_fits([*recorder.points[:2], changed])['group_1']['point_count'], 2)

    def test_interval_flags_reference_variation_reset_and_gap(self):
        recorder = PressureRecorder()
        begin(recorder)
        recorder.add_rows([row(0, 1), row(15, 2, segment=1), row(30, 2, segment=1)])
        for t, force in [(0, 1), (15, 2), (30, 1)]:
            recorder.command(command('reference', t, force_n=force))
        recorder.command(command('interrupt', 30))
        saved = recorder.points[0]
        self.assertEqual(saved['status'], 'interrupted')
        self.assertTrue({'reference_variation_in_interval', 'mcu_reset_in_interval', 'voltage_data_gap'} <= set(saved['quality_flags']))

    def test_validation_rejects_nonfinite_invalid_area_and_ambiguous_channel(self):
        recorder = PressureRecorder()
        for field, value in [('contact_area_mm2', 0), ('contact_area_mm2', float('inf')),
                             ('target_force_n', float('nan')), ('pressure_channel', 'S6'),
                             ('cycle', True), ('direction', '')]:
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                begin(recorder, **{field: value})
            self.assertIsNone(recorder.active)
        begin(recorder)
        with self.assertRaises(ValueError):
            recorder.command(command('reference', 1, force_n=-1))
        self.assertEqual(recorder.active['references'], [])
        recorder.command(command('reference', 2, force_n=1))
        with self.assertRaises(ValueError):
            recorder.command(command('end', 1))
        self.assertIsNotNone(recorder.active)
        with self.assertRaises(ValueError):
            validate_settings({'voltage_rate_limit_v_per_min': 25})


class FakeReader:
    def __init__(self):
        self.queue = Queue()
        self.closed = False
    def read(self, timeout=.1):
        try:
            return self.queue.get(timeout=timeout)
        except Empty:
            return None
    def close(self):
        self.closed = True


class PressureCaptureTests(unittest.TestCase):
    def test_invalid_channel_rejected_before_opening_session(self):
        with tempfile.TemporaryDirectory() as root:
            opened = []
            def transport(*args):
                opened.append(args)
                raise AssertionError('invalid channel must never open transport')
            session = SerialCaptureSession(root, transport)
            for channel in ('A2/S6', 'A2 / S6', 'A 2', '压力', 'A'*33, '2A'):
                with self.subTest(channel=channel), self.assertRaisesRegex(ValueError, '稳定标识'):
                    session.start(CaptureConfig('/dev/cu.FAKE', 'pressure_v1', 'test',
                                                channel=channel, pressure_calibration=True))
                self.assertEqual(opened, [])
                self.assertIsNone(session.thread)
                self.assertFalse((Path(root)/'Data').exists())
            CaptureConfig('/dev/cu.FAKE', 'pressure_v1', 'test', channel='A2',
                          pressure_calibration=True).validate()

    def test_config_mode_conflicts(self):
        for values in ({'temperature_calibration': True}, {'temperature_ramp': {}}, {'pressure_calibration': 'yes'}):
            fields = {'pressure_calibration': True, **values}
            with self.assertRaises(ValueError):
                CaptureConfig('/dev/cu.FAKE', 'temperature_v1', 'test', **fields).validate()

    def test_fake_serial_continuous_intervals_save_seal_and_rejected_commands(self):
        with tempfile.TemporaryDirectory() as root:
            reader = FakeReader()
            session = SerialCaptureSession(root, lambda *_: reader)
            session.start(CaptureConfig('/dev/cu.FAKE', 'pressure_v1', 'pressure sample', pressure_calibration=True))
            reader.queue.put(b'100,1.000\n')
            wait_until(lambda: session.snapshot()['rows'] == 1)
            session.plateau_command('end')
            wait_until(lambda: session.snapshot()['plateau'].get('last_error'))
            session.plateau_command('begin', target_force_n=2, reference_source='load cell',
                                    pressure_channel='A2', direction='loading', cycle=1, contact_area_mm2=50)
            wait_until(lambda: session.snapshot()['plateau'].get('active'))
            session.plateau_command('reference', force_n=1.8)
            reader.queue.put(b'200,1.200\n300,1.400\n')
            wait_until(lambda: session.snapshot()['rows'] == 3)
            session.plateau_command('end')
            wait_until(lambda: len(session.snapshot()['plateau'].get('points', [])) == 1)
            self.assertTrue(session.is_running)
            with self.assertRaises(ValueError):
                session.confirm_ramp_endpoint()
            session.plateau_command('begin', target_force_n=3, reference_source='load cell',
                                    pressure_channel='A2', direction='loading', cycle=1)
            wait_until(lambda: session.snapshot()['plateau'].get('active'))
            self.assertTrue(session.stop())
            manifest = json.loads((session.run_path/'manifest.json').read_text())
            doc = json.loads((session.run_path/'calibration_points.json').read_text())
            self.assertEqual(doc['schema'], 'sspp.pressure_plateaus.v1')
            self.assertEqual(doc['points'][0]['reference_mean_n'], 1.8)
            self.assertEqual(doc['points'][0]['reference_mean_kpa'], 36)
            self.assertEqual(doc['points'][1]['status'], 'interrupted')
            self.assertEqual(manifest['status'], 'complete')
            self.assertIn('pressure_calibration', manifest)
            self.assertNotIn('temperature_calibration', manifest)
            self.assertTrue(reader.closed)
            self.assertEqual((session.run_path/'raw_serial.bin').read_bytes(), b'100,1.000\n200,1.200\n300,1.400\n')
            events = [json.loads(line) for line in (session.run_path/'calibration_events.jsonl').read_text().splitlines()]
            self.assertFalse(events[0]['accepted'])
            for record in manifest['files']:
                path = session.run_path/record['path']
                self.assertEqual(record['sha256'], hashlib.sha256(path.read_bytes()).hexdigest())

    def test_reset_on_first_interval_sample_and_duplicate_stamp_are_flagged(self):
        with tempfile.TemporaryDirectory() as root:
            reader = FakeReader()
            session = SerialCaptureSession(root, lambda *_: reader)
            session.start(CaptureConfig('/dev/cu.FAKE', 'pressure_v1', 'reset', pressure_calibration=True))
            reader.queue.put(b'100,1.000\n')
            wait_until(lambda: session.snapshot()['rows'] == 1)
            session.plateau_command('begin', target_force_n=2, reference_source='load cell',
                                    pressure_channel='A2', direction='loading', cycle=1)
            wait_until(lambda: session.snapshot()['plateau'].get('active'))
            reader.queue.put(b'50,1.100\n50,1.200\n')
            wait_until(lambda: session.snapshot()['rows'] == 3)
            session.plateau_command('end')
            wait_until(lambda: len(session.snapshot()['plateau'].get('points', [])) == 1)
            self.assertTrue(session.stop())
            saved = json.loads((session.run_path/'calibration_points.json').read_text())['points'][0]
            self.assertEqual(saved['channels']['A2']['time_segments'], [1])
            self.assertTrue({'mcu_reset_in_interval', 'duplicate_mcu_time_in_interval'} <= set(saved['quality_flags']))

    def test_fake_dual_capture_preserves_both_channels(self):
        with tempfile.TemporaryDirectory() as root:
            reader = FakeReader()
            session = SerialCaptureSession(root, lambda *_: reader)
            session.start(CaptureConfig('/dev/cu.FAKE', 'temperature_v1', 'dual', pressure_calibration=True))
            reader.queue.put(b'100,600,2.900,140,650,3.100\n')
            wait_until(lambda: session.snapshot()['rows'] == 2)
            session.plateau_command('begin', target_force_n=2, reference_source='load cell',
                                    pressure_channel='S7', direction='loading', cycle=1)
            wait_until(lambda: session.snapshot()['plateau'].get('active'))
            reader.queue.put(b'200,600,2.920,240,650,3.120\n')
            wait_until(lambda: session.snapshot()['rows'] == 4)
            self.assertTrue(session.stop())
            doc = json.loads((session.run_path/'calibration_points.json').read_text())
            self.assertEqual(set(doc['points'][0]['channels']), {'S6', 'S7'})
            self.assertEqual(doc['points'][0]['pressure_channel'], 'S7')


if __name__ == '__main__':
    unittest.main()
