"""Offline pressure v2 end-to-end tests; never opens hardware."""
import csv
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from sspp_experiments.temperature_pressure import CaptureConfig, SerialCaptureSession
from sspp_experiments.temperature_pressure.protocol import LineParser
from sspp_experiments.temperature_pressure.pressure_control import PressureControl
from sspp_experiments.temperature_pressure.firmware import PreparationSession, firmware_bundle
from sspp_experiments.temperature_pressure.serial_io import PosixSerialReader


def frame(t=100, channel="S3", token=123, zero_id=0, count=0, baseline="NA", delta="NA", baseline_ms="NA"):
    return f"P,{t},{channel},556.0,2.7175,{baseline},{delta},{zero_id},{baseline_ms},{count},{token}\n".encode()


def ack(channel="S3", token=123):
    return f"# SELECT token={token} channel={channel} mask=0x{1 << (int(channel[1])-1):02X} adc=A2 verified=1\n".encode()


def wait_for(fn, timeout=2):
    limit = time.monotonic()+timeout
    while time.monotonic() < limit:
        if fn(): return
        time.sleep(.005)
    raise AssertionError("offline worker timed out")


class Duplex:
    def __init__(self):
        self.chunks = [b"# Pressure_Diagnostic v2; pressure_v2\n"]
        self.writes = []
        self.closed = False
        self.token = None
        self.channel = None
        self.t = 100

    def read(self, timeout=.1):
        time.sleep(.003)
        return self.chunks.pop(0) if self.chunks else None

    def write(self, data):
        self.writes.append(data)
        words = data.decode().split()
        if words[0] == 'SELECT':
            previous_channel, previous_token = self.channel or 'S3', self.token or 0
            self.channel, self.token = words[1], int(words[2])
            # Deliberately include old S3/token0 before ACK in the same read.
            self.chunks.append(frame(t=self.t-1, channel=previous_channel,token=previous_token) + ack(self.channel, self.token) + b''.join(frame(t=self.t+i*100,channel=self.channel,token=self.token) for i in range(3)))
            self.t += 300
        else:
            operation = 'zero' if words[0]=='Z' else 'clear'
            self.chunks.append(f'# COMMAND token={self.token} op={operation} channel={self.channel} accepted=1\n'.encode())
        return len(data)

    def close(self): self.closed=True


class ProtocolTests(unittest.TestCase):
    def parse(self, raw):
        return LineParser('pressure_v2').feed(raw,host_time_utc='2026-11-04T00:00:00Z',host_elapsed_s=0)

    def test_exact_fields_and_manual_delta(self):
        rows, events=self.parse(frame(zero_id=7,count=40,baseline='2.7000',delta='0.0175',baseline_ms=99))
        self.assertEqual(events,[])
        self.assertEqual(rows[0]['zero_id'],7)
        self.assertEqual(rows[0]['baseline_v'],2.7)
        self.assertEqual(rows[0]['channel'],'S3')
        self.assertEqual(rows[0]['selection_token'],123)
        rows,_=self.parse(frame(zero_id=1,count=12))
        self.assertEqual(rows[0]['baseline_v'],'')

    def test_invalid_fields_never_become_rows(self):
        bad=[frame(channel='S6'),frame(count=41),frame(count=40),frame(zero_id=1,count=40,baseline='2.7',delta='1',baseline_ms=12),frame(token=2**32),b'100,2.7\n',frame(zero_id=0,count=1)]
        for raw in bad:
            with self.subTest(raw=raw):
                rows,events=self.parse(raw)
                self.assertFalse(rows)
                self.assertEqual(events[0]['kind'],'malformed_line')

    def test_stale_rows_and_wrong_token_are_not_accepted(self):
        events=[]; control=PressureControl('S2',events.append,token=123)
        transport=Duplex(); parser=LineParser('pressure_v2')
        control.select(transport)
        rows,messages=parser.feed(frame(token=0)+ack('S2')+frame(t=200,channel='S2'),host_time_utc='',host_elapsed_s=0)
        self.assertEqual(len(control.consume(rows,messages,transport)),1)
        rows,messages=parser.feed(frame(t=300,channel='S2',token=999),host_time_utc='',host_elapsed_s=.2)
        with self.assertRaisesRegex(RuntimeError,'令牌'):
            control.consume(rows,messages,transport)

    def test_zero_rejection_and_timeout_are_fatal(self):
        control=PressureControl('S3',lambda e:None,token=123);reader=Duplex()
        control.select(reader)
        rows,events=self.parse(ack()+frame())
        control.consume(rows,events,reader)
        control.command(reader,'zero')
        control.pending_since=time.monotonic()-11
        with self.assertRaisesRegex(RuntimeError,'未确认'):control.tick(reader)
        rows,events=self.parse(b'# COMMAND token=123 channel=S3 op=zero accepted=0 reason=not_ready\n')
        with self.assertRaisesRegex(RuntimeError,'拒绝'):control.consume(rows,events,reader)

    def test_boot_banner_after_old_frame_and_delayed_retry_ack(self):
        parser=LineParser('pressure_v2');control=PressureControl('S3',lambda e:None,token=123);reader=Duplex()
        rows,events=parser.feed(frame(token=0)+b'# Pressure_Diagnostic v2; pressure_v2\n',host_time_utc='',host_elapsed_s=0)
        self.assertFalse(control.consume(rows,events,reader))
        current=control.token
        self.assertNotEqual(current,123)
        rows,events=parser.feed(ack(token=123)+frame(t=200,token=123)+ack(token=current)+frame(t=300,token=current),host_time_utc='',host_elapsed_s=.2)
        accepted=control.consume(rows,events,reader)
        self.assertEqual(len(accepted),1)
        self.assertEqual(accepted[0]['selection_token'],current)
        # Delayed first ACK arriving after a timeout retry remains stale too.
        control=PressureControl('S3',lambda e:None,token=123)
        control.select(reader);control.last_send=time.monotonic()-2;control.tick(reader)
        current=control.token;self.assertNotEqual(current,123)
        rows,events=self.parse(ack(token=123)+frame(token=123)+ack(token=current)+frame(t=200,token=current))
        self.assertEqual(len(control.consume(rows,events,reader)),1)

    def test_stale_zero_ack_before_new_session_banner_is_ignored(self):
        control=PressureControl('S3',lambda e:None,token=123);reader=Duplex();parser=LineParser('pressure_v2')
        raw=b'# COMMAND token=999 channel=S1 op=zero accepted=1\n# Pressure_Diagnostic v2; pressure_v2\n'
        rows,events=parser.feed(raw,host_time_utc='',host_elapsed_s=0)
        self.assertFalse(control.consume(rows,events,reader))
        rows,events=parser.feed(ack()+frame(),host_time_utc='',host_elapsed_s=.1)
        self.assertEqual(len(control.consume(rows,events,reader)),1)

    def test_all_channels_validate_exact_mask(self):
        for n in range(1,6):
            channel=f'S{n}'; control=PressureControl(channel,lambda e:None,token=123); reader=Duplex()
            control.select(reader)
            rows,events=self.parse(ack(channel)+frame(channel=channel))
            self.assertEqual(control.consume(rows,events,reader)[0]['channel'],channel)
        control=PressureControl('S3',lambda e:None,token=123)
        rows,events=self.parse(ack().replace(b'0x04',b'0x08'))
        with self.assertRaisesRegex(RuntimeError,'不一致'):control.consume(rows,events,Duplex())


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.reader=Duplex(); self.session=SerialCaptureSession(self.tmp.name,lambda p,b:self.reader,first_sample_timeout_s=.2)
        self.addCleanup(self.session.stop)

    def start(self, channel='S2'):
        self.session.start(CaptureConfig('/dev/cu.FAKE','pressure_v2','sample',channel='实物第二块待核对',pressure_channel=channel,pressure_calibration=True))
        wait_for(lambda:self.session.snapshot()['rows']==3)

    def test_real_ack_required_and_archive_keeps_actual_channel(self):
        self.start('S5');self.session.stop()
        state=self.session.snapshot();self.assertEqual(state['pressure_control']['confirmed_channel'],'S5')
        self.assertEqual({r['channel'] for r in state['points']},{'S5'})
        manifest=json.loads((self.session.run_path/'manifest.json').read_text())
        self.assertEqual(manifest['channels'],['S5'])
        self.assertEqual(manifest['status'],'complete',manifest['errors'])
        self.assertEqual(manifest['pressure_control']['selection_token'],self.reader.token)
        self.assertIn('SELECT S5',self.reader.writes[0].decode())
        self.assertTrue(self.reader.closed)

    def test_zero_waits_for_future40_then_preserves_manual_delta(self):
        self.start();self.session.pressure_command('zero')
        with self.assertRaisesRegex(ValueError,'等待'): self.session.plateau_command('begin')
        wait_for(lambda:len(self.reader.writes)==2)
        self.reader.chunks.append(frame(t=400,channel='S2',token=self.reader.token,zero_id=1,count=2))
        wait_for(lambda:self.session.snapshot()['rows']==4)
        self.assertEqual(self.session.snapshot()['pressure_control']['pending'],'zero')
        self.reader.chunks.append(frame(t=500,channel='S2',token=self.reader.token,zero_id=1,count=40,baseline='2.7',delta='.0175',baseline_ms=500))
        wait_for(lambda:self.session.snapshot()['rows']==5)
        state=self.session.snapshot();self.assertIsNone(state['pressure_control']['pending'])
        self.assertAlmostEqual(state['points'][-1]['delta_voltage_v'],.0175)
        self.assertEqual(state['points'][-1]['response_delta_voltage_v'],0)
        self.session.pressure_command('clear');wait_for(lambda:len(self.reader.writes)==3)
        self.reader.chunks.append(frame(t=600,channel='S2',token=self.reader.token))
        wait_for(lambda:self.session.snapshot()['rows']==6)
        self.assertIsNone(self.session.snapshot()['pressure_control']['pending'])
        self.session.stop()
        with (self.session.run_path/'samples.csv').open() as f: rows=list(csv.DictReader(f))
        self.assertEqual(rows[-2]['delta_voltage_v'],'0.0175')
        self.assertEqual(rows[-1]['delta_voltage_v'],'')

    def test_midrun_reset_and_channel_mismatch_stop_without_mislabeled_rows(self):
        for raw in (b'# Pressure_Diagnostic v2; pressure_v2\n',frame(t=400,channel='S4',token=123),b'# ERROR I2C\n'):
            with self.subTest(raw=raw),tempfile.TemporaryDirectory() as tmp:
                reader=Duplex();session=SerialCaptureSession(tmp,lambda p,b:reader)
                session.start(CaptureConfig('/dev/cu.FAKE','pressure_v2','sample',pressure_channel='S2'))
                wait_for(lambda:session.snapshot()['rows']==3)
                reader.chunks.append(raw)
                wait_for(lambda:not session.is_running)
                self.assertEqual(session.snapshot()['rows'],3)
                self.assertEqual(session.snapshot()['status'],'failed_or_incomplete')
                self.assertTrue(reader.closed)

    def test_legacy_output_cannot_verify_selected_channel(self):
        self.reader.chunks=[b'100,2.7\n150,2.8\n']
        self.session.start(CaptureConfig('/dev/cu.FAKE','pressure_v2','sample'))
        wait_for(lambda:not self.session.is_running)
        self.assertEqual(self.session.snapshot()['rows'],0)
        self.assertEqual(self.reader.writes,[])

    def test_active_interval_blocks_zero_and_pending_commands_block_begin(self):
        self.start()
        self.session.plateau_command('begin', target_force_n=1, reference_source='offline force', pressure_channel='S2', direction='loading', cycle=1)
        # Even before the worker applies the queued begin, zero cannot overtake it.
        with self.assertRaisesRegex(ValueError,'区间'):self.session.pressure_command('zero')
        wait_for(lambda:self.session.snapshot()['plateau'].get('active'))
        with self.assertRaisesRegex(ValueError,'区间'):self.session.pressure_command('clear')
        self.assertEqual(len(self.reader.writes),1)
        self.session.plateau_command('end')
        wait_for(lambda:not self.session.snapshot()['plateau'].get('active'))
        self.session.pressure_command('zero')
        wait_for(lambda:len(self.reader.writes)==2)
        with self.assertRaisesRegex(ValueError,'等待'):self.session.plateau_command('begin')

    def test_runtime_switch_records_channels_and_resets_live_windows(self):
        self.start('S2')
        first_token=self.reader.token
        self.session.pressure_command('select',channel='S5')
        wait_for(lambda:self.session.snapshot()['rows']==6)
        state=self.session.snapshot()
        self.assertEqual(state['pressure_control']['confirmed_channel'],'S5')
        self.assertNotEqual(first_token,self.reader.token)
        self.assertEqual(set(state['plateau']['channels']),{'S5'})
        self.assertEqual(len(state['pressure_control']['selection_history']),2)
        self.session.plateau_command('begin', target_force_n=1, reference_source='offline', pressure_channel='S2', direction='loading', cycle=1)
        wait_for(lambda:self.session.snapshot()['plateau'].get('last_error'))
        self.assertIsNone(self.session.snapshot()['plateau']['active'])
        self.session.plateau_command('begin', target_force_n=1, reference_source='offline', pressure_channel='S5', direction='loading', cycle=1)
        wait_for(lambda:self.session.snapshot()['plateau'].get('active'))
        with self.assertRaisesRegex(ValueError,'区间'):self.session.pressure_command('select',channel='S1')
        self.session.plateau_command('end')
        wait_for(lambda:not self.session.snapshot()['plateau'].get('active'))
        self.session.pressure_command('select',channel='S2')
        wait_for(lambda:self.session.snapshot()['rows']==9)
        self.assertEqual(set(self.session.snapshot()['plateau']['channels']),{'S2'})
        self.session.stop()
        manifest=json.loads((self.session.run_path/'manifest.json').read_text())
        self.assertEqual(manifest['status'],'complete',manifest['errors'])
        self.assertEqual(manifest['channels'],['S2','S5'])
        self.assertEqual(manifest['initial_pressure_channel'],'S2')
        self.assertEqual(len(manifest['pressure_control']['selection_history']),3)
        self.assertEqual(self.session.snapshot()['points'][-1]['response_delta_voltage_v'],0)

    def test_v2_preparation_binding_includes_channel(self):
        config=CaptureConfig('/dev/cu.FAKE','pressure_v2','sample',pressure_channel='S2',preparation={'port':'/dev/cu.FAKE','protocol':'pressure_v2','preparation_key':'x','pressure_channel':'S3'})
        with self.assertRaisesRegex(ValueError,'准备记录'):config.validate()


class PreparationTests(unittest.TestCase):
    def test_verify_matches_requested_channel_and_releases_serial(self):
        with tempfile.TemporaryDirectory() as tmp:
            reader=Duplex();session=PreparationSession(tmp,transport_factory=lambda p,b:reader,verify_timeout_s=.3)
            session.start('/dev/cu.usbserialFAKE','pressure_v2',upload=False,pressure_channel='S4')
            wait_for(lambda:not session.is_running)
            state=session.snapshot()
            self.assertEqual(state['status'],'ready',state)
            self.assertTrue(session.matches('/dev/cu.usbserialFAKE','pressure_v2',pressure_channel='S4'))
            self.assertFalse(session.matches('/dev/cu.usbserialFAKE','pressure_v2',pressure_channel='S3'))
            self.assertTrue(reader.closed)
            self.assertEqual(state['result']['pressure_control']['confirmed_channel'],'S4')
            self.assertEqual(len(state['result']['preview']),3)


class TransportAndBundleTests(unittest.TestCase):
    def test_bounded_serial_write_handles_partial_and_rejects_readonly(self):
        reader=object.__new__(PosixSerialReader);reader.fd=123;reader.writable=True
        with patch('sspp_experiments.temperature_pressure.serial_io.select.select',return_value=([],[123],[])), patch('sspp_experiments.temperature_pressure.serial_io.os.write',side_effect=[2,4]) as write:
            self.assertEqual(reader.write(b'Z 123\n'),6)
            self.assertEqual(write.call_count,2)
        with patch('sspp_experiments.temperature_pressure.serial_io.select.select',return_value=([],[],[])), self.assertRaises(TimeoutError):
            reader.write(b'Z 123\n')
        reader.writable=False
        with self.assertRaises(OSError):reader.write(b'Z 123\n')

    def test_bundle_is_new_application_and_retains_legacy_packages(self):
        root=Path(__file__).resolve().parents[1]
        bundle=firmware_bundle(root,'pressure_v2')
        self.assertEqual(bundle['application_bytes'],13176)
        self.assertEqual(bundle['hardware_validation'],'not_uploaded_or_hardware_validated')
        for protocol in ('pressure_v1','temperature_v1'):
            self.assertGreater(firmware_bundle(root,protocol)['application_bytes'],0)
