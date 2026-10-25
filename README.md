# Realforce to vJoy

A Windows bridge from stock REALFORCE key-depth telemetry, through calibration,
to a vJoy joystick axis. It reads sensor status without changing keyboard
firmware or settings.

**Current scope: R3SD11, firmware 0.12.** Left/Right arrows control the vJoy
**X axis**. Arbitrary remapping and other models are unsupported.

## Setup

1. Install Python 3.11 or newer, with **Add Python to PATH** enabled. Run
   `Setup.cmd` once.
2. Install the signed [vJoy 2.2.2.0 Windows 10/11 driver](https://github.com/BrunnerInnovation/vJoy/releases/tag/v2.2.2.0).
   In **Configure vJoy**, enable device **1** with its **X axis**.
3. Connect the keyboard by USB and run `Start-Analog.cmd`.

## Calibrate and use

1. Release every key and click **Scan idle**.
2. Hold Left fully down. Click **Hold ← and map** using the mouse, keeping the
   arrow held until the scan finishes. Release it and repeat for Right.
3. Click **Start preview** and check that partial presses produce partial output.
   Calibration saves automatically.
4. Click **Stop**, then **Start vJoy output**. In the receiving application,
   bind the selected vJoy device's **X axis** to the desired analog control.

**Stop** centers and releases the axis. Keep the utility running during use.
Close REALFORCE CONNECT if keyboard access is busy.

## Verified example and performance

The tested keyboard works with [Trackmania analog steering](docs/trackmania.md).
Measured throughput: about **75 host responses/second per arrow**,
not end-to-end input latency.

## Development

Run tests without accessing hardware:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Calibration (`settings.local.json`), reports and dependencies stay local.
See [PROTOCOL.md](PROTOCOL.md) for packet details and the
[TopreRT](https://github.com/rocketman2654/TopreRT) research reference.
