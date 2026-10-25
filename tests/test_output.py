import ctypes
import math
import unittest
from unittest.mock import patch

from realforce_analog.output import OutputError, VJoyOutput


class Function:
    def __init__(self, implementation):
        self.implementation = implementation
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self.implementation(*args)


class FakeVJoy:
    def __init__(self, minimum=1, maximum=32768):
        self.minimum = minimum
        self.maximum = maximum
        self.status = 1
        self.enabled = True
        self.match = True
        self.axis_exists = True
        self.writes = []
        self.acquisitions = []
        self.releases = []
        self.write_results = []
        self.vJoyEnabled = Function(lambda: self.enabled)
        self.DriverMatch = Function(self.driver_match)
        self.GetVJDStatus = Function(lambda device: self.status)
        self.GetVJDAxisExist = Function(lambda device, axis: self.axis_exists)
        self.GetVJDAxisMin = Function(lambda device, axis, pointer: self.write_long(pointer, self.minimum))
        self.GetVJDAxisMax = Function(lambda device, axis, pointer: self.write_long(pointer, self.maximum))
        self.AcquireVJD = Function(self.acquire)
        self.SetAxis = Function(self.set_axis)
        self.RelinquishVJD = Function(lambda device: self.releases.append(device))

    def driver_match(self, dll, driver):
        ctypes.cast(dll, ctypes.POINTER(ctypes.c_uint16))[0] = 0x222
        ctypes.cast(driver, ctypes.POINTER(ctypes.c_uint16))[0] = 0x222 if self.match else 0x219
        return self.match

    @staticmethod
    def write_long(pointer, value):
        ctypes.cast(pointer, ctypes.POINTER(ctypes.c_int32))[0] = value
        return True

    def acquire(self, device):
        self.acquisitions.append(device)
        return True

    def set_axis(self, raw, device, axis):
        self.writes.append((raw, device, axis))
        if self.write_results:
            result = self.write_results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        return True


class OutputTests(unittest.TestCase):
    def test_axis_mapping_uses_reported_range_and_neutralizes(self):
        driver = FakeVJoy(minimum=-100, maximum=300)
        with patch("realforce_analog.output._load_vjoy_library", return_value=driver):
            with VJoyOutput(device_id=3) as output:
                self.assertTrue(output.is_open)
                for value in (-1, -0.5, 0, 0.5, 1):
                    output.set_steering(value)
            output.close()
        self.assertEqual(driver.acquisitions, [3])
        self.assertEqual(driver.releases, [3])
        self.assertEqual(driver.writes, [(v, 3, 0x30) for v in (100, -100, 0, 100, 200, 300, 100)])
        self.assertFalse(output.is_open)
        self.assertEqual(ctypes.sizeof(driver.SetAxis.argtypes[0]), 4)

    def test_failed_update_centers_and_releases(self):
        driver = FakeVJoy()
        with patch("realforce_analog.output._load_vjoy_library", return_value=driver):
            output = VJoyOutput().open()
            driver.write_results = [False, True]
            with self.assertRaises(OutputError):
                output.set_steering(1)
        self.assertEqual([value for value, _, _ in driver.writes], [16384, 32768, 16384])
        self.assertEqual(driver.releases, [1])
        self.assertFalse(output.is_open)

    def test_initial_center_failure_releases_device(self):
        driver = FakeVJoy()
        driver.write_results = [False, True]
        with patch("realforce_analog.output._load_vjoy_library", return_value=driver):
            with self.assertRaises(OutputError):
                VJoyOutput().open()
        self.assertEqual(driver.releases, [1])

    def test_release_even_when_shutdown_center_raises(self):
        driver = FakeVJoy()
        with patch("realforce_analog.output._load_vjoy_library", return_value=driver):
            output = VJoyOutput().open()
            driver.write_results = [OSError("device disconnected")]
            with self.assertRaises(OutputError):
                output.close()
            output.close()
        self.assertEqual(driver.releases, [1])

    def test_configuration_errors_do_not_acquire(self):
        cases = ("enabled", "match", "axis_exists")
        for attribute in cases:
            with self.subTest(attribute=attribute):
                driver = FakeVJoy()
                setattr(driver, attribute, False)
                with patch("realforce_analog.output._load_vjoy_library", return_value=driver):
                    with self.assertRaises(OutputError):
                        VJoyOutput().open()
                self.assertEqual(driver.acquisitions, [])
        for status in (0, 2, 3, 4, 99):
            with self.subTest(status=status):
                driver = FakeVJoy()
                driver.status = status
                with patch("realforce_analog.output._load_vjoy_library", return_value=driver):
                    with self.assertRaises(OutputError):
                        VJoyOutput().open()
                self.assertEqual(driver.acquisitions, [])

    def test_context_preserves_application_error(self):
        driver = FakeVJoy()
        with patch("realforce_analog.output._load_vjoy_library", return_value=driver):
            with self.assertRaisesRegex(RuntimeError, "application failure"):
                with VJoyOutput():
                    driver.write_results = [False]
                    raise RuntimeError("application failure")
        self.assertEqual(driver.releases, [1])

    def test_invalid_values_never_reach_driver(self):
        driver = FakeVJoy()
        with patch("realforce_analog.output._load_vjoy_library", return_value=driver):
            with VJoyOutput() as output:
                for value in (math.nan, math.inf, -1.1, 1.1):
                    with self.assertRaises(ValueError):
                        output.set_steering(value)
        self.assertEqual(len(driver.writes), 2)

    def test_device_id_validation(self):
        for value in (True, 0, 17, 1.2, "1"):
            with self.assertRaises(ValueError):
                VJoyOutput(device_id=value)


if __name__ == "__main__":
    unittest.main()
