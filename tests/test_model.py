import unittest
from realforce_analog.model import KeyCalibration, SteeringCalibration


class CalibrationTests(unittest.TestCase):
    def test_noise_deadzone_fullrange_and_combination(self):
        left = KeyCalibration(1, 20, 2020, 3)
        right = KeyCalibration(2, 0, 1000, 2)
        cal = SteeringCalibration(left, right)
        self.assertEqual(left.normalize(22), 0)
        self.assertEqual(left.normalize(2020), 1)
        self.assertEqual(cal.steering(2020, 0), -1)
        self.assertEqual(cal.steering(20, 1000), 1)
        self.assertEqual(cal.steering(2020, 1000), 0)
        self.assertEqual(cal.steering(-1000, 10000), 1)

    def test_reverse_direction_and_invalid_calibration(self):
        self.assertEqual(KeyCalibration(1, 1000, 0).normalize(0), 1)
        for values in ((128, 0, 1000, 0), (1, 0, 0, 0), (1, 0, 100, 20),
                       (1, 0, float('nan'), 0)):
            with self.assertRaises(ValueError):
                KeyCalibration(*values)
        with self.assertRaises(ValueError):
            SteeringCalibration(KeyCalibration(1, 0, 100), KeyCalibration(1, 0, 100))
