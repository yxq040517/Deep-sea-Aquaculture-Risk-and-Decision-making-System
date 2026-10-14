"""Events and operational lead-time boundaries used by the research interface."""

from __future__ import annotations

import unittest

from src import pipeline  # Load torch before pandas in the Windows test process.
from src.events import EventRule, forecast_lead, score_replay

import pandas as pd


class EventEvaluationTests(unittest.TestCase):
    @staticmethod
    def replay_rows() -> list[dict]:
        times = pd.date_range("2026-08-01", periods=6, freq="15min")
        return [
            {
                "timestamp": stamp.isoformat(),
                "actual_temperature_c": actual_t,
                "predicted_temperature_c": predicted_t,
                "actual_dissolved_oxygen_mg_l": actual_o,
                "predicted_dissolved_oxygen_mg_l": predicted_o,
            }
            for stamp, actual_t, predicted_t, actual_o, predicted_o in zip(
                times,
                [14, 16, 16, 14, 14, 14],
                [14, 14, 16, 14, 16, 14],
                [6, 6, 6, 6, 4, 6],
                [6, 6, 6, 6, 6, 6],
            )
        ]

    def test_missed_steps_false_episodes_and_duration_are_distinct(self) -> None:
        rule = EventRule(max_temp_c=15, min_do_mg_l=5, source="现场试验暂定限值")
        scores = score_replay(self.replay_rows(), rule, 15 * 60 * 1_000_000_000)
        temp = scores["by_variable"]["temperature"]
        self.assertEqual((temp["tp"], temp["fp"], temp["fn"]), (1, 1, 1))
        self.assertEqual(temp["precision"], .5)
        self.assertEqual(temp["recall"], .5)
        self.assertEqual(temp["false_alarm_episodes"], 1)
        self.assertEqual(temp["missed_event_episodes"], 0)
        self.assertEqual(temp["duration_bias_minutes"], 0)
        oxygen = scores["by_variable"]["oxygen"]
        self.assertEqual(oxygen["missed_event_episodes"], 1)
        self.assertEqual(oxygen["recall"], 0)
        self.assertIsNone(oxygen["precision"])
        self.assertEqual(scores["by_variable"]["combined"]["duration_bias_minutes"], -15)
        self.assertEqual(scores["source"], rule.source)

    def test_no_actual_event_is_not_perfect_recall(self) -> None:
        rule = EventRule(max_temp_c=50, source="限定情景")
        result = score_replay(self.replay_rows(), rule, 15 * 60 * 1_000_000_000)
        temp = result["by_variable"]["temperature"]
        self.assertIsNone(temp["recall"])
        self.assertIsNone(temp["precision"])
        self.assertTrue(result["warnings"])
        with self.assertRaisesRegex(ValueError, "来源"):
            EventRule(max_temp_c=19)

    def test_first_crossing_and_required_action_window(self) -> None:
        issued = pd.Timestamp("2026-08-01 12:00:00")
        future = pd.DataFrame({
            "timestamp": pd.date_range("2026-08-01 12:15:00", periods=3, freq="15min"),
            "temperature_c": [14.0, 16.0, 15.0],
            "dissolved_oxygen_mg_l": [6.0, 5.4, 4.5],
        })
        rule = EventRule(max_temp_c=15, min_do_mg_l=5, source="项目管理预设，不是生理线", action_minutes=35)
        result = forecast_lead(future, issued, rule, as_of=issued)
        self.assertEqual(result["status"], "crossing")
        self.assertEqual(result["minutes_until"], 30)
        self.assertEqual(result["triggered_variables"], ["temperature_c"])
        self.assertFalse(result["action_feasible"])
        self.assertEqual(result["first_crossing_timestamp"], "2026-08-01T12:30:00+00:00")
        self.assertFalse(result["expired"])
        expired = forecast_lead(future, issued, rule, as_of=issued + pd.Timedelta(hours=1))
        self.assertTrue(expired["expired"])
        self.assertEqual(expired["remaining_minutes_at_generation"], -30)
        self.assertFalse(expired["action_feasible"])
        no_crossing = forecast_lead(future, issued, EventRule(max_temp_c=30, source="已注明来源"), as_of=issued)
        self.assertEqual(no_crossing["status"], "no_crossing")
        self.assertIsNone(no_crossing["action_feasible"])
        self.assertIn("不代表安全", no_crossing["disclaimer"])


if __name__ == "__main__":
    unittest.main()
