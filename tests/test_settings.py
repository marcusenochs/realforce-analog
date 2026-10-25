"""Loading settings needs neither a Tk window nor a keyboard handle."""

from dataclasses import asdict
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from realforce_analog.gui import App


class SettingsTests(unittest.TestCase):
    def load(self, contents):
        app = SimpleNamespace(calibration={}, status=Mock(), left_info=Mock(), right_info=Mock())
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "settings.local.json"
            path.write_text(contents, encoding="utf-8")
            with patch("realforce_analog.gui.SETTINGS", path):
                App._load(app)
        return app

    def test_invalid_top_level_json_is_reported_without_crashing(self):
        for value in (None, [], 1, "calibration"):
            with self.subTest(value=value):
                app = self.load(json.dumps(value))
                self.assertEqual(app.calibration, {})
                self.assertIn("could not be loaded", app.status.set.call_args.args[0])

    def test_incomplete_or_corrupt_settings_do_not_install_a_partial_mapping(self):
        for contents in ("{", "{}", '{"version": 2}', '{"version": 1, "left": {}}'):
            with self.subTest(contents=contents):
                app = self.load(contents)
                self.assertEqual(app.calibration, {})
                self.assertIn("could not be loaded", app.status.set.call_args.args[0])

    def test_valid_calibration_loads_both_arrows(self):
        saved = {"version": 1,
                 "left": {"index": 78, "rest": 5, "pressed": 2000, "noise": 4},
                 "right": {"index": 88, "rest": 6, "pressed": 1900, "noise": 8}}
        app = self.load(json.dumps(saved))
        self.assertEqual(asdict(app.calibration["left"]), saved["left"])
        self.assertEqual(asdict(app.calibration["right"]), saved["right"])
