import importlib.machinery
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]

def load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module

helper = load('experimental_helper', ROOT / 'deployment/passwordless/omagpu-control')
engine = load('experimental_engine', ROOT / 'scripts/gpu_engine.py')

def response(stdout='', code=0, stderr=''):
    return SimpleNamespace(stdout=stdout, returncode=code, stderr=stderr)

class PowerLimits(unittest.TestCase):
    def test_out_of_range_or_nonfinite_values_never_write(self):
        for value in ('122', '146', 'nan', 'inf'):
            with self.subTest(value=value), patch.object(helper.subprocess, 'run', return_value=response('123, 145, 145')) as run:
                with self.assertRaises(ValueError):
                    helper.set_power_limit('0', value)
                self.assertEqual(run.call_count, 1)
                self.assertNotIn('-pl', run.call_args.args[0])

    def test_min_max_and_default_use_driver_limits(self):
        for value, expected in [('123', '123'), ('145', '145'), ('default', '145')]:
            with self.subTest(value=value), patch.object(helper.subprocess, 'run', side_effect=[response('123, 145, 145'), response()]) as run:
                result = helper.set_power_limit('0', value)
                self.assertEqual(result['status'], 'success')
                self.assertEqual(run.call_args.args[0], ['/usr/bin/nvidia-smi', '-i', '0', '-pl', expected])

    def test_unavailable_or_malformed_limits_never_write(self):
        for limits in ('N/A, N/A, N/A', '145, 123, 130', '123, inf, 145', '123, 145', '123, 145, 999'):
            with self.subTest(limits=limits), patch.object(helper.subprocess, 'run', return_value=response(limits)) as run:
                with self.assertRaises(ValueError):
                    helper.set_power_limit('0', '130')
                self.assertEqual(run.call_count, 1)

    def test_driver_write_failure_is_not_success(self):
        with patch.object(helper.subprocess, 'run', side_effect=[response('123, 145, 145'), response(code=1, stderr='Not supported')]):
            self.assertEqual(helper.set_power_limit('0', '130')['status'], 'error')

    def test_argument_injection_rejected_before_gpu_lookup(self):
        with patch.object(helper, 'nvidia_control_target') as lookup:
            for value in ('130; id', '--help', 'nan', '-1', '145\n'):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    helper.control(['--set-power-limit', 'nvidia0', value])
            lookup.assert_not_called()

class PowerTelemetry(unittest.TestCase):
    def test_limits_match_gpu_index_and_omit_unsupported(self):
        gpus = [{'index': 0}, {'index': 1}]
        engine.attach_power_limits(gpus, '1, 123, 145, 145\n0, N/A, N/A, N/A')
        self.assertNotIn('powerLimits', gpus[0])
        self.assertEqual(gpus[1]['powerLimits'], {'min': 123, 'max': 145, 'default': 145})

    def test_invalid_ranges_stay_unavailable(self):
        for row in ('0, 145, 123, 130', '0, nan, 145, 145', '0, 123, 145, 999', '0, 123, inf, 145'):
            gpu = {'index': 0}
            engine.attach_power_limits([gpu], row)
            self.assertNotIn('powerLimits', gpu)

    def test_old_controller_keeps_power_actions_disabled(self):
        with patch.object(engine.subprocess, 'run', return_value=response('{"status":"success","euid":1000}')):
            self.assertFalse(engine.power_control_available())

    def test_updated_controller_enables_power_capability(self):
        with patch.object(engine.subprocess, 'run', return_value=response('{"status":"success","euid":1000,"powerLimit":true}')):
            self.assertTrue(engine.power_control_available())

    def test_helper_error_is_preserved(self):
        with patch.object(engine.os, 'geteuid', return_value=1000), patch.object(engine.subprocess, 'run', return_value=response('{"status":"error","message":"Power limit must be between 123 and 145 W"}', 1)):
            result = engine.run_self_pkexec('--set-power-limit', 'nvidia0', '1')
            self.assertIn('123 and 145', result['message'])

if __name__ == '__main__':
    unittest.main()
