import unittest

from main import PowerSnapshot, ProtectionEngine

AC = PowerSnapshot(True, 90, True)
BATTERY = PowerSnapshot(False, 90, True)
UNKNOWN = PowerSnapshot(None, 90, True)


class ProtectionEngineTests(unittest.TestCase):
    def test_disabled_engine_ignores_power_loss(self):
        engine = ProtectionEngine(60, AC)
        self.assertIsNone(engine.update_snapshot(BATTERY, 100.0))
        self.assertFalse(engine.countdown_active)
        self.assertFalse(engine.tick(1000.0))

    def test_countdown_starts_and_shuts_down_once(self):
        engine = ProtectionEngine(60, AC)
        engine.set_enabled(True, 0.0)
        self.assertEqual(engine.update_snapshot(BATTERY, 10.0), "power_lost")
        self.assertTrue(engine.countdown_active)
        self.assertEqual(engine.remaining(10.0), 60)
        self.assertFalse(engine.tick(69.0))
        self.assertTrue(engine.tick(70.0))
        self.assertTrue(engine.shutdown_requested)
        self.assertFalse(engine.tick(71.0))

    def test_power_restored_cancels_countdown(self):
        engine = ProtectionEngine(60, AC)
        engine.set_enabled(True, 0.0)
        engine.update_snapshot(BATTERY, 10.0)
        self.assertEqual(engine.update_snapshot(AC, 25.0), "power_restored")
        self.assertFalse(engine.countdown_active)
        self.assertFalse(engine.tick(100.0))

    def test_disable_cancels_countdown(self):
        engine = ProtectionEngine(60, AC)
        engine.set_enabled(True, 0.0)
        engine.update_snapshot(BATTERY, 10.0)
        self.assertEqual(engine.set_enabled(False, 20.0), "disabled")
        self.assertFalse(engine.countdown_active)
        self.assertFalse(engine.tick(100.0))

    def test_enabling_while_on_battery_starts_immediately(self):
        engine = ProtectionEngine(60, BATTERY)
        self.assertEqual(engine.set_enabled(True, 5.0), "power_lost")
        self.assertEqual(engine.remaining(5.0), 60)

    def test_unknown_power_does_not_cancel_countdown(self):
        engine = ProtectionEngine(60, AC)
        engine.set_enabled(True, 0.0)
        engine.update_snapshot(BATTERY, 10.0)
        self.assertIsNone(engine.update_snapshot(UNKNOWN, 20.0))
        self.assertTrue(engine.countdown_active)

    def test_custom_delay(self):
        engine = ProtectionEngine(30, AC)
        engine.set_enabled(True, 0.0)
        engine.update_snapshot(BATTERY, 5.0)
        self.assertEqual(engine.remaining(5.0), 30)
        self.assertTrue(engine.tick(35.0))


if __name__ == "__main__":
    unittest.main()
