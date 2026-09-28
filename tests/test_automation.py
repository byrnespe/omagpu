import contextlib
import importlib.machinery
import importlib.util
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]

def module(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    result = importlib.util.module_from_spec(spec)
    loader.exec_module(result)
    return result

app = module('automation', ROOT / 'scripts/automation.py')
helper = module('advanced_helper', ROOT / 'deployment/passwordless/omagpu-control')

class FakeWatch:
    def __init__(self, card, spec):
        self.spec, self.result, self.released = spec, None, False
    def touch(self): pass
    def finished(self): return self.result
    def release(self): self.released = True
    def recover(self): self.result = {'status': 'success'}

class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = 100
        self.exec = Mock(return_value={'status': 'success'})
        self.c = app.Controller(Path(self.tmp.name) / 'settings.json', self.exec, FakeWatch, lambda: self.now)
        self.c.caps['nvidia0'] = {'graphics': [180, 1500, 3090], 'memory': [405, 14001], 'powerMin': 123, 'powerMax': 145, 'powerDefault': 145}
        self.c.notify = Mock()
        self.s = self.c.gpu('nvidia0')
        self.eco = {'graphics': 1500, 'memory': 405, 'power': 123, 'fan': 'auto'}
        self.c.config['profiles']['Eco'] = self.eco
    def tearDown(self): self.tmp.cleanup()
    def command(self, op, **kw): return self.c.command(dict(op=op, gpu='nvidia0', **kw))
    def baseline(self): self.command('apply-profile', name='Default'); self.exec.reset_mock()

    def test_startup_is_inert(self):
        self.c.tick('nvidia0', 90, {'blender'})
        self.exec.assert_not_called()
        self.assertFalse(self.s['curve'] or self.s['apps'] or self.s['guard'])

    def test_curve_interpolation_monotonic_validation(self):
        self.assertEqual(app.curve_duty([[30, 30], [70, 70], [85, 100]], 50), 50)
        for points in ([[40, 30], [30, 100]], [[30, 60], [50, 30], [85, 100]], [[30, 30], [90, 100]], [[30, 30], [85, 90]], [[float('nan'), 30], [85, 100]]):
            with self.subTest(points=points), self.assertRaises(ValueError): app.validate_curve(points)

    def test_curve_debounce_and_missing_sensor_returns_auto(self):
        self.command('set-curve', enabled=True, points=app.DEFAULT_CURVE)
        self.c.tick('nvidia0', 50, set())
        self.exec.assert_called_once_with('nvidia0', '--set-fan', 102)
        self.now += 2; self.c.tick('nvidia0', 51, set())
        self.assertEqual(self.exec.call_count, 1)
        for _ in range(3): self.c.tick('nvidia0', None, set())
        self.assertFalse(self.s['curve'])
        self.assertEqual(self.exec.call_args.args, ('nvidia0', '--set-fan', 'auto'))

    def test_profile_persists_but_activation_does_not(self):
        self.command('save-profile', name='Work', settings=self.eco)
        loaded = app.Controller(self.c.path, self.exec, FakeWatch)
        self.assertEqual(loaded.config['profiles']['Work'], self.eco)
        self.assertFalse(loaded.gpu('nvidia0')['apps'])
        self.assertIsNone(loaded.gpu('nvidia0')['current'])

    def test_invalid_profile_is_rejected_before_hardware(self):
        for key, value in [('graphics', 99999), ('memory', 900), ('power', 146), ('power', float('nan')), ('fan', 256)]:
            profile = dict(self.eco); profile[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError): self.command('save-profile', name='Bad', settings=profile)
        self.exec.assert_not_called()

    def test_failed_apply_rolls_back_known_prior_values(self):
        self.baseline()
        self.exec.side_effect = [{'status': 'success'}, RuntimeError('clock failed'), {'status': 'success'}]
        with self.assertRaisesRegex(RuntimeError, 'previous settings restored'): self.command('apply-profile', name='Eco')
        self.assertEqual(self.s['current'], app.DEFAULT)
        self.assertEqual(self.exec.call_args.args, ('nvidia0', '--set-fan', 'auto'))

    def test_timed_profile_requires_baseline_and_restores(self):
        with self.assertRaisesRegex(ValueError, 'restore'): self.command('temporary', name='Eco', seconds=60)
        self.baseline()
        self.command('temporary', name='Eco', seconds=60)
        self.assertEqual(self.s['current'], self.eco)
        self.assertEqual(self.s['watchdog'].spec['restore'], app.DEFAULT)
        self.assertEqual(self.s['watchdog'].spec['seconds'], 60)
        self.s['watchdog'].result = {'status': 'success'}
        self.c.tick('nvidia0', 40, set())
        self.assertIsNone(self.s['timer'])
        self.assertEqual(self.s['current'], app.DEFAULT)

    def test_cancel_timer_restores_and_releases_watchdog(self):
        self.baseline(); self.command('temporary', name='Eco', seconds=60)
        watcher = self.s['watchdog']
        self.command('cancel-timer')
        self.assertTrue(watcher.released)
        self.assertEqual(self.s['current'], app.DEFAULT)

    def test_app_rules_first_match_then_restore_on_exit(self):
        self.baseline()
        self.command('save-rules', rules=[{'app': 'blender', 'profile': 'Eco'}, {'app': 'game', 'profile': 'Default'}])
        self.command('set-apps', enabled=True)
        self.c.tick('nvidia0', 40, {'blender', 'game'})
        self.assertEqual(self.s['appActive'], 'blender')
        self.assertEqual(self.s['current'], self.eco)
        self.c.tick('nvidia0', 40, set())
        self.assertEqual(self.s['current'], app.DEFAULT)
        self.assertIsNone(self.s['appRestore'])

    def test_alert_hysteresis_and_cooldown(self):
        self.command('set-alert', temperature=80, enabled=True)
        for t in (80, 82, 79, 80): self.c.tick('nvidia0', t, set())
        self.assertEqual(self.c.notify.call_count, 1)
        self.now += 61
        self.c.tick('nvidia0', 74, set()); self.c.tick('nvidia0', 80, set())
        self.assertEqual(self.c.notify.call_count, 2)

    def test_thermal_fallback_preempts_timer_and_latches(self):
        self.baseline(); self.command('temporary', name='Eco', seconds=60)
        self.command('set-guard', temperature=85, enabled=True)
        self.s['watchdog'].result = {'status': 'success', 'thermal': True}
        self.c.tick('nvidia0', 86, set())
        self.assertTrue(self.s['latched'])
        self.assertIsNone(self.s['timer'])
        with self.assertRaises(ValueError): self.command('manual', key='power', value=145)
        with self.assertRaises(ValueError): self.command('acknowledge')
        self.c.tick('nvidia0', 79, set()); self.command('acknowledge')
        self.assertFalse(self.s['latched'])

    def test_shutdown_invokes_recovery(self):
        self.command('set-curve', enabled=True)
        watcher = self.s['watchdog']
        self.c.shutdown()
        self.assertEqual(watcher.result, {'status': 'success'})

class HelperTests(unittest.TestCase):
    def test_clock_cap_uses_supported_minimum_and_requested_maximum(self):
        response = Mock(returncode=0, stdout='', stderr='')
        with patch.object(helper, 'supported_clocks', return_value=[[405,180], [14001,3090]]), patch.object(helper.subprocess,'run',return_value=response) as run:
            helper.set_clock('0', 'graphics', '3090')
            self.assertEqual(run.call_args.args[0][-2:], ['-lgc','180,3090'])
            helper.set_clock('0', 'memory', '14001')
            self.assertEqual(run.call_args.args[0][-2:], ['-lmc','14001,14001'])
            with self.assertRaises(ValueError): helper.set_clock('0','graphics','4000')
            self.assertEqual(run.call_count, 2)

    def run_watch(self, spec, text, *, ready=True, subprocess_results=None):
        stream = io.StringIO(text)
        with patch.object(helper.os,'geteuid',return_value=0), patch.object(helper.signal,'signal'), patch.object(helper.sys,'stdin',stream), patch.object(helper,'read_watchdog_message',side_effect=stream.readline), patch.object(helper.select,'select',return_value=([stream] if ready else [],[],[])), patch.object(helper,'restore_controls',return_value={'status':'success'}) as restore, patch.object(helper.subprocess,'run',side_effect=subprocess_results), contextlib.redirect_stdout(io.StringIO()):
            result = helper.watchdog('nvidia0',spec)
            return result, restore.call_args_list

    def test_queued_release_is_not_lost_to_stdio_buffering(self):
        read, write = os.pipe()
        os.write(write, b'heartbeat\nrelease\n')
        os.close(write)
        with os.fdopen(read) as stream, patch.object(helper.sys, 'stdin', stream):
            self.assertEqual(helper.read_watchdog_message(), 'heartbeat\n')
            self.assertEqual(helper.read_watchdog_message(), 'release\n')

    def test_controller_eof_and_timeout_restore_auto_fan(self):
        for ready in (True,False):
            result,calls=self.run_watch({'cooling':True},'',ready=ready)
            self.assertEqual(result['status'],'success')
            self.assertEqual(calls[0].args,('nvidia0',None,True))

    def test_timer_deadline_restores_even_with_healthy_controller(self):
        with patch.object(helper.time, 'monotonic', side_effect=[0, 11]):
            result, calls = self.run_watch({'restore': app.DEFAULT.copy(), 'seconds': 10}, 'heartbeat\n')
        self.assertEqual(result['status'], 'success')
        self.assertEqual(calls[0].args, ('nvidia0', app.DEFAULT, False))

    def test_repeated_sensor_timeout_triggers_thermal_fallback(self):
        timeout = helper.subprocess.TimeoutExpired('nvidia-smi', 3)
        result, calls = self.run_watch({'thermal':85}, 'heartbeat\nheartbeat\nheartbeat\n',
            subprocess_results=[timeout, timeout, timeout, Mock(returncode=0, stdout='123')])
        self.assertTrue(result['thermal'])
        self.assertEqual(calls[0].args[1]['power'], '123')

    def test_clean_release_does_not_write_hardware(self):
        result,calls=self.run_watch({'cooling':True},'release\n')
        self.assertTrue(result['released']); self.assertEqual(calls,[])

    def test_thermal_fallback_does_not_restore_hot_profile(self):
        result,calls=self.run_watch({'thermal':85,'restore':dict(app.DEFAULT,power=145)},'heartbeat\n',subprocess_results=[Mock(returncode=0,stdout='90'),Mock(returncode=0,stdout='123')])
        self.assertTrue(result['thermal'])
        self.assertEqual(calls[0].args,('nvidia0',dict(app.DEFAULT,power='123'),True))
        self.assertEqual(len(calls),1)

    def test_invalid_watchdog_and_clock_arguments_rejected(self):
        with patch.object(helper.os,'geteuid',return_value=0):
            for spec in ({'cooling':True,'seconds':float('inf')},{'thermal':99},{'restore':{'fan':'auto'}},{'shell':'id'}):
                with self.subTest(spec=spec),self.assertRaises(ValueError): helper.watchdog('nvidia0',spec)
            with self.assertRaises(ValueError): helper.control(['--set-gpu-clock','nvidia0','3000;id'])

if __name__ == '__main__': unittest.main()
