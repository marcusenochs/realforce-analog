"""Stock R3S C5 telemetry. This module has no configuration/update commands.

Packet layout researched by TopreRT; see PROTOCOL.md for provenance.
"""

from dataclasses import dataclass
import ctypes
import hashlib
import os
import struct
import threading
import time


class TelemetryError(RuntimeError):
    pass


@dataclass(frozen=True)
class Sample:
    key: int
    state: int
    current: int
    baseline: int
    scale: int
    thresholds: tuple[int, ...]
    received_ns: int
    round_trip_ms: float

    @property
    def delta(self):
        return self.current - self.baseline

    @property
    def populated(self):
        return self.current != 0


def request_for(key: int) -> bytes:
    if type(key) is not int or not 0 <= key < 128:
        raise ValueError("Sensor index must be an integer from 0 through 127.")
    packet = bytearray(65)
    packet[1:4] = b"\xaa\xaa\xc5"
    packet[6] = key
    return bytes(packet)


def decode_reply(reply: bytes, key: int, received_ns=0, round_trip_ms=0.0) -> Sample:
    if len(reply) != 64:
        raise TelemetryError(f"Expected a 64-byte input report, received {len(reply)} bytes.")
    if reply[:3] != b"\x55\x55\xc5":
        raise TelemetryError(f"Unexpected telemetry header: {reply[:6].hex(' ')}")
    if reply[3] != 0:
        raise TelemetryError(f"Keyboard rejected the sensor query (status {reply[3]}).")
    length = reply[5]
    if not 24 <= length <= 58:
        raise TelemetryError(f"Invalid telemetry payload length: {length}")
    state, _reserved, current, baseline, scale, *thresholds = struct.unpack_from(
        ">BB11H", reply, 6
    )
    return Sample(key, state, current, baseline, scale, tuple(thresholds),
                  received_ns, round_trip_ms)


def discover():
    import hid
    # This project targets the observed R3SD11 US TKL PID, not another model.
    return [row for row in hid.enumerate(0x0853, 0x0313)
            if row.get("interface_number") == 2
            and row.get("usage_page") == 0xFF00
            and row.get("product_string") == "REALFORCE R3S US TKL"]


def device_summary(row):
    return {key: row.get(key) for key in (
        "vendor_id", "product_id", "manufacturer_string", "product_string",
        "interface_number", "usage_page", "usage", "release_number")}


def _reader_guard_name(path):
    if isinstance(path, bytes):
        path = path.decode("utf-8")
    if not isinstance(path, str) or not path:
        raise TelemetryError("Keyboard discovery returned an invalid device path. Reconnect USB and retry.")
    # Windows device paths are case-insensitive. Do not put device identifiers
    # directly in the named object; every process derives the same opaque name.
    digest = hashlib.sha256(path.casefold().encode("utf-8")).hexdigest()
    return "Local\\RealforceAnalog.C5." + digest


def _mutex_api():
    if os.name != "nt":
        raise TelemetryError("Exclusive keyboard sessions require Windows 10 or 11.")
    from ctypes import wintypes
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    api.CreateMutexW.restype = wintypes.HANDLE
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = wintypes.BOOL
    return api


class _ReaderGuard:
    """Keep one session-local named object alive for this device's reader.

    Object existence is the lock: CreateMutexW atomically reports an existing
    object. We never acquire thread ownership, so a different thread can close
    the reader safely. Windows also closes the handle if the process exits.
    This coordinates our app/CLI instances, not other vendors' applications.
    """

    def __init__(self, path):
        self._handle = None
        name = _reader_guard_name(path)
        self._api = _mutex_api()
        ctypes.set_last_error(0)
        handle = self._api.CreateMutexW(None, False, name)
        error = ctypes.get_last_error()
        if not handle:
            raise TelemetryError(
                f"Could not reserve the keyboard reader (Windows error {error}). "
                "Close other REALFORCE Analog windows and commands, then retry."
            )
        if error == 183:  # ERROR_ALREADY_EXISTS
            self._api.CloseHandle(handle)
            raise TelemetryError(
                "Another REALFORCE Analog window or command is reading this keyboard. "
                "Stop its calibration/preview or output, then retry here."
            )
        self._handle = handle

    def close(self):
        handle, self._handle = self._handle, None
        if handle is not None and not self._api.CloseHandle(handle):
            raise TelemetryError(
                "Could not release the keyboard reader reservation. "
                "Close this application before starting another reader."
            )


class SensorReader:
    """One outstanding request; a failed transaction invalidates the session."""

    def __init__(self, timeout_ms=100, device=None):
        if type(timeout_ms) is not int or not 1 <= timeout_ms <= 1000:
            raise ValueError("Timeout must be between 1 and 1000 ms.")
        self.timeout_ms = timeout_ms
        self.device = device
        self.info = None
        self._failed = False
        self._lock = threading.Lock()
        self._guard = None

    def open(self):
        with self._lock:
            # Injected transports are for offline tests and do not reserve a
            # Windows object or enumerate/open physical hardware.
            if self.device is not None:
                return self
            import hid
            devices = discover()
            if len(devices) != 1:
                raise TelemetryError(f"Expected one R3SD11 vendor interface, found {len(devices)}. "
                                     "Check USB connection and close REALFORCE CONNECT.")
            guard = _ReaderGuard(devices[0]["path"])
            dev = None
            try:
                dev = hid.device()
                dev.open_path(devices[0]["path"])
                dev.set_nonblocking(False)
                # Discard any old input before our first request. Never retry after a timeout.
                for _ in range(32):
                    if not dev.read(64, timeout_ms=1):
                        break
                else:
                    raise TelemetryError("Vendor interface is busy; close REALFORCE CONNECT.")
            except BaseException:
                # Preserve the original open failure while releasing both
                # resources, including if a native close itself fails.
                if dev is not None:
                    try:
                        dev.close()
                    except Exception:
                        pass
                try:
                    guard.close()
                except Exception:
                    pass
                raise
            self.device = dev
            self._guard = guard
            self.info = device_summary(devices[0])
            self._failed = False
            return self

    def read(self, key):
        packet = request_for(key)
        with self._lock:
            if self.device is None or self._failed:
                raise TelemetryError("Telemetry session is closed or failed; reopen to continue.")
            start = time.perf_counter_ns()
            try:
                written = self.device.write(packet)
                if written != len(packet):
                    raise TelemetryError(f"Incomplete C5 request: {written}/{len(packet)} bytes.")
                reply = bytes(self.device.read(64, timeout_ms=self.timeout_ms))
                end = time.perf_counter_ns()
                if not reply:
                    raise TelemetryError(f"Sensor {key} timed out after {self.timeout_ms} ms. "
                                         "No further commands were sent; close REALFORCE CONNECT and retry.")
                return decode_reply(reply, key, end, (end - start) / 1e6)
            except Exception:
                self._failed = True
                raise

    def close(self):
        with self._lock:
            dev, self.device = self.device, None
            guard, self._guard = self._guard, None
            try:
                if dev is not None:
                    dev.close()
            finally:
                if guard is not None:
                    guard.close()

    def __enter__(self):
        return self.open()

    def __exit__(self, *_):
        self.close()
