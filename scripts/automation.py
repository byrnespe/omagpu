#!/usr/bin/python3
"""Single-instance, per-user OmaGPU profiles and automation controller."""
import copy
import fcntl
import json
import math
import os
from pathlib import Path
import select
import signal
import socket
import struct
import subprocess
import sys
import time

HELPER = '/usr/local/libexec/omagpu-control'
RUNTIME = Path(os.environ.get('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}')) / 'omagpu-automation'
CONFIG = Path.home() / '.config/omarchy/omagpu-controls.json'
DEFAULT = {'graphics': 'auto', 'memory': 'auto', 'power': 'default', 'fan': 'auto'}
ACTIONS = {'graphics': '--set-gpu-clock', 'memory': '--set-memory-clock', 'power': '--set-power-limit', 'fan': '--set-fan'}
DEFAULT_CURVE = [[35, 30], [50, 40], [65, 60], [75, 80], [85, 100]]


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, indent=2) + '\n')
    temporary.chmod(0o600)
    temporary.replace(path)


def validate_curve(points):
    if not isinstance(points, list) or not 2 <= len(points) <= 10:
        raise ValueError('Use 2–10 temperature/fan points')
    previous = (-1, -1)
    for point in points:
        if not isinstance(point, list) or len(point) != 2 or any(type(v) not in (int, float) or not math.isfinite(v) for v in point):
            raise ValueError('Curve points must be numeric temperature/percentage pairs')
        temp, duty = point
        if not 20 <= temp <= 95 or not 0 <= duty <= 100 or temp <= previous[0] or duty < previous[1]:
            raise ValueError('Temperatures must increase (20–95°C), with nondecreasing fan duty (0–100%)')
        previous = point
    if points[-1][0] > 85 or points[-1][1] != 100:
        raise ValueError('Finish the curve at 100% fan by 85°C')
    return points


def curve_duty(points, temperature):
    if temperature <= points[0][0]:
        return points[0][1]
    for left, right in zip(points, points[1:]):
        if temperature <= right[0]:
            return round(left[1] + (right[1] - left[1]) * (temperature - left[0]) / (right[0] - left[0]))
    return points[-1][1]


def matching_rule(rules, names):
    return next((rule for rule in rules if rule['app'] in names), None)


def process_names():
    names = set()
    for path in Path('/proc').glob('[0-9]*'):
        try:
            if path.stat().st_uid != os.getuid():
                continue
            exe = (path / 'exe').resolve(strict=True)
            names.update((str(exe), exe.name))
        except (OSError, RuntimeError):
            pass
    return names


def execute(card, action, value):
    result = subprocess.run(['/usr/bin/pkexec', '--disable-internal-agent', HELPER, action, card, str(value)],
                            capture_output=True, text=True, timeout=25)
    try:
        response = json.loads(result.stdout)
    except ValueError:
        raise RuntimeError(result.stderr.strip()[:300] or 'GPU controller did not respond')
    if response.get('status') != 'success':
        raise RuntimeError(response.get('message', 'GPU controller failed'))
    return response


class Watchdog:
    def __init__(self, card, spec):
        self.process = subprocess.Popen(['/usr/bin/pkexec', '--disable-internal-agent', HELPER, '--watchdog', card, json.dumps(spec)],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
        ready, _, _ = select.select([self.process.stdout], [], [], 8)
        if not ready or json.loads(self.process.stdout.readline() or '{}').get('status') != 'ready':
            self.process.stdin.close()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass  # Root watchdog exits on EOF; never kill it before recovery.
            raise RuntimeError('Safety watchdog could not start; controller update may be needed')
        self.touch()

    def touch(self):
        if self.process.poll() is None:
            self.process.stdin.write('heartbeat\n')
            self.process.stdin.flush()

    def finished(self):
        if self.process.poll() is None:
            return None
        output = self.process.stdout.read().strip()
        try:
            return json.loads(output.splitlines()[-1])
        except (ValueError, IndexError):
            return {'status': 'error', 'message': 'Safety watchdog stopped'}

    def release(self):
        try:
            self.process.stdin.write('release\n')
            self.process.stdin.flush()
            self.process.stdin.close()
            self.process.wait(timeout=3)
        except (BrokenPipeError, ValueError, subprocess.TimeoutExpired):
            pass

    def recover(self):
        try:
            self.process.stdin.close()
        except (BrokenPipeError, ValueError):
            pass
        try:
            self.process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            pass


class Controller:
    def __init__(self, config=CONFIG, execute_fn=execute, watchdog_factory=Watchdog, clock=time.monotonic):
        self.path, self._execute, self.watchdog_factory, self.clock = config, execute_fn, watchdog_factory, clock
        self.config = {'profiles': {'Default': DEFAULT.copy()}, 'gpus': {}}
        if config.exists():
            try:
                loaded = json.loads(config.read_text())
                if isinstance(loaded, dict) and isinstance(loaded.get('profiles'), dict) and isinstance(loaded.get('gpus'), dict):
                    self.config = loaded
            except (OSError, ValueError):
                pass
        self.states, self.caps = {}, {}

    def execute(self, card, action, value):
        watcher = self.states.get(card, {}).get('watchdog')
        if watcher:
            watcher.touch()
        result = self._execute(card, action, value)
        if watcher:
            watcher.touch()
        return result

    def gpu(self, card):
        import re
        if not re.fullmatch(r'nvidia[0-9]{1,3}', card):
            raise ValueError('Advanced automation currently requires NVIDIA')
        if card not in self.states:
            self.states[card] = {'current': None, 'activeProfile': '', 'curve': False, 'apps': False,
                'appActive': '', 'appRestore': None, 'timer': None, 'guard': False, 'latched': False,
                'alerts': False, 'alertHot': False, 'lastAlert': -1000, 'lastFan': None, 'lastFanAt': 0,
                'watchdog': None, 'event': '', 'temperature': None, 'sensorErrors': 0}
        self.config['gpus'].setdefault(card, {'curve': copy.deepcopy(DEFAULT_CURVE), 'rules': [], 'alertTemp': 80, 'guardTemp': 85})
        return self.states[card]

    def capabilities(self, card):
        self.gpu(card)
        if card in self.caps:
            return self.caps[card]
        data = subprocess.run(['/usr/bin/nvidia-smi', '-i', card[6:], '--query-supported-clocks=memory,graphics', '--format=csv,noheader,nounits'], capture_output=True, text=True, timeout=5)
        pairs = []
        for row in data.stdout.splitlines():
            columns = [p.strip() for p in row.split(',')]
            if len(columns) == 2 and all(p.isdigit() for p in columns):
                pairs.append([int(v) for v in columns])
        limits = subprocess.run(['/usr/bin/nvidia-smi', '-i', card[6:], '--query-gpu=power.min_limit,power.max_limit,power.default_limit', '--format=csv,noheader,nounits'], capture_output=True, text=True, timeout=5)
        values = [float(v) for v in limits.stdout.split(',')]
        if len(values) != 3 or not all(math.isfinite(v) for v in values):
            raise ValueError('Power capabilities unavailable')
        self.caps[card] = {'graphics': sorted({p[1] for p in pairs}), 'memory': sorted({p[0] for p in pairs}),
                           'powerMin': values[0], 'powerMax': values[1], 'powerDefault': values[2]}
        return self.caps[card]

    def validate_profile(self, card, profile):
        caps = self.capabilities(card)
        if not isinstance(profile, dict) or set(profile) != set(DEFAULT):
            raise ValueError('Profile must include graphics, memory, power and fan')
        for key in ('graphics', 'memory'):
            if profile[key] != 'auto' and (type(profile[key]) is not int or profile[key] not in caps[key]):
                raise ValueError('Choose a supported ' + key + ' clock')
        power = profile['power']
        if power != 'default' and (type(power) not in (int, float) or not math.isfinite(power) or not caps['powerMin'] <= power <= caps['powerMax']):
            raise ValueError('Power is outside the driver range')
        fan = profile['fan']
        if fan != 'auto' and (type(fan) is not int or not 0 <= fan <= 255):
            raise ValueError('Fan must be auto or PWM 0–255')
        return copy.deepcopy(profile)

    def apply(self, card, profile):
        profile = self.validate_profile(card, profile)
        state = self.gpu(card)
        previous = copy.deepcopy(state['current'])
        applied = []
        try:
            for key in ('fan', 'graphics', 'memory', 'power'):
                self.execute(card, ACTIONS[key], profile[key])
                applied.append(key)
        except Exception as exc:
            errors = []
            for key in reversed(applied):
                if previous:
                    try:
                        self.execute(card, ACTIONS[key], previous[key])
                    except Exception as rollback:
                        errors.append(str(rollback))
            if not previous or errors:
                state['current'] = None
            raise RuntimeError(str(exc) + ('; previous settings restored' if previous and not errors else '; state uncertain; review GPU settings'))
        state['current'] = profile

    def watch(self, card):
        state = self.gpu(card)
        spec = {}
        if state['curve']:
            spec['cooling'] = True
        if state['guard']:
            spec['thermal'] = self.config['gpus'][card]['guardTemp']
        if state['timer']:
            spec['restore'] = state['timer']['before']
            spec['seconds'] = max(0.1, state['timer']['deadline'] - self.clock())
        elif state['appRestore']:
            spec['restore'] = state['appRestore']
        old = state['watchdog']
        if not spec:
            if old:
                old.release()
            state['watchdog'] = None
            return
        fresh = self.watchdog_factory(card, spec)
        state['watchdog'] = fresh
        if old:
            old.release()

    def cancel_temporary(self, card):
        state = self.gpu(card)
        before = state['timer']['before'] if state['timer'] else state['appRestore']
        if before:
            self.apply(card, before)
        state['timer'], state['appRestore'], state['appActive'] = None, None, ''
        self.watch(card)

    def command(self, request):
        card = request.get('gpu', 'nvidia0')
        state = self.gpu(card)
        cfg = self.config['gpus'][card]
        op = request.get('op', 'status')
        if op == 'status':
            return self.status(card)
        if state['latched'] and op not in ('acknowledge', 'save-profile', 'delete-profile', 'save-rules', 'set-alert', 'set-guard'):
            raise ValueError('Thermal fallback is latched; cool the GPU and acknowledge it first')
        if op == 'save-profile':
            name = str(request.get('name', '')).strip()
            if not name or len(name) > 48 or any(ord(c) < 32 for c in name):
                raise ValueError('Use a profile name of 1–48 characters')
            if len(self.config['profiles']) >= 32 and name not in self.config['profiles']:
                raise ValueError('Maximum 32 profiles')
            profile = self.validate_profile(card, request['settings'])
            if name == 'Default' and profile != DEFAULT:
                raise ValueError('Use a different name; Default is reserved for driver defaults')
            self.config['profiles'][name] = profile
        elif op == 'delete-profile':
            name = request['name']
            if name == 'Default' or any(r['profile'] == name for c in self.config['gpus'].values() for r in c['rules']):
                raise ValueError('Default or a profile used by an app rule cannot be deleted')
            self.config['profiles'].pop(name, None)
        elif op == 'apply-profile':
            state['apps'] = False
            self.cancel_temporary(card)
            if state['curve']:
                self.execute(card, '--set-fan', 'auto')
                state['curve'] = False
                self.watch(card)
            self.apply(card, self.config['profiles'][request['name']])
            state['activeProfile'] = request['name']
        elif op == 'manual':
            key = request['key']
            if key not in ACTIONS:
                raise ValueError('Unknown manual control')
            state['apps'] = False
            self.cancel_temporary(card)
            if key == 'fan':
                state['curve'] = False
            self.watch(card)
            value = request['value']
            candidate = copy.deepcopy(state['current'] or DEFAULT)
            candidate[key] = value
            self.validate_profile(card, candidate)
            self.execute(card, ACTIONS[key], value)
            if state['current']:
                state['current'][key] = value
            state['activeProfile'] = ''
        elif op == 'legacy-profile':
            state['apps'] = False
            self.cancel_temporary(card)
            self.execute(card, '--set-power-profile', request['value'])
            state['current'] = None  # Legacy profiles may lock both clocks; establish a complete baseline explicitly.
            state['activeProfile'] = ''
        elif op == 'set-curve':
            points = validate_curve(request.get('points', cfg['curve']))
            enabled = request.get('enabled') is True
            if state['timer'] or state['apps']:
                raise ValueError('Stop the timed override or app switching before enabling a curve')
            old = state['curve']
            cfg['curve'] = points
            state['curve'] = enabled
            if enabled:
                state['lastFan'] = None
            try:
                if not enabled and old:
                    self.execute(card, '--set-fan', 'auto')
                    if state['current']:
                        state['current']['fan'] = 'auto'
                self.watch(card)
            except Exception:
                state['curve'] = old
                raise
        elif op == 'temporary':
            if not state['current']:
                raise ValueError('Apply a saved profile first to establish the configuration to restore')
            if state['curve'] or state['apps'] or state['timer']:
                raise ValueError('Stop curve/app automation or the current timer before a timed override')
            seconds = request['seconds']
            if type(seconds) not in (int, float) or not math.isfinite(seconds) or not 10 <= seconds <= 86400:
                raise ValueError('Duration must be 10 seconds to 24 hours')
            state['timer'] = {'before': copy.deepcopy(state['current']), 'deadline': self.clock() + seconds, 'profile': request['name']}
            try:
                self.watch(card)
                self.apply(card, self.config['profiles'][request['name']])
            except Exception:
                state['timer'] = None
                self.watch(card)
                raise
        elif op == 'cancel-timer':
            self.cancel_temporary(card)
        elif op == 'save-rules':
            rules = request['rules']
            if not isinstance(rules, list) or len(rules) > 20:
                raise ValueError('Maximum 20 application rules')
            for rule in rules:
                if not isinstance(rule, dict) or set(rule) != {'app', 'profile'} or not isinstance(rule['app'], str) or not rule['app'].strip() or len(rule['app']) > 256 or any(ord(c) < 32 for c in rule['app']) or rule['profile'] not in self.config['profiles']:
                    raise ValueError('Each rule needs an executable name/path and an existing profile')
            cfg['rules'] = rules
        elif op == 'set-apps':
            enabled = request.get('enabled') is True
            if enabled and (not state['current'] or state['curve'] or state['timer']):
                raise ValueError('Apply a baseline profile and stop fan curves/timers before enabling app rules')
            if not enabled:
                self.cancel_temporary(card)
            state['apps'] = enabled
        elif op in ('set-alert', 'set-guard'):
            temperature = request['temperature']
            if type(temperature) is not int or not 50 <= temperature <= 95:
                raise ValueError('Temperature must be 50–95°C')
            key = 'alertTemp' if op == 'set-alert' else 'guardTemp'
            flag = 'alerts' if op == 'set-alert' else 'guard'
            old = state[flag]
            cfg[key], state[flag] = temperature, request.get('enabled') is True
            try:
                self.watch(card)
            except Exception:
                state[flag] = old
                raise
        elif op == 'acknowledge':
            if state['temperature'] is None or state['temperature'] > cfg['guardTemp'] - 5:
                raise ValueError('Wait until temperature is at least 5°C below the fallback threshold')
            state['latched'] = False
            state['event'] = 'Fallback acknowledged; automation remains stopped'
            self.watch(card)
        else:
            raise ValueError('Unknown controller command')
        atomic_json(self.path, self.config)
        return self.status(card)

    def status(self, card):
        state = self.gpu(card)
        return {'status': 'success', 'profiles': self.config['profiles'], 'config': self.config['gpus'][card],
                'capabilities': self.capabilities(card), 'current': state['current'], 'activeProfile': state['activeProfile'],
                'curve': state['curve'], 'apps': state['apps'], 'appActive': state['appActive'],
                'timerSeconds': max(0, math.ceil(state['timer']['deadline'] - self.clock())) if state['timer'] else 0,
                'alerts': state['alerts'], 'guard': state['guard'], 'latched': state['latched'],
                'temperature': state['temperature'], 'event': state['event']}

    def tick(self, card, temperature, names):
        state = self.gpu(card)
        cfg = self.config['gpus'][card]
        state['temperature'] = temperature
        now = self.clock()
        watcher = state['watchdog']
        ended = watcher.finished() if watcher else None
        if ended is not None:
            if ended.get('thermal'):
                state['latched'] = True
                state['event'] = 'Thermal fallback: automatic cooling and minimum power applied'
                state['current'] = None
            elif state['timer']:
                state['current'] = state['timer']['before'] if ended.get('status') == 'success' else None
                state['event'] = 'Timed override ended; previous settings restored' if ended.get('status') == 'success' else 'Restore failed; review GPU settings'
            elif state['appRestore']:
                state['current'] = state['appRestore'] if ended.get('status') == 'success' else None
                state['event'] = 'Application settings restored by watchdog'
            else:
                state['event'] = 'Safety watchdog stopped; fan automation disabled'
            state.update(watchdog=None, timer=None, appRestore=None, appActive='', apps=False, curve=False)
            if ended.get('status') != 'success':
                state['event'] = 'Safety recovery reported an error; inspect GPU controls'
            self.notify(state['event'])
            if not state['latched'] and state['guard']:
                self.watch(card)
        if state['latched']:
            return
        if state['watchdog']:
            state['watchdog'].touch()
        if temperature is None:
            state['sensorErrors'] += 1
            if state['curve'] and state['sensorErrors'] >= 3:
                self.execute(card, '--set-fan', 'auto')
                state['curve'] = False
                state['event'] = 'Temperature unavailable; returned fan control to automatic'
                self.watch(card)
            return
        state['sensorErrors'] = 0
        if state['alerts']:
            if temperature >= cfg['alertTemp'] and not state['alertHot'] and now - state['lastAlert'] >= 60:
                self.notify(f'{card}: GPU temperature {temperature:g}°C (warning {cfg["alertTemp"]}°C)')
                state['alertHot'], state['lastAlert'] = True, now
            elif temperature <= cfg['alertTemp'] - 5:
                state['alertHot'] = False
        if state['timer']:
            return  # The root watchdog owns the deadline and rollback.
        if state['apps']:
            rule = matching_rule(cfg['rules'], names)
            name = rule['app'] if rule else ''
            if name != state['appActive']:
                if rule:
                    if state['appRestore'] is None:
                        state['appRestore'] = copy.deepcopy(state['current'])
                    self.watch(card)
                    self.apply(card, self.config['profiles'][rule['profile']])
                    state['appActive'] = name
                    state['event'] = 'Application profile: ' + rule['profile']
                else:
                    self.cancel_temporary(card)
                    state['event'] = 'Application exited; previous configuration restored'
        if state['curve']:
            duty = curve_duty(cfg['curve'], temperature)
            if state['lastFan'] is None or abs(duty - state['lastFan']) >= 3 and now - state['lastFanAt'] >= 5:
                self.execute(card, '--set-fan', round(duty * 255 / 100))
                state['lastFan'], state['lastFanAt'] = duty, now

    def notify(self, message):
        try:
            subprocess.Popen(['/usr/bin/notify-send', '-a', 'OmaGPU', 'OmaGPU', message], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass

    def shutdown(self):
        for state in self.states.values():
            if state['watchdog']:
                state['watchdog'].recover()


def temperatures():
    result = subprocess.run(['/usr/bin/nvidia-smi', '--query-gpu=index,temperature.gpu', '--format=csv,noheader,nounits'], capture_output=True, text=True, timeout=3)
    values = {}
    for row in result.stdout.splitlines():
        try:
            index, temp = row.split(',')
            value = float(temp)
            if math.isfinite(value):
                values['nvidia' + str(int(index))] = value
        except ValueError:
            pass
    return values


def serve():
    RUNTIME.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = (RUNTIME / 'controller.lock').open('w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return
    path = RUNTIME / 'controller.sock'
    path.unlink(missing_ok=True)
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(path)); path.chmod(0o600); server.listen(8)
    controller = Controller()
    stopping = False
    def stop(*args):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM, stop); signal.signal(signal.SIGINT, stop)
    last_tick = 0
    try:
        while not stopping:
            ready, _, _ = select.select([server], [], [], 1)
            if ready:
                connection, _ = server.accept()
                with connection:
                    connection.settimeout(2)
                    uid = struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]
                    if uid != os.getuid():
                        continue
                    try:
                        data = bytearray()
                        while b'\n' not in data and len(data) <= 65536:
                            part = connection.recv(4096)
                            if not part:
                                break
                            data.extend(part)
                        if len(data) > 65536:
                            raise ValueError('Request too large')
                        result = controller.command(json.loads(data))
                    except Exception as exc:
                        result = {'status': 'error', 'message': str(exc)[:500]}
                    try:
                        connection.sendall((json.dumps(result) + '\n').encode())
                    except OSError:
                        pass
            if time.monotonic() - last_tick >= 2 and controller.states:
                last_tick = time.monotonic()
                try:
                    temps, names = temperatures(), process_names()
                except (OSError, subprocess.SubprocessError):
                    temps, names = {}, set()
                for card in list(controller.states):
                    try:
                        controller.tick(card, temps.get(card), names)
                    except Exception as exc:
                        controller.states[card]['event'] = 'Automation paused: ' + str(exc)[:250]
                        controller.states[card]['apps'] = False
                        watcher = controller.states[card]['watchdog']
                        if watcher:
                            watcher.recover()
    finally:
        controller.shutdown()
        server.close(); path.unlink(missing_ok=True)


def client(request):
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(65)
        connection.connect(str(RUNTIME / 'controller.sock'))
        connection.sendall((json.dumps(request) + '\n').encode())
        data = bytearray()
        while b'\n' not in data and len(data) < 262144:
            part = connection.recv(8192)
            if not part:
                break
            data.extend(part)
        return json.loads(data)


if __name__ == '__main__':
    if sys.argv[1:] == ['--serve']:
        serve()
    else:
        try:
            print(json.dumps(client(json.loads(sys.argv[1]))))
        except Exception as exc:
            print(json.dumps({'status': 'error', 'message': str(exc)}))
