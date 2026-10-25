"""Calibrated sensor range to steering; no time-based steering assistance."""

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class KeyCalibration:
    index: int
    rest: float
    pressed: float
    noise: float = 0.0

    def __post_init__(self):
        if type(self.index) is not int or not 0 <= self.index < 128:
            raise ValueError("Calibration needs a sensor index from 0 through 127.")
        if not all(math.isfinite(x) for x in (self.rest, self.pressed, self.noise)):
            raise ValueError("Calibration values must be finite.")
        if self.noise < 0 or abs(self.pressed - self.rest) < max(16, self.noise * 8):
            raise ValueError("Sensor movement is too small or noisy; repeat the full-press calibration.")

    def normalize(self, delta):
        if not math.isfinite(delta):
            raise ValueError("Sensor value must be finite.")
        span = abs(self.pressed - self.rest)
        direction = 1 if self.pressed > self.rest else -1
        deadzone = max(2.0, self.noise * 3)
        movement = (delta - self.rest) * direction
        return max(0.0, min(1.0, (movement - deadzone) / (span - deadzone)))


@dataclass(frozen=True)
class SteeringCalibration:
    left: KeyCalibration
    right: KeyCalibration

    def __post_init__(self):
        if self.left.index == self.right.index:
            raise ValueError("Left and Right must use different sensors.")

    def steering(self, left_delta, right_delta):
        return self.right.normalize(right_delta) - self.left.normalize(left_delta)
