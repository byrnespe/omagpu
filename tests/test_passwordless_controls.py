import importlib.machinery,importlib.util,unittest
from pathlib import Path
from unittest.mock import patch
REPO_ROOT = Path(__file__).resolve().parents[1]
loader=importlib.machinery.SourceFileLoader('helper', str(REPO_ROOT / 'deployment/passwordless/omagpu-control'))
spec=importlib.util.spec_from_loader('helper',loader)
h=importlib.util.module_from_spec(spec)
loader.exec_module(h)
class Controls(unittest.TestCase):
    def test_rejects_unbounded_inputs_without_touching_hardware(self):
        invalid=[[],['sh','-c','id'],['--set-fan','../../etc/passwd','100'],['--set-fan','nvidia0;id','100'],['--set-fan','nvidia0','256'],['--set-fan','nvidia0','-1'],['--set-power-profile','card0','high;id'],['--set-fan','card0','auto','extra']]
        with patch.object(h,'nvidia_control_target') as lookup:
            for args in invalid:
                with self.subTest(args=args),self.assertRaises(ValueError):h.control(args)
            lookup.assert_not_called()
    def test_valid_power_and_fan_route_to_gpu_only(self):
        with patch.object(h.os,'geteuid',return_value=0),patch.object(h,'nvidia_control_target',return_value='0'),patch.object(h,'set_nvidia_performance',return_value={'status':'success'}) as power,patch.object(h,'set_nvidia_fan',return_value={'status':'success'}) as fan:
            h.control(['--set-power-profile','nvidia0','auto'])
            power.assert_called_once_with('nvidia0','0','auto')
            h.control(['--set-fan','nvidia0','auto'])
            fan.assert_called_once_with('0','auto')
    def test_check_does_not_touch_hardware(self):
        with patch.object(h,'nvidia_control_target') as lookup:
            self.assertEqual(h.control(['--check'])['status'],'success')
            lookup.assert_not_called()
    def test_plugin_uses_bounded_helper(self):
        spec=importlib.util.spec_from_file_location('engine',str(REPO_ROOT / 'scripts/gpu_engine.py'))
        engine=importlib.util.module_from_spec(spec);spec.loader.exec_module(engine)
        with patch.object(engine.os,'geteuid',return_value=1000),patch.object(engine.subprocess,'run') as run:
            run.return_value.returncode=0;run.return_value.stdout='{"status":"success"}'
            self.assertEqual(engine.run_self_pkexec('--set-fan','nvidia0','auto')['status'],'success')
            self.assertEqual(run.call_args.args[0],['/usr/bin/pkexec','/usr/local/libexec/omagpu-control','--set-fan','nvidia0','auto'])
if __name__ == '__main__':
    unittest.main()
