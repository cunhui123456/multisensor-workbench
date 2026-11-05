"""Surface recorded RF failures without retrying hardware or weakening stop evidence."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sspp_desktop.config import Settings
from sspp_desktop.services import DesktopService
from sspp_lab.controller import DEFAULTS, LabController, LabError


class AcquisitionFailureSummaryTests(unittest.TestCase):
    def test_recorded_transport_failure_survives_collection_and_invalidates_only_usrp(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            output = root / 'Data/runs/fault'
            output.mkdir(parents=True)
            telemetry = {'rf_start_attempted': True, 'tx_end_of_burst_sent': False,
                         'rx_stop_command_sent': False, 'workers_stopped': True,
                         'failure_diagnosis': 'usrp_transport_control_unresponsive'}
            errors = ['TX: TX send returned zero samples', 'RX shutdown: wait_for_ack accum_timeout',
                      'TX shutdown: TX EOB send accepted 0 of 1 sample; stop unconfirmed',
                      'No quality-approved RF measurements; missing points are not valid data']
            manifest = output / 'manifest.json'
            manifest.write_text(json.dumps({'status': 'failed_or_incomplete', 'telemetry': telemetry,
                                            'errors': errors}))
            original = manifest.read_bytes()
            service = DesktopService(Settings(root=root))
            for kind, target in (('usrp', {'kind': 'usrp', 'profile': 'own'}),
                                 ('serial', {'kind': 'serial', 'port': '/dev/cu.FAKE', 'protocol': 'temperature_v1'})):
                service._device_environments[kind] = {'state': 'ready', 'target': target,
                                                       'message': 'ready', 'log_path': None}
            serial_before = service.environment_snapshot('serial')
            service._environment_binding = ('fake-profile', 'fake-hash')
            job = {'id': 'fault', 'status': 'running', 'config': {**DEFAULTS, 'profile': 'own'},
                   'output': 'Data/runs/fault', 'errors': [], 'rf_stopped': False, 'hardware_ready': True}
            service.job = job
            test = self

            class Process:
                stdout = io.StringIO(json.dumps({'event': 'STOP_STATUS', **telemetry}) + '\n')

                def wait(self):
                    test.assertEqual(job['status'], 'saving')
                    test.assertIn('B210 通信无响应', job['failure_summary'])
                    test.assertFalse(job['rf_stopped'])
                    return 2

                def poll(self):
                    return 2

            with patch('sspp_lab.controller.subprocess.Popen') as launch:
                service._collect(Process(), job)
                launch.assert_not_called()
            self.assertEqual(job['status'], 'stop_unconfirmed')
            self.assertFalse(job['rf_stopped'])
            self.assertTrue(all(error in job['errors'] for error in errors))
            self.assertIn('停止状态未确认', job['failure_summary'])
            self.assertIn('重新连接 B210，再初始化', job['failure_summary'])
            self.assertIsNone(service._environment_binding)
            state = service.environment_snapshot('usrp')
            self.assertEqual(state['state'], 'failed')
            self.assertEqual(state['message'], job['failure_summary'])
            self.assertEqual(service.environment_snapshot('serial'), serial_before)
            with patch.object(LabController, 'start') as launch:
                with self.assertRaises(LabError):
                    service.start({**DEFAULTS, 'profile': 'own', 'setup_confirmed': True})
                launch.assert_not_called()
            saved = json.loads((output / 'desktop_context.json').read_text())
            self.assertEqual(saved['failure_summary'], job['failure_summary'])
            self.assertEqual(saved['failure_diagnosis'], telemetry['failure_diagnosis'])
            self.assertFalse(saved['rf_stopped_confirmed'])
            self.assertEqual(manifest.read_bytes(), original)

    def test_missing_diagnosis_does_not_invent_transport_failure(self):
        for evidence in ({}, {'failure_diagnosis': 'quality_deadline'}, {'rf_stopped_confirmed': False}):
            with self.subTest(evidence=evidence):
                job = {'errors': ['TX: TX send returned zero samples']}
                LabController._record_failure_diagnosis(job, evidence)
                self.assertNotIn('failure_summary', job)
                self.assertNotIn('failure_diagnosis', job)

    def test_summary_needs_each_stop_evidence_not_just_event_flag(self):
        evidence = {'failure_diagnosis': 'usrp_transport_control_unresponsive',
                    'rf_stopped_confirmed': True, 'workers_stopped': True}
        job = {}
        LabController._record_failure_diagnosis(job, evidence)
        self.assertIn('停止状态未确认', job['failure_summary'])
        evidence.update(tx_end_of_burst_sent=True, rx_stop_command_sent=True)
        LabController._record_failure_diagnosis(job, evidence)
        self.assertIn('收发停止已确认', job['failure_summary'])


if __name__ == '__main__':
    unittest.main()
