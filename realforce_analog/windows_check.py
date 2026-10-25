"""Read back optional vJoy output through Windows' independent joystick API."""

import ctypes as c
import time
from .output import VJoyOutput


class _Caps(c.Structure):
    _fields_ = [("mid", c.c_uint16), ("pid", c.c_uint16), ("name", c.c_wchar * 32)] + [
        (name, c.c_uint32) for name in (
            "xmin", "xmax", "ymin", "ymax", "zmin", "zmax", "buttons", "period_min",
            "period_max", "rmin", "rmax", "umin", "umax", "vmin", "vmax", "caps",
            "maxaxes", "numaxes", "maxbuttons")
    ] + [("regkey", c.c_wchar * 32), ("oem", c.c_wchar * 260)]


class _Position(c.Structure):
    _fields_ = [(name, c.c_uint32) for name in (
        "size", "flags", "x", "y", "z", "r", "u", "v", "buttons", "button_number",
        "pov", "reserved1", "reserved2")]


def verify_output():
    winmm = c.WinDLL("winmm")
    winmm.joyGetDevCapsW.argtypes = [c.c_size_t, c.POINTER(_Caps), c.c_uint32]
    winmm.joyGetDevCapsW.restype = c.c_uint32
    winmm.joyGetPosEx.argtypes = [c.c_uint32, c.POINTER(_Position)]
    winmm.joyGetPosEx.restype = c.c_uint32
    devices = []
    for index in range(winmm.joyGetNumDevs()):
        caps = _Caps()
        if winmm.joyGetDevCapsW(index, c.byref(caps), c.sizeof(caps)) == 0:
            # WinMM may expose the generic Microsoft driver name instead of
            # the friendly vJoy label. Match the observed vJoy VID/PID too.
            if "vjoy" in caps.name.lower() or (caps.mid, caps.pid) == (0x1234, 0xBEAD):
                devices.append({"index": index, "name": caps.name,
                                "manufacturer_id": caps.mid, "product_id": caps.pid,
                                "xmin": caps.xmin, "xmax": caps.xmax})
    if len(devices) != 1:
        raise RuntimeError(f"Expected one Windows vJoy joystick, found {len(devices)}.")
    device = devices[0]
    checks = []
    with VJoyOutput() as output:
        for steering in (-1.0, -.5, 0.0, .5, 1.0, 0.0):
            output.set_steering(steering)
            time.sleep(.03)
            position = _Position()
            position.size = c.sizeof(position)
            position.flags = 1  # JOY_RETURNX
            status = winmm.joyGetPosEx(device["index"], c.byref(position))
            if status != 0:
                raise RuntimeError(f"Windows joystick readback failed: {status}")
            span = device["xmax"] - device["xmin"]
            expected = device["xmin"] + (steering + 1) * .5 * span
            checks.append({"requested": steering, "windows_x": position.x,
                           "expected_windows_x": expected})
            if abs(position.x - expected) > max(2, span * .01):
                raise RuntimeError(f"vJoy readback mismatch: {checks[-1]}")
    return {"device": device, "checks": checks, "success": True}
