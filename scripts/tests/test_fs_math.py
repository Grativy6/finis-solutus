from __future__ import annotations

import json
import subprocess
import sys
import unittest
from decimal import Decimal
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from scripts.fs_math import (  # noqa: E402
    ImplementationLimitError,
    advance_exp,
    advance_time,
    apply_fatigue_cost,
    base_load_limit,
    calculate_maxima,
    decimal_string,
    format_campaign_time,
    level_threshold,
    practical_load_limit,
    recover_fatigue,
    to_decimal,
)


class DerivedResourceTests(unittest.TestCase):
    def test_neutral_maxima(self) -> None:
        maxima = calculate_maxima("15", "15", "15", "15")
        self.assertEqual(maxima.maximum_hp, Decimal("135"))
        self.assertEqual(maxima.maximum_rp, Decimal("150"))
        self.assertEqual(maxima.maximum_fatigue, Decimal("105"))

    def test_decimal_inputs_remain_exact(self) -> None:
        maxima = calculate_maxima("12.5", "15.25", "9.75", "20.125")
        self.assertEqual(maxima.maximum_hp, Decimal("107.00"))
        self.assertEqual(maxima.maximum_rp, Decimal("201.250"))
        self.assertEqual(maxima.maximum_fatigue, Decimal("82.00"))

    def test_negative_stat_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "nonnegative"):
            calculate_maxima("-1", "15", "15", "15")

    def test_accepted_large_values_do_not_round_at_default_context_precision(self) -> None:
        strength = "9" * 100
        maxima = calculate_maxima(strength, "0", "0", "0")
        expected = Decimal(str(int(strength) * 3))
        self.assertEqual(maxima.maximum_hp, expected)

    def test_float_bool_and_nonfinite_inputs_are_rejected(self) -> None:
        with self.assertRaisesRegex(TypeError, "float"):
            to_decimal(0.1)
        with self.assertRaisesRegex(TypeError, "boolean"):
            to_decimal(True)
        for value in (Decimal("NaN"), Decimal("Infinity"), "-Infinity"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "finite"):
                to_decimal(value)

    def test_decimal_size_bounds_are_explicit_implementation_limits(self) -> None:
        with self.assertRaisesRegex(ImplementationLimitError, "digit"):
            to_decimal("9" * 129)
        with self.assertRaisesRegex(ImplementationLimitError, "exponent"):
            to_decimal("1e129")

    def test_canonical_decimal_string_removes_cosmetic_zeros(self) -> None:
        self.assertEqual(decimal_string(Decimal("0.000")), "0")
        self.assertEqual(decimal_string(Decimal("-0")), "0")
        self.assertEqual(decimal_string(Decimal("107.00")), "107")
        self.assertEqual(decimal_string(Decimal("201.2500")), "201.25")


class ExperienceTests(unittest.TestCase):
    def test_threshold_formula(self) -> None:
        self.assertEqual(level_threshold(1), Decimal("100"))
        self.assertEqual(level_threshold(8), Decimal("170"))

    def test_civilization_scale_example(self) -> None:
        result = advance_exp(1, "0", "1000")
        self.assertEqual(result.level, 8)
        self.assertEqual(result.exp, Decimal("90"))
        self.assertEqual(result.current_threshold, Decimal("170"))
        self.assertEqual(result.levels_gained, 7)
        self.assertEqual(result.player_points_granted, 14)
        self.assertEqual(result.earned_points_granted, 7)
        self.assertEqual(
            result.thresholds_crossed,
            tuple(Decimal(str(value)) for value in range(100, 170, 10)),
        )

    def test_fractional_carry_is_exact(self) -> None:
        result = advance_exp(1, "99.9", "0.1")
        self.assertEqual(result.level, 2)
        self.assertEqual(result.exp, Decimal("0.0"))
        self.assertEqual(result.current_threshold, Decimal("110"))

    def test_starting_exp_must_already_be_normalized(self) -> None:
        for current_exp in ("100", "100.5"):
            with self.subTest(current_exp=current_exp), self.assertRaisesRegex(
                ValueError, "less than the current Level threshold"
            ):
                advance_exp(1, current_exp, "0")

    def test_extreme_crossing_count_fails_as_implementation_not_gameplay_limit(self) -> None:
        crossings = 10_001
        award = crossings * 100 + 5 * crossings * (crossings - 1)
        with self.assertRaisesRegex(
            ImplementationLimitError, "not a gameplay Level cap"
        ):
            advance_exp(1, "0", str(award))

    def test_invalid_level_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "positive integer"):
            advance_exp("1.5", "0", "10")


class FatigueTests(unittest.TestCase):
    def test_cost_uses_headroom_then_overexertion(self) -> None:
        result = apply_fatigue_cost("90", "100", "15", "2", "10")
        self.assertEqual(result.available_headroom, Decimal("10"))
        self.assertEqual(result.overflow, Decimal("5"))
        self.assertEqual(result.fatigue, Decimal("100"))
        self.assertEqual(result.hp_lost, Decimal("1"))
        self.assertEqual(result.assessed_hp_loss, Decimal("1"))
        self.assertEqual(result.applied_hp_loss, Decimal("1"))
        self.assertEqual(result.remainder, Decimal("3"))
        self.assertEqual(result.hp, Decimal("9"))
        self.assertFalse(result.over_cap)

    def test_assessed_and_applied_hp_loss_are_distinct(self) -> None:
        result = apply_fatigue_cost("100", "100", "20", "0", "1")
        self.assertEqual(result.assessed_hp_loss, Decimal("5"))
        self.assertEqual(result.applied_hp_loss, Decimal("1"))
        self.assertEqual(result.hp_lost, Decimal("5"))
        self.assertEqual(result.hp, Decimal("0"))

    def test_applied_hp_loss_is_unknown_without_current_hp(self) -> None:
        result = apply_fatigue_cost("100", "100", "4")
        self.assertEqual(result.assessed_hp_loss, Decimal("1"))
        self.assertIsNone(result.applied_hp_loss)
        self.assertIsNone(result.hp)

    def test_existing_over_cap_fatigue_is_preserved(self) -> None:
        result = apply_fatigue_cost("110", "100", "2.5", "0")
        self.assertEqual(result.available_headroom, Decimal("0"))
        self.assertEqual(result.overflow, Decimal("2.5"))
        self.assertEqual(result.fatigue, Decimal("110"))
        self.assertEqual(result.hp_lost, Decimal("0"))
        self.assertEqual(result.remainder, Decimal("2.5"))
        self.assertTrue(result.over_cap)

    def test_zero_maximum_fatigue_is_valid(self) -> None:
        cost = apply_fatigue_cost("0", "0", "4", "0", "10")
        self.assertEqual(cost.fatigue, Decimal("0"))
        self.assertEqual(cost.overflow, Decimal("4"))
        self.assertEqual(cost.applied_hp_loss, Decimal("1"))
        self.assertFalse(cost.over_cap)

        recovery = recover_fatigue("1", "0", "0", "0")
        self.assertTrue(recovery.over_cap)

    def test_recovery_clears_remainder_before_fatigue(self) -> None:
        result = recover_fatigue("100", "100", "6", "3")
        self.assertEqual(result.remainder_recovered, Decimal("3"))
        self.assertEqual(result.remainder, Decimal("0"))
        self.assertEqual(result.fatigue_recovered, Decimal("3"))
        self.assertEqual(result.fatigue, Decimal("97"))
        self.assertEqual(result.unused_recovery, Decimal("0"))

    def test_fractional_recovery_is_exact(self) -> None:
        result = recover_fatigue("10", "100", "0.5", "0.25")
        self.assertEqual(result.remainder, Decimal("0"))
        self.assertEqual(result.fatigue, Decimal("9.75"))

    def test_remainder_invariant_is_enforced(self) -> None:
        with self.assertRaisesRegex(ValueError, "less than 4"):
            apply_fatigue_cost("10", "100", "1", "4")


class CampaignTimeTests(unittest.TestCase):
    def test_time_advancement_and_day_rollover(self) -> None:
        total = advance_time("86399.5", "1")
        self.assertEqual(total, Decimal("86400.5"))
        self.assertEqual(format_campaign_time(total), "Day 2 · 12:00:00.5 AM")

    def test_midnight_noon_and_evening(self) -> None:
        self.assertEqual(format_campaign_time("0"), "Day 1 · 12:00 AM")
        self.assertEqual(format_campaign_time("43200"), "Day 1 · 12:00 PM")
        self.assertEqual(format_campaign_time("65580"), "Day 1 · 6:13 PM")

    def test_seconds_can_be_forced_for_second_sensitive_play(self) -> None:
        self.assertEqual(
            format_campaign_time("3600", include_seconds=True),
            "Day 1 · 1:00:00 AM",
        )

    def test_time_cannot_advance_by_negative_duration(self) -> None:
        with self.assertRaisesRegex(ValueError, "nonnegative"):
            advance_time("100", "-1")


class LoadLimitTests(unittest.TestCase):
    def test_piecewise_base_load_limit(self) -> None:
        expected = {
            "0": "0",
            "20": "20",
            "30": "25.0",
            "40": "30.0",
            "1000": "30",
        }
        for strength, limit in expected.items():
            with self.subTest(strength=strength):
                self.assertEqual(base_load_limit(strength), Decimal(limit))

    def test_practical_limit_applies_both_factors(self) -> None:
        self.assertEqual(
            practical_load_limit("30", "1.5", "0.8"), Decimal("30.00")
        )

    def test_large_factor_product_is_exact(self) -> None:
        body_factor = "9" * 50
        capability_factor = "8" * 50
        result = practical_load_limit("30", body_factor, capability_factor)
        expected = Decimal(str(25 * int(body_factor) * int(capability_factor)))
        self.assertEqual(result, expected)


class CliTests(unittest.TestCase):
    def run_cli(self, *arguments: str) -> dict[str, object]:
        completed = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "fs_math.py"), *arguments],
            check=True,
            capture_output=True,
            text=True,
        )
        return json.loads(completed.stdout)

    def test_maxima_cli_uses_json_decimal_strings(self) -> None:
        result = self.run_cli(
            "maxima",
            "--strength",
            "15",
            "--agility",
            "15",
            "--stamina",
            "15",
            "--wisdom",
            "15",
        )
        self.assertEqual(
            result,
            {
                "maximum_fatigue": "105",
                "maximum_hp": "135",
                "maximum_rp": "150",
            },
        )

    def test_exp_cli_reports_all_crossings(self) -> None:
        result = self.run_cli(
            "exp", "--level", "1", "--current-exp", "0", "--award", "1000"
        )
        self.assertEqual(result["level"], 8)
        self.assertEqual(result["exp"], "90")
        self.assertEqual(
            result["thresholds_crossed"],
            ["100", "110", "120", "130", "140", "150", "160"],
        )

    def test_cli_decimal_strings_are_canonical(self) -> None:
        result = self.run_cli(
            "maxima",
            "--strength",
            "12.5",
            "--agility",
            "15.25",
            "--stamina",
            "9.75",
            "--wisdom",
            "20.125",
        )
        self.assertEqual(result["maximum_hp"], "107")
        self.assertEqual(result["maximum_rp"], "201.25")
        self.assertEqual(result["maximum_fatigue"], "82")


if __name__ == "__main__":
    unittest.main()
