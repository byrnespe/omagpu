# Local passwordless OmaGPU controls

This setup grants user `org`, in an active local desktop session, passwordless
access to OmaGPU power-profile, fan and bounded NVIDIA power-limit controls through a bounded root-owned
helper. Other users and inactive or remote sessions retain administrator
authentication. The helper validates GPU IDs, actions, profiles, PWM values and wattage against
driver-reported power limits;
it never executes user-provided shell commands or imports the writable plugin.

The plugin routes tuning actions through `/usr/bin/pkexec` and
`/usr/local/libexec/omagpu-control`. Install these files before using tuning
controls. Telemetry does not require this helper. Hardware/driver support still
limits which tuning operations are available.

## Install

Review the three files, then run these commands from this directory as an
administrator (one-time authentication):

```sh
sudo install -d -o root -g root -m 0755 /usr/local/libexec
sudo install -o root -g root -m 0755 omagpu-control /usr/local/libexec/omagpu-control
sudo install -o root -g root -m 0644 org.omarchy.omagpu.policy /usr/share/polkit-1/actions/org.omarchy.omagpu.policy
sudo install -o root -g root -m 0644 49-omagpu.rules /etc/polkit-1/rules.d/49-omagpu.rules
```

Keep the installed helper root-owned and not writable by ordinary users. Copy
it into place; do not symlink it to the user-writable plugin checkout.
PolicyKit automatically reloads the rule. The local username is intentionally
explicit in `49-omagpu.rules`; review it before installing on another machine.

## Verify without changing GPU settings

From the active desktop session:

```sh
pkexec --disable-internal-agent /usr/local/libexec/omagpu-control --check
```

Expected: JSON reporting `status: success` and `euid: 0`, with no password prompt.
Run repository tests with `python3 -B -m unittest discover -s tests -v`.

## Revoke passwordless access

```sh
sudo rm /etc/polkit-1/rules.d/49-omagpu.rules
```

The helper remains available through normal administrator authentication.
To remove the helper and action as well, first restore the plugin's original
tuning dispatch; otherwise tuning actions will fail. Plugin updates may replace
the local dispatch change; this commit records the integration and policy files.

## Experimental controls

The experimental panel adds custom fan duty, power-limit presets and a slider,
quick power profiles, live-poll pause/rate controls, report copying, and command
progress/error feedback. Power-limit actions stay disabled until the installed
helper reports `powerLimit: true` from `--check`; installing a newer plugin
alone does not expand root privileges. The updated helper requires explicit
approval before installation. Current GPU settings are never changed on open.

## Profiles, clocks and automation

The Profiles & Clocks tab provides supported core caps, supported memory clock
locks, saved full profiles, and timed overrides. Automation provides fan curves,
per-application profiles, temperature notifications, and a latched thermal
fallback. The controller uses NVIDIA's reported frequency and power ranges;
these controls do not change voltage or apply frequency offsets.

Install the updated root-owned helper using the commands above after approving
the new privileged capabilities. `--check` then reports `advancedControls: true`.
The PolicyKit rule remains limited to this helper and the active local user.
The root watchdog accepts only a GPU identifier and validated restore/cooling/
temperature settings; it never loads the writable plugin or executes shell input.

The plugin starts one user-owned controller from the desktop session. A lock and
private Unix socket under `$XDG_RUNTIME_DIR/omagpu-automation/` prevent duplicate
controllers and reject other user IDs. Configuration lives in
`~/.config/omarchy/omagpu-controls.json` (mode 0600). Automation runs even while
the panel is closed. Profiles, app rules, curve points and thresholds persist;
all automation starts disabled when the controller process starts. No GPU
settings change just by opening the panel or installing the update.

Apply a complete saved profile before starting a timed override or app rules.
This establishes a known restore point: NVIDIA does not expose getters for
previous locked-clock ranges. External tuning performed after that point is not
tracked. A manual action stops app overrides and restores a pending timer first.
App matching uses exact executable names or absolute paths, only for processes
owned by the desktop user; the first matching rule wins. App rules, fan curves,
and timed overrides are mutually exclusive. Thermal fallback has priority over
all of them and requires acknowledgement after cooling 5°C below its threshold.

Fan curves must use increasing temperatures and nondecreasing fan percentages,
ending at 100% by 85°C. Updates have a 3-point duty deadband and 5-second minimum
interval. Three missing temperature samples restore automatic cooling. Driver
minimum fan duty still applies. A root-owned watchdog restores cooling/previous
settings on controller EOF, missed heartbeats, or timer expiry. Heartbeats come
from controller progress, not a separate keepalive thread. Thermal fallback
uses independent temperature checks inside the watchdog; it restores automatic
fans/clocks and the driver minimum power limit. It latches even if a driver
reports partial recovery, and exposes the error for review. This supplements,
and does not replace, the GPU's firmware thermal protections.

Clock and fan support remains driver-dependent; rejected operations surface as
errors. Profile application rolls back completed operations when a known prior
profile exists; without a known prior profile, failures report uncertain state.
Status notifications explain restoration failures instead of claiming success.

References: [NVIDIA clock controls](https://docs.nvidia.com/deploy/nvidia-smi/)
and [NVML device queries](https://docs.nvidia.com/deploy/nvml-api/api/group__nvmlDeviceQueries.html).
