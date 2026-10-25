# Realforce Analog

A Windows utility that turns the REALFORCE R3SD11's Left/Right arrow depth into
analog steering for Trackmania. Up stays acceleration; Space stays brake.
It reads stock sensor telemetry without changing keyboard firmware or settings.

## Setup

1. Install Python 3.11 or newer, with **Add Python to PATH** enabled. Run
   `Setup.cmd` once.
2. Install the signed [vJoy 2.2.2.0 Windows 10/11 driver](https://github.com/BrunnerInnovation/vJoy/releases/tag/v2.2.2.0).
   In **Configure vJoy**, enable device **1** with its **X axis**.
3. Connect the keyboard by USB and run `Start-Analog.cmd`.

## Calibrate and play

1. Release every key and click **Scan idle**.
2. Hold Left fully down. Click **Hold ← and map** using the mouse, keeping the
   arrow held until the scan finishes. Release it and repeat for Right.
3. Click **Start preview** and check that partial presses produce partial steering.
   Calibration saves automatically.
4. Click **Stop**, then **Start vJoy steering**.
5. In Trackmania, bind steering to the **vJoy X axis**. Remove the keyboard
   Left/Right steering bindings. Keep **Up = acceleration**, **Space = brake**.

Use **Stop** before closing or recalibrating; it centers and releases the axis.
Close REALFORCE CONNECT if keyboard access is busy.

## Compatibility and development

Tested with one **R3SD11, firmware 0.12**, on Windows, including working
Trackmania steering. Other models are unsupported. Measured throughput was about
**75 host responses/second per arrow**; this is not end-to-end input latency.

Run tests without accessing hardware:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Calibration (`settings.local.json`), reports and dependencies stay local.
See [PROTOCOL.md](PROTOCOL.md) for packet details and the
[TopreRT](https://github.com/rocketman2654/TopreRT) research reference.
