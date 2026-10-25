# R3SD11 sensor protocol

The application reads stock firmware telemetry through the keyboard's vendor HID
interface. It contains no firmware-update or keyboard-configuration commands.

Tested device: **REALFORCE R3SD11**, firmware reported as **0.12**.

| USB field | Value |
|---|---|
| Vendor / product ID | `0853:0313` |
| Interface | `2` |
| Usage page / usage | `FF00:0001` |
| Product string | `REALFORCE R3S US TKL` |

## C5 sensor query

- Output: report ID `00`, then a 64-byte command:
  `AA AA C5 00 00 index`, padded with zeroes.
- Input, after hidapi removes the report ID:
  `55 55 C5 status reserved length payload`.
- Successful status is `00`; the payload contains at least 24 bytes.
- Payload: state byte, reserved byte, then 11 big-endian unsigned 16-bit values:
  current, baseline, scale and eight thresholds.
- The application uses `current - baseline` as the sensor signal. Saved rest and
  full-press readings normalize this signal; they do not establish millimeters of travel.
- Sensor indices are restricted to `0..127`. Unpopulated slots can return current
  `0` and are excluded from mapping. Physical calibration on the tested keyboard
  identified Left as `78` and Right as `88`; new installations still calibrate.

Replies contain no key echo or sequence number. Queries remain sequential, with
one reader holding the vendor interface. A timeout or malformed reply ends the
session; it is not blindly retried. Steering output centers on stop/error, with
an additional watchdog for missing sensor pairs.

Measured request throughput and revisit intervals describe host communication,
not fresh sensor scan rate or full key-to-screen latency.

## Research reference

The packet format was published by xvlqwz and rocketman2654 in
[TopreRT's C5 probe at commit `692998e`](https://github.com/rocketman2654/TopreRT/blob/692998e7748ffe7a726f50ca12b3c6da5af4be7e/backends/r3s/r3sb_c5_live_probe_v5.py).
This custom utility uses those protocol facts; it includes no TopreRT firmware,
patcher or recovery implementation.
