"""Optional virtual joystick output, with no driver installation or extra packages.

The vJoy interface is a cdecl DLL. Its LONG/BOOL/UINT types are explicitly
32-bit here, including when these classes are tested on a non-Windows host.
"""

from __future__ import annotations

import ctypes
import math
import os
from pathlib import Path
import sys
import threading


class OutputError(RuntimeError):
    """The virtual joystick could not be opened or updated safely."""


_BOOL = ctypes.c_int32
_LONG = ctypes.c_int32
_UINT = ctypes.c_uint32
_WORD = ctypes.c_uint16
_HID_USAGE_X = 0x30
_VJD_FREE = 1


def _checked_steering(value: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Steering must be a finite number between -1 and 1.") from exc
    if not math.isfinite(result) or not -1.0 <= result <= 1.0:
        raise ValueError("Steering must be a finite number between -1 and 1.")
    return result


def _find_vjoy_dll(dll_path: str | os.PathLike[str] | None) -> Path:
    if dll_path is not None:
        path = Path(dll_path).expanduser().resolve()
        if not path.is_file():
            raise OutputError(f"vJoy interface DLL does not exist: {path}")
        return path

    configured = os.environ.get("VJOY_DLL_PATH")
    if configured:
        return _find_vjoy_dll(configured)

    architecture = "x64" if ctypes.sizeof(ctypes.c_void_p) == 8 else "x86"
    roots: list[Path] = []
    for name in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"):
        root = os.environ.get(name)
        if root and Path(root) not in roots:
            roots.append(Path(root))
    for root in roots:
        for path in (
            root / "vJoy" / architecture / "vJoyInterface.dll",
            root / "vJoy" / "vJoyInterface.dll",
        ):
            if path.is_file():
                return path.resolve()
    raise OutputError(
        "vJoyInterface.dll was not found. Install the signed vJoy driver for "
        "Windows 10/11, enable device 1 with its X axis in Configure vJoy, "
        "and restart this utility. For a custom install, supply dll_path or "
        "set VJOY_DLL_PATH to the matching 32/64-bit vJoyInterface.dll."
    )


def _load_vjoy_library(dll_path: str | os.PathLike[str] | None):
    if sys.platform != "win32":
        raise OutputError("vJoy output requires Windows.")
    path = _find_vjoy_dll(dll_path)
    try:
        # vJoy's public functions use __cdecl, so CDLL is intentional.
        return ctypes.CDLL(str(path))
    except OSError as exc:
        raise OutputError(
            f"Could not load {path}. Use a DLL matching this Python process's "
            "32/64-bit architecture and the installed vJoy driver. "
            f"Windows reported: {exc}"
        ) from exc


def _bind_vjoy_library(library) -> None:
    signatures = {
        "vJoyEnabled": ([], _BOOL),
        "DriverMatch": ([ctypes.POINTER(_WORD), ctypes.POINTER(_WORD)], _BOOL),
        "GetVJDStatus": ([_UINT], ctypes.c_int),
        "GetVJDAxisExist": ([_UINT, _UINT], _BOOL),
        "GetVJDAxisMin": ([_UINT, _UINT, ctypes.POINTER(_LONG)], _BOOL),
        "GetVJDAxisMax": ([_UINT, _UINT, ctypes.POINTER(_LONG)], _BOOL),
        "AcquireVJD": ([_UINT], _BOOL),
        "SetAxis": ([_LONG, _UINT, _UINT], _BOOL),
        "RelinquishVJD": ([_UINT], None),
    }
    try:
        for name, (arguments, result) in signatures.items():
            function = getattr(library, name)
            function.argtypes = arguments
            function.restype = result
    except AttributeError as exc:
        raise OutputError(
            "The selected DLL is missing a required vJoy interface function. "
            "Use vJoyInterface.dll supplied with the installed vJoy driver."
        ) from exc


class VJoyOutput:
    """Own one vJoy device and drive its X axis from -1 (left) to 1 (right).

    Call close(), or use a context manager, to center and release the device.
    A failed write also attempts to center the device before releasing it.
    """

    def __init__(
        self,
        device_id: int = 1,
        dll_path: str | os.PathLike[str] | None = None,
    ) -> None:
        if isinstance(device_id, bool) or not isinstance(device_id, int) or not 1 <= device_id <= 16:
            raise ValueError("vJoy device_id must be an integer between 1 and 16.")
        self.device_id = device_id
        self.dll_path = dll_path
        self._library = None
        self._acquired = False
        self._minimum: int | None = None
        self._maximum: int | None = None
        self._center: int | None = None
        self._lock = threading.RLock()

    @property
    def is_open(self) -> bool:
        return self._acquired

    def open(self) -> VJoyOutput:
        with self._lock:
            if self._acquired:
                return self
            library = _load_vjoy_library(self.dll_path)
            _bind_vjoy_library(library)
            if not library.vJoyEnabled():
                raise OutputError(
                    "The vJoy driver is not enabled. Enable it in Configure vJoy "
                    "or install the signed Windows 10/11 driver, then restart this utility."
                )
            dll_version, driver_version = _WORD(), _WORD()
            if not library.DriverMatch(ctypes.byref(dll_version), ctypes.byref(driver_version)):
                raise OutputError(
                    "The vJoy DLL and driver versions do not match "
                    f"(DLL 0x{dll_version.value:04X}, driver 0x{driver_version.value:04X}). "
                    "Select the DLL from the installed vJoy driver."
                )
            status = library.GetVJDStatus(self.device_id)
            if status != _VJD_FREE:
                reasons = {
                    0: "is already owned by another output in this process",
                    2: "is in use by another application",
                    3: "is missing or disabled",
                    4: "has an unknown driver status",
                }
                raise OutputError(
                    f"vJoy device {self.device_id} {reasons.get(status, f'has status {status}')}. "
                    "Close other joystick feeders, or enable/select a free device in Configure vJoy."
                )
            if not library.GetVJDAxisExist(self.device_id, _HID_USAGE_X):
                raise OutputError(
                    f"vJoy device {self.device_id} has no X axis. "
                    "Enable its X axis in Configure vJoy."
                )
            minimum, maximum = _LONG(), _LONG()
            if not library.GetVJDAxisMin(self.device_id, _HID_USAGE_X, ctypes.byref(minimum)):
                raise OutputError("Could not query the vJoy X-axis minimum. Reconfigure or restart vJoy.")
            if not library.GetVJDAxisMax(self.device_id, _HID_USAGE_X, ctypes.byref(maximum)):
                raise OutputError("Could not query the vJoy X-axis maximum. Reconfigure or restart vJoy.")
            if minimum.value >= maximum.value:
                raise OutputError("The vJoy driver returned an invalid X-axis range.")
            if not library.AcquireVJD(self.device_id):
                raise OutputError(
                    f"Could not acquire vJoy device {self.device_id}. "
                    "Close other joystick feeders and try again."
                )
            self._library = library
            self._acquired = True
            self._minimum, self._maximum = minimum.value, maximum.value
            self._center = round((minimum.value + maximum.value) / 2)
            try:
                if not library.SetAxis(self._center, self.device_id, _HID_USAGE_X):
                    raise OutputError("vJoy refused the initial neutral steering value.")
            except Exception as exc:
                self._close_locked(suppress_errors=True)
                if isinstance(exc, OutputError):
                    raise
                raise OutputError(f"Could not center the vJoy device: {exc}") from exc
            return self

    def set_steering(self, value: float) -> None:
        value = _checked_steering(value)
        with self._lock:
            if not self._acquired or self._library is None:
                raise OutputError("vJoy output is not open. Open it before sending steering.")
            assert self._minimum is not None and self._maximum is not None and self._center is not None
            # Map each half independently so zero always uses the exact neutral
            # value, including asymmetric or odd-sized driver ranges.
            if value < 0:
                raw = round(self._center + value * (self._center - self._minimum))
            else:
                raw = round(self._center + value * (self._maximum - self._center))
            try:
                if not self._library.SetAxis(raw, self.device_id, _HID_USAGE_X):
                    raise OutputError("vJoy refused a steering update; output has been released.")
            except Exception as exc:
                self._close_locked(suppress_errors=True)
                if isinstance(exc, OutputError):
                    raise
                raise OutputError(f"vJoy steering update failed; output has been released: {exc}") from exc

    def _close_locked(self, *, suppress_errors: bool) -> None:
        library = self._library
        acquired = self._acquired
        center = self._center
        # Forget ownership even if a driver call raises, keeping close idempotent.
        self._library = None
        self._acquired = False
        self._minimum = self._maximum = self._center = None
        if library is None or not acquired:
            return
        error: Exception | None = None
        try:
            if center is not None and not library.SetAxis(center, self.device_id, _HID_USAGE_X):
                error = OutputError("vJoy refused neutral steering during shutdown.")
        except Exception as exc:
            error = exc
        finally:
            try:
                library.RelinquishVJD(self.device_id)
            except Exception as exc:
                if error is None:
                    error = exc
        if error is not None and not suppress_errors:
            raise OutputError(
                f"Could not fully reset/release vJoy device {self.device_id}: {error}"
            ) from error

    def close(self) -> None:
        with self._lock:
            self._close_locked(suppress_errors=False)

    def __enter__(self) -> VJoyOutput:
        return self.open()

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        with self._lock:
            # Preserve the application's original failure while still attempting
            # to center and relinquish the virtual controller.
            self._close_locked(suppress_errors=exc_type is not None)
