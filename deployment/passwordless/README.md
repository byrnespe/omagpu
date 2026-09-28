# Local passwordless OmaGPU controls

This setup grants user `org`, in an active local desktop session, passwordless
access to OmaGPU power-profile and fan controls through a bounded root-owned
helper. Other users and inactive or remote sessions retain administrator
authentication. The helper validates GPU IDs, actions, profiles and PWM values;
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
