import ast
import inspect
import unittest
from datetime import date, datetime

import services.age_eligibility_service as age_service
from services.age_eligibility_service import (
    AgeEligibility,
    AgeEligibilityError,
    check_age_eligibility,
    parse_verified_birth_date,
)


class AgeEligibilityServiceTest(unittest.TestCase):

    def test_typical_under_14_birth_date_is_not_eligible(self):
        result = check_age_eligibility(
            birth_date=date(2015, 5, 20),
            reference_date=date(2026, 10, 8),
        )

        self.assertIs(result, AgeEligibility.UNDER_14)

    def test_day_before_fourteenth_birthday_is_under_14(self):
        result = check_age_eligibility(
            birth_date=date(2012, 10, 9),
            reference_date=date(2026, 10, 8),
        )

        self.assertIs(result, AgeEligibility.UNDER_14)

    def test_fourteenth_birthday_is_eligible(self):
        result = check_age_eligibility(
            birth_date=date(2012, 10, 9),
            reference_date=date(2026, 10, 9),
        )

        self.assertIs(result, AgeEligibility.AGE_14_OR_OVER)

    def test_day_after_fourteenth_birthday_is_eligible(self):
        result = check_age_eligibility(
            birth_date=date(2012, 10, 9),
            reference_date=date(2026, 10, 10),
        )

        self.assertIs(result, AgeEligibility.AGE_14_OR_OVER)

    def test_verified_yyyymmdd_value_is_parsed(self):
        self.assertEqual(
            parse_verified_birth_date("20120324"),
            date(2012, 3, 24),
        )

    def test_birth_date_parser_rejects_invalid_lengths(self):
        for value in ("2012032", "201203240"):
            with self.subTest(value_length=len(value)):
                self._assert_private_parse_error(value)

    def test_birth_date_parser_rejects_non_ascii_digits(self):
        for value in ("2012O324", "２０１２０３２４"):
            with self.subTest(value_type=type(value).__name__):
                self._assert_private_parse_error(value)

    def test_birth_date_parser_rejects_nonexistent_calendar_date(self):
        self._assert_private_parse_error("20120230")

    def test_future_birth_date_is_rejected_without_exposing_it(self):
        future_birth_date = date(2026, 10, 9)

        with self.assertRaises(AgeEligibilityError) as context:
            check_age_eligibility(
                birth_date=future_birth_date,
                reference_date=date(2026, 10, 8),
            )

        self.assertNotIn(
            future_birth_date.isoformat(),
            str(context.exception),
        )

    def test_year_start_boundary_is_deterministic(self):
        birth_date = date(2012, 1, 1)

        self.assertIs(
            check_age_eligibility(
                birth_date=birth_date,
                reference_date=date(2025, 12, 31),
            ),
            AgeEligibility.UNDER_14,
        )
        self.assertIs(
            check_age_eligibility(
                birth_date=birth_date,
                reference_date=date(2026, 1, 1),
            ),
            AgeEligibility.AGE_14_OR_OVER,
        )

    def test_year_end_boundary_is_deterministic(self):
        birth_date = date(2012, 12, 31)

        self.assertIs(
            check_age_eligibility(
                birth_date=birth_date,
                reference_date=date(2026, 12, 30),
            ),
            AgeEligibility.UNDER_14,
        )
        self.assertIs(
            check_age_eligibility(
                birth_date=birth_date,
                reference_date=date(2026, 12, 31),
            ),
            AgeEligibility.AGE_14_OR_OVER,
        )

    def test_valid_leap_day_is_parsed(self):
        self.assertEqual(
            parse_verified_birth_date("20120229"),
            date(2012, 2, 29),
        )

    def test_february_29_policy_uses_march_1_in_a_non_leap_year(self):
        birth_date = date(2012, 2, 29)

        self.assertIs(
            check_age_eligibility(
                birth_date=birth_date,
                reference_date=date(2026, 2, 28),
            ),
            AgeEligibility.UNDER_14,
        )
        self.assertIs(
            check_age_eligibility(
                birth_date=birth_date,
                reference_date=date(2026, 3, 1),
            ),
            AgeEligibility.AGE_14_OR_OVER,
        )

    def test_service_requires_plain_date_values(self):
        invalid_values = (
            ("2012-10-09", date(2026, 10, 9)),
            (date(2012, 10, 9), "2026-10-09"),
            (
                datetime(2012, 10, 9, 0, 0),
                date(2026, 10, 9),
            ),
        )

        for birth_date, reference_date in invalid_values:
            with self.subTest(
                birth_type=type(birth_date).__name__,
                reference_type=type(reference_date).__name__,
            ):
                with self.assertRaises(AgeEligibilityError):
                    check_age_eligibility(
                        birth_date=birth_date,
                        reference_date=reference_date,
                    )

    def test_service_has_only_standard_library_dependencies(self):
        source = inspect.getsource(age_service)
        syntax_tree = ast.parse(source)
        imported_roots = {
            alias.name.split(".", 1)[0]
            for node in ast.walk(syntax_tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imported_roots.update(
            node.module.split(".", 1)[0]
            for node in ast.walk(syntax_tree)
            if isinstance(node, ast.ImportFrom)
            and node.module is not None
        )

        self.assertEqual(imported_roots, {"datetime", "enum"})

    def _assert_private_parse_error(self, value):
        with self.assertRaises(AgeEligibilityError) as context:
            parse_verified_birth_date(value)

        self.assertNotIn(value, str(context.exception))


if __name__ == "__main__":
    unittest.main()
