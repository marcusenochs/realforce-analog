"""Protocol checks use fake HID devices and never enumerate real hardware."""

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import struct
import subprocess
import sys
import threading
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import Mock, patch

from realforce_analog.protocol import (
    SensorReader, TelemetryError, _ReaderGuard, _reader_guard_name,
    decode_reply, discover, request_for,
)


def sensor_reply(*, current=0x1234, baseline=0x0123, state=3, length=24):
    result = bytearray(64)
    result[:6] = bytes.fromhex("55 55 c5 00 00 18")
    result[5] = length
    struct.pack_into(
        ">BB11H", result, 6, state, 0, current, baseline, 0x3456,
        1, 0x0102, 0x1234, 0x2345, 0x3456, 0x4567, 0xABCD, 0xFFFF,
    )
    return bytes(result)


def device_row(**changes):
    result = {
        "vendor_id": 0x0853, "product_id": 0x0313,
        "interface_number": 2, "usage_page": 0xFF00,
        "product_string": "REALFORCE R3S US TKL", "path": b"fake-r3sd-path",
    }
    result.update(changes)
    return result


class FakeDevice:
    def __init__(self, replies=(), write_result=65):
        self.replies = list(replies)
        self.write_result = write_result
        self.writes = []
        self.read_calls = []
        self.opened_paths = []
        self.blocking = None
        self.close_count = 0

    def open_path(self, path):
        self.opened_paths.append(path)

    def set_nonblocking(self, enabled):
        self.blocking = not enabled

    def write(self, packet):
        self.writes.append(bytes(packet))
        if isinstance(self.write_result, Exception):
            raise self.write_result
        return self.write_result

    def read(self, length, timeout_ms):
        self.read_calls.append((length, timeout_ms))
        result = self.replies.pop(0) if self.replies else []
        if isinstance(result, Exception):
            raise result
        return result

    def close(self):
        self.close_count += 1


class PacketTests(unittest.TestCase):
    def test_only_c5_with_zero_padding_and_unnumbered_report_id(self):
        expected = bytes.fromhex("00 aa aa c5 00 00 1e") + bytes(58)
        self.assertEqual(request_for(30), expected)
        self.assertEqual(request_for(0)[6], 0)
        self.assertEqual(request_for(127)[6], 127)

    def test_invalid_indices_never_reach_transport_or_poison_good_session(self):
        device = FakeDevice([sensor_reply()])
        reader = SensorReader(device=device)
        for key in (-1, 128, 256, True, False, 1.0, "30", None):
            with self.subTest(key=key), self.assertRaises(ValueError):
                reader.read(key)
        self.assertEqual(device.writes, [])
        self.assertEqual(reader.read(30).key, 30)

    def test_big_endian_values_signed_delta_and_measurement_metadata(self):
        sample = decode_reply(sensor_reply(current=100, baseline=300), 31, 123456, 2.5)
        self.assertEqual((sample.key, sample.state), (31, 3))
        self.assertEqual((sample.current, sample.baseline, sample.delta), (100, 300, -200))
        self.assertEqual(sample.scale, 0x3456)
        self.assertEqual(sample.thresholds, (1, 258, 4660, 9029, 13398, 17767, 43981, 65535))
        self.assertEqual((sample.received_ns, sample.round_trip_ms), (123456, 2.5))
        self.assertTrue(sample.populated)
        self.assertFalse(decode_reply(sensor_reply(current=0), 127).populated)

    def test_malformed_reports_rejected(self):
        good = sensor_reply()
        wrong_header = bytearray(good)
        wrong_header[2] = 0xC4
        rejected = bytearray(good)
        rejected[3] = 1
        cases = {
            "short": good[:-1], "report-id-not-normalized": b"\0" + good,
            "wrong-command": bytes(wrong_header), "status-error": bytes(rejected),
            "short-payload": sensor_reply(length=23),
            "oversized-payload": sensor_reply(length=59),
        }
        for name, reply in cases.items():
            with self.subTest(name=name), self.assertRaises(TelemetryError):
                decode_reply(reply, 30)


class ReaderTests(unittest.TestCase):
    def test_timeout_requires_bounded_integer_before_any_transport_use(self):
        for value in (True, False, 0, -1, 1001, 1.0, "100", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                SensorReader(timeout_ms=value, device=FakeDevice())
        for value in (1, 1000):
            self.assertEqual(SensorReader(timeout_ms=value, device=FakeDevice()).timeout_ms, value)

    def test_injected_transport_does_not_create_process_guard(self):
        device = FakeDevice([sensor_reply()])
        with patch("realforce_analog.protocol._ReaderGuard") as guard:
            with SensorReader(device=device) as reader:
                reader.read(30)
        guard.assert_not_called()

    def test_each_transport_failure_permanently_stops_writes_until_reopened(self):
        cases = {
            "timeout": FakeDevice([[]]),
            "malformed": FakeDevice([b"bad"]),
            "read-error": FakeDevice([OSError("disconnected")]),
            "short-write": FakeDevice(write_result=64),
            "write-error": FakeDevice(write_result=OSError("write failed")),
        }
        for name, device in cases.items():
            with self.subTest(name=name):
                reader = SensorReader(timeout_ms=70, device=device)
                with self.assertRaises((TelemetryError, OSError)):
                    reader.read(30)
                reads_at_failure = len(device.read_calls)
                # Calling open on an existing failed handle must not permit a retry.
                reader.open()
                with self.assertRaises(TelemetryError):
                    reader.read(32)
                self.assertEqual(len(device.writes), 1)
                self.assertEqual(len(device.read_calls), reads_at_failure)
                if name in ("short-write", "write-error"):
                    self.assertEqual(device.read_calls, [])
                reader.close()

    def test_success_preserves_request_key_and_closing_prevents_more_commands(self):
        device = FakeDevice([sensor_reply(current=200), sensor_reply(current=400)])
        reader = SensorReader(timeout_ms=70, device=device)
        self.assertEqual(reader.read(30).current, 200)
        self.assertEqual(reader.read(32).current, 400)
        self.assertEqual(device.read_calls, [(64, 70), (64, 70)])
        self.assertEqual([packet[6] for packet in device.writes], [30, 32])
        reader.close()
        reader.close()
        self.assertEqual(device.close_count, 1)
        with self.assertRaises(TelemetryError):
            reader.read(30)
        self.assertEqual(len(device.writes), 2)

    def test_concurrent_calls_keep_one_request_outstanding(self):
        first_read_started = threading.Event()
        release_first_reply = threading.Event()
        second_call_started = threading.Event()

        class BlockingDevice(FakeDevice):
            def read(self, length, timeout_ms):
                if not self.read_calls:
                    first_read_started.set()
                    if not release_first_reply.wait(3):
                        raise RuntimeError("Test failed to release the first reply")
                return super().read(length, timeout_ms)

        device = BlockingDevice([sensor_reply(current=111), sensor_reply(current=222)])
        reader = SensorReader(device=device)

        def second_call():
            second_call_started.set()
            return reader.read(32)

        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(reader.read, 30)
            try:
                self.assertTrue(first_read_started.wait(3))
                second = executor.submit(second_call)
                self.assertTrue(second_call_started.wait(3))
                self.assertEqual(len(device.writes), 1)
            finally:
                release_first_reply.set()
            self.assertEqual((first.result(timeout=3).key, second.result(timeout=3).key), (30, 32))
        self.assertEqual([packet[6] for packet in device.writes], [30, 32])


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.guard = Mock()
        reservation = patch("realforce_analog.protocol._ReaderGuard", return_value=self.guard)
        self.guard_factory = reservation.start()
        self.addCleanup(reservation.stop)

    def test_discovery_limits_vid_pid_and_observed_vendor_interface(self):
        intended = device_row()
        rows = [intended, device_row(interface_number=0), device_row(usage_page=1),
                device_row(product_string="Another keyboard"), {}]
        fake_hid = SimpleNamespace(enumerate=Mock(return_value=rows))
        with patch.dict(sys.modules, {"hid": fake_hid}):
            self.assertEqual(discover(), [intended])
        fake_hid.enumerate.assert_called_once_with(0x0853, 0x0313)

    def test_missing_or_ambiguous_selection_never_opens_or_writes(self):
        for rows in ([], [device_row(), device_row(path=b"second-device")]):
            with self.subTest(count=len(rows)):
                fake_hid = SimpleNamespace(enumerate=Mock(return_value=rows), device=Mock())
                with patch.dict(sys.modules, {"hid": fake_hid}):
                    with self.assertRaises(TelemetryError):
                        SensorReader().open()
                fake_hid.device.assert_not_called()
        self.guard_factory.assert_not_called()

    def test_open_drains_old_input_then_queries_exact_selected_path(self):
        device = FakeDevice([b"old-input", [], sensor_reply()])
        fake_hid = SimpleNamespace(enumerate=Mock(return_value=[device_row()]),
                                   device=Mock(return_value=device))
        with patch.dict(sys.modules, {"hid": fake_hid}):
            with SensorReader() as reader:
                self.assertEqual(device.writes, [])
                self.assertEqual(reader.read(30).key, 30)
        self.assertEqual(device.opened_paths, [b"fake-r3sd-path"])
        self.assertTrue(device.blocking)
        self.assertEqual(device.read_calls[:2], [(64, 1), (64, 1)])
        self.assertEqual(device.close_count, 1)
        self.guard_factory.assert_called_once_with(b"fake-r3sd-path")
        self.guard.close.assert_called_once_with()

    def test_continuously_busy_input_fails_without_sending_c5(self):
        device = FakeDevice([sensor_reply()] * 32)
        fake_hid = SimpleNamespace(enumerate=Mock(return_value=[device_row()]),
                                   device=Mock(return_value=device))
        with patch.dict(sys.modules, {"hid": fake_hid}):
            with self.assertRaisesRegex(TelemetryError, "busy"):
                SensorReader().open()
        self.assertEqual(device.writes, [])
        self.assertEqual(device.close_count, 1)
        self.guard.close.assert_called_once_with()

    def test_another_process_reservation_refuses_before_opening_hid(self):
        self.guard_factory.side_effect = TelemetryError("Another reader is running")
        fake_hid = SimpleNamespace(enumerate=Mock(return_value=[device_row()]), device=Mock())
        with patch.dict(sys.modules, {"hid": fake_hid}):
            with self.assertRaisesRegex(TelemetryError, "Another reader"):
                SensorReader().open()
        fake_hid.device.assert_not_called()

    def test_open_errors_release_guard_even_when_device_cleanup_raises(self):
        for stage in ("construct", "open", "drain"):
            with self.subTest(stage=stage):
                self.guard.reset_mock()
                device = FakeDevice()
                device.close = Mock(side_effect=OSError("cleanup failed"))
                if stage == "open":
                    device.open_path = Mock(side_effect=OSError("open failed"))
                elif stage == "drain":
                    device.replies = [OSError("drain failed")]
                factory = Mock(return_value=device)
                if stage == "construct":
                    factory.side_effect = OSError("construct failed")
                fake_hid = SimpleNamespace(enumerate=Mock(return_value=[device_row()]), device=factory)
                with patch.dict(sys.modules, {"hid": fake_hid}):
                    with self.assertRaisesRegex(OSError, f"{stage} failed"):
                        SensorReader().open()
                self.guard.close.assert_called_once_with()

    def test_close_error_still_releases_guard_and_close_stays_idempotent(self):
        device = FakeDevice([[]])
        fake_hid = SimpleNamespace(enumerate=Mock(return_value=[device_row()]),
                                   device=Mock(return_value=device))
        with patch.dict(sys.modules, {"hid": fake_hid}):
            reader = SensorReader().open()
        device.close = Mock(side_effect=OSError("close failed"))
        with self.assertRaisesRegex(OSError, "close failed"):
            reader.close()
        reader.close()
        self.guard.close.assert_called_once_with()
        device.close.assert_called_once_with()
        self.assertIsNone(reader.device)


class ProcessGuardTests(unittest.TestCase):
    def test_device_name_is_case_insensitive_and_does_not_expose_path(self):
        self.assertEqual(_reader_guard_name(b"FAKE-Path"), _reader_guard_name("fake-path"))
        name = _reader_guard_name(b"fake-path")
        self.assertTrue(name.startswith("Local\\RealforceAnalog.C5."))
        self.assertNotIn("fake-path", name)
        self.assertNotEqual(name, _reader_guard_name(b"different-device"))

    def test_guard_handle_lifetime_and_existing_object_refusal(self):
        for error in (0, 183, 5):
            with self.subTest(error=error):
                api = SimpleNamespace(CreateMutexW=Mock(return_value=123 if error != 5 else None),
                                      CloseHandle=Mock(return_value=True))
                with patch("realforce_analog.protocol._mutex_api", return_value=api), \
                     patch("realforce_analog.protocol.ctypes.set_last_error", create=True), \
                     patch("realforce_analog.protocol.ctypes.get_last_error", return_value=error, create=True):
                    if error == 0:
                        guard = _ReaderGuard(b"fake-device")
                        api.CloseHandle.assert_not_called()
                        guard.close()
                        guard.close()
                        api.CloseHandle.assert_called_once_with(123)
                    else:
                        with self.assertRaisesRegex(TelemetryError, "Another|Could not reserve"):
                            _ReaderGuard(b"fake-device")
                        if error == 183:
                            api.CloseHandle.assert_called_once_with(123)
                        else:
                            api.CloseHandle.assert_not_called()
                api.CreateMutexW.assert_called_once_with(None, False, _reader_guard_name(b"fake-device"))

    @unittest.skipUnless(os.name == "nt", "Windows named object test")
    def test_named_object_blocks_a_second_process_and_releases_for_next_reader(self):
        # A synthetic path exercises only kernel object lifetime, never HID.
        path = "offline-test-" + uuid.uuid4().hex
        script = """import sys
from realforce_analog.protocol import _ReaderGuard, TelemetryError
try:
    guard = _ReaderGuard(sys.argv[1])
except TelemetryError as exc:
    if 'Another REALFORCE Analog' not in str(exc):
        raise
    print('blocked')
else:
    guard.close()
    print('acquired')
"""

        def attempt():
            result = subprocess.run(
                [sys.executable, "-B", "-c", script, path],
                cwd=Path(__file__).resolve().parents[1],
                capture_output=True, text=True, timeout=5,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout.strip()

        guard = _ReaderGuard(path)
        try:
            self.assertEqual(attempt(), "blocked")
        finally:
            guard.close()
        self.assertEqual(attempt(), "acquired")


if __name__ == "__main__":
    unittest.main()
