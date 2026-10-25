"""Worker safety checks using scripted sensors and a fake virtual joystick.

These tests neither open a keyboard handle nor load a virtual joystick driver.
"""

import queue
import threading
import time
import unittest
from unittest.mock import patch

from realforce_analog.gui import Worker
from realforce_analog.model import KeyCalibration, SteeringCalibration
from realforce_analog.protocol import Sample


def worker_without_background_loop():
    worker = Worker.__new__(Worker)
    worker.commands = queue.Queue(maxsize=4)
    worker.events = queue.Queue(maxsize=64)
    worker.live = queue.Queue(maxsize=1)
    worker.stop = threading.Event()
    worker.shutdown = threading.Event()
    return worker


def sensor_sample(key, delta=0, populated=True):
    return Sample(key=key, state=0, current=1000 + delta if populated else 0,
                  baseline=1000, scale=1400, thresholds=(),
                  received_ns=time.perf_counter_ns(), round_trip_ms=1.0)


class ScriptedReader:
    def __init__(self, script):
        self.script = script
        self.calls = []
        self.opened = False
        self.closed = False
        self.info = {"product_string": "FAKE TEST KEYBOARD"}

    def __enter__(self):
        self.opened = True
        return self

    def __exit__(self, *_):
        self.closed = True

    def read(self, key):
        self.calls.append(key)
        return self.script(key, len(self.calls))


class FakeOutput:
    def __init__(self):
        self.opened = False
        self.closed = False
        self.released = False
        self.values = []
        self.had_nonzero = False
        self.emergency_neutral = threading.Event()
        self.write_error = None
        self.close_error = None

    def open(self):
        self.opened = True
        self.set_steering(0.0)
        return self

    def set_steering(self, value):
        self.values.append(value)
        if value != 0:
            self.had_nonzero = True
            if self.write_error is not None:
                raise self.write_error
        elif self.had_nonzero:
            self.emergency_neutral.set()

    def close(self):
        self.closed = True
        self.set_steering(0.0)
        self.released = True
        if self.close_error is not None:
            raise self.close_error


class WorkerSafetyTests(unittest.TestCase):
    def setUp(self):
        self.worker = worker_without_background_loop()
        self.calibration = SteeringCalibration(KeyCalibration(7, 0, 1000),
                                               KeyCalibration(9, 0, 1000))
        self.output = FakeOutput()

    def run_preview(self, reader, output_enabled=True):
        with patch("realforce_analog.gui.SensorReader", return_value=reader), \
             patch("realforce_analog.gui.VJoyOutput", return_value=self.output):
            self.worker._preview(self.calibration, output_enabled, 1)

    def assert_released_at_neutral(self):
        self.assertTrue(self.output.closed)
        self.assertTrue(self.output.released)
        self.assertEqual(self.output.values[-1], 0.0)

    def test_stop_during_left_read_does_not_read_right_or_send_partial_pair(self):
        def read(key, _):
            self.worker.stop.set()
            return sensor_sample(key, 1000)

        reader = ScriptedReader(read)
        self.run_preview(reader)
        self.assertEqual(reader.calls, [7])
        self.assertTrue(reader.closed)
        self.assertEqual(self.output.values, [0.0, 0.0])
        self.assert_released_at_neutral()

    def test_stop_during_right_read_never_emits_that_pair(self):
        def read(key, number):
            if number == 2:
                self.worker.stop.set()
            return sensor_sample(key, 1000 if key == 9 else 0)

        reader = ScriptedReader(read)
        self.run_preview(reader)
        self.assertEqual(reader.calls, [7, 9])
        self.assertEqual(self.output.values, [0.0, 0.0])
        self.assertTrue(reader.closed)
        self.assert_released_at_neutral()

    def test_error_after_nonzero_steering_centers_and_releases(self):
        failure = RuntimeError("scripted USB disconnect")

        def read(key, number):
            if number > 2:
                raise failure
            return sensor_sample(key, 1000 if key == 9 else 0)

        reader = ScriptedReader(read)
        with self.assertRaisesRegex(RuntimeError, "scripted USB disconnect"):
            self.run_preview(reader)
        self.assertTrue(reader.closed)
        self.assertEqual(self.output.values, [0.0, 1.0, 0.0])
        self.assert_released_at_neutral()

    def test_unpopulated_mapped_sensor_stops_before_output(self):
        reader = ScriptedReader(lambda key, _: sensor_sample(key, populated=key != 9))
        with self.assertRaisesRegex(RuntimeError, "populated"):
            self.run_preview(reader)
        self.assertTrue(reader.closed)
        self.assertEqual(self.output.values, [0.0, 0.0])
        self.assert_released_at_neutral()

    def test_output_write_failure_closes_both_resources_and_preserves_first_error(self):
        self.output.write_error = RuntimeError("scripted steering write failed")
        self.output.close_error = RuntimeError("scripted cleanup failed too")
        reader = ScriptedReader(lambda key, _: sensor_sample(key, 1000 if key == 9 else 0))
        with self.assertRaisesRegex(RuntimeError, "scripted steering write failed"):
            self.run_preview(reader)
        self.assertTrue(reader.closed)
        self.assert_released_at_neutral()

    def test_preview_only_does_not_acquire_virtual_controller(self):
        def read(key, number):
            if number > 2:
                self.worker.stop.set()
            return sensor_sample(key, 1000 if key == 9 else 0)

        reader = ScriptedReader(read)
        self.run_preview(reader, output_enabled=False)
        self.assertTrue(reader.closed)
        self.assertFalse(self.output.opened)
        self.assertEqual(self.output.values, [])
        frame = self.worker.live.get_nowait()
        self.assertEqual(frame["steering"], 1.0)
        self.assertFalse(frame["output"])

    def test_watchdog_centers_while_sensor_read_is_blocked_and_prevents_late_output(self):
        blocked_read = threading.Event()
        release_read = threading.Event()
        errors = []

        def read(key, number):
            if number == 3:
                blocked_read.set()
                if not release_read.wait(2):
                    raise RuntimeError("Test failed to release scripted sensor read")
            return sensor_sample(key, 1000 if key == 9 else 0)

        reader = ScriptedReader(read)

        def preview():
            try:
                self.run_preview(reader)
            except BaseException as error:
                errors.append(error)

        thread = threading.Thread(target=preview, daemon=True)
        thread.start()
        try:
            self.assertTrue(blocked_read.wait(1), "Preview never reached the blocked read")
            self.assertTrue(self.output.emergency_neutral.wait(1),
                            "Watchdog did not center output independently of the blocked read")
            self.assertTrue(self.worker.stop.is_set())
            self.assertFalse(reader.closed, "Read should remain blocked when watchdog centers")
            self.assertEqual(self.output.values, [0.0, 1.0, 0.0])
        finally:
            release_read.set()
            thread.join(timeout=1)
        self.assertFalse(thread.is_alive(), "Preview failed to stop after scripted read returned")
        self.assertEqual(errors, [])
        self.assertTrue(reader.closed)
        self.assert_released_at_neutral()
        # No nonzero write may follow the watchdog's neutral write.
        self.assertEqual(self.output.values, [0.0, 1.0, 0.0, 0.0])
        events = []
        while not self.worker.events.empty():
            events.append(self.worker.events.get_nowait())
        self.assertTrue(any(kind == "error" and "100 ms" in text for kind, text in events))

    def test_scan_excludes_empty_and_inconsistently_populated_matrix_slots(self):
        def read(key, number):
            sweep = (number - 1) // 128
            if key == 10:
                return sensor_sample(key, 100 + sweep * 10)
            if key == 30 and sweep == 0:
                return sensor_sample(key, 500)
            return sensor_sample(key, populated=False)

        reader = ScriptedReader(read)
        with patch("realforce_analog.gui.SensorReader", return_value=reader):
            self.worker._scan("idle")
        self.assertEqual(len(reader.calls), 256)
        self.assertTrue(reader.closed)
        scan = None
        while not self.worker.events.empty():
            kind, value = self.worker.events.get_nowait()
            if kind == "scan":
                scan = value
        self.assertEqual(scan, ("idle", {10: {"delta": 105.0, "noise": 10}}))

    def test_scan_stop_closes_handle_without_publishing_incomplete_calibration(self):
        def read(key, _):
            self.worker.stop.set()
            return sensor_sample(key)

        reader = ScriptedReader(read)
        with patch("realforce_analog.gui.SensorReader", return_value=reader):
            self.worker._scan("left")
        self.assertEqual(reader.calls, [0])
        self.assertTrue(reader.closed)
        events = []
        while not self.worker.events.empty():
            events.append(self.worker.events.get_nowait())
        self.assertFalse(any(kind == "scan" for kind, _ in events))


if __name__ == "__main__":
    unittest.main()
