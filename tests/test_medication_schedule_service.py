import unittest
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

from services.medication_schedule_service import (  # noqa: E402
    MedicationScheduleCalculationError,
    calculate_daily_frequency,
    calculate_occurrence_summary,
    calculate_planned_total,
    count_elapsed_occurrences,
    generate_future_occurrences,
    normalize_medication_times,
    validate_plan_capacity,
)


SEOUL = ZoneInfo("Asia/Seoul")
NEW_YORK = ZoneInfo("America/New_York")


def local_datetime(
    year,
    month,
    day,
    hour=0,
    minute=0,
    *,
    timezone=SEOUL,
    fold=0,
):
    return datetime(
        year,
        month,
        day,
        hour,
        minute,
        tzinfo=timezone,
        fold=fold,
    )


class MedicationScheduleServiceTest(unittest.TestCase):
    def test_daily_frequency_for_one_and_multiple_times(self):
        self.assertEqual(calculate_daily_frequency([time(9)]), 1)
        self.assertEqual(
            calculate_daily_frequency([time(8), time(14), time(20)]),
            3,
        )

    def test_times_are_sorted(self):
        self.assertEqual(
            normalize_medication_times(
                [time(20), time(8), time(14)]
            ),
            (time(8), time(14), time(20)),
        )

    def test_empty_and_duplicate_times_are_rejected(self):
        for invalid_times in ([], [time(8), time(8)]):
            with self.subTest(times=invalid_times):
                with self.assertRaises(
                    MedicationScheduleCalculationError
                ):
                    normalize_medication_times(invalid_times)

    def test_zero_course_days_are_rejected(self):
        with self.assertRaises(MedicationScheduleCalculationError):
            calculate_planned_total(times=[time(9)], course_days=0)

    def test_negative_counts_are_rejected(self):
        invalid_counts = ((-1, 0), (0, -1))

        for reported, accounted in invalid_counts:
            with self.subTest(reported=reported, accounted=accounted):
                with self.assertRaises(
                    MedicationScheduleCalculationError
                ):
                    validate_plan_capacity(
                        times=[time(9)],
                        course_days=1,
                        reported_doses_taken_before_tracking=reported,
                        accounted_occurrence_count=accounted,
                    )

    def test_planned_total_is_frequency_times_course_days(self):
        self.assertEqual(
            calculate_planned_total(
                times=[time(8), time(14), time(20)],
                course_days=3,
            ),
            9,
        )

    def test_reported_and_accounted_counts_reduce_remaining(self):
        reported_summary = self._summary(
            reference_at=local_datetime(2026, 10, 5, 14),
            reported=2,
        )
        accounted_summary = self._summary(
            reference_at=local_datetime(2026, 10, 5, 14),
            accounted=3,
        )

        self.assertEqual(reported_summary.remaining, 7)
        self.assertEqual(accounted_summary.remaining, 6)

    def test_elapsed_current_segment_reduces_remaining(self):
        summary = self._summary(
            reference_at=local_datetime(2026, 10, 6, 15)
        )

        self.assertEqual(summary.elapsed_in_current_segment, 4)
        self.assertEqual(summary.remaining, 5)

    def test_remaining_can_be_zero(self):
        tracking = local_datetime(2026, 10, 5, 14)
        summary = self._summary(reference_at=tracking, reported=9)
        occurrences = generate_future_occurrences(
            start_date=date(2026, 10, 5),
            course_days=3,
            times=[time(8), time(14), time(20)],
            reported_doses_taken_before_tracking=9,
            accounted_occurrence_count=0,
            reminder_tracking_started_at=tracking.astimezone(UTC),
            reference_at=tracking,
            timezone_name="Asia/Seoul",
        )

        self.assertEqual(summary.remaining, 0)
        self.assertEqual(occurrences, ())

    def test_counts_above_planned_total_are_rejected(self):
        with self.assertRaises(MedicationScheduleCalculationError):
            validate_plan_capacity(
                times=[time(8), time(20)],
                course_days=2,
                reported_doses_taken_before_tracking=2,
                accounted_occurrence_count=3,
            )

    def test_counts_equal_to_planned_total_are_allowed(self):
        self.assertEqual(
            validate_plan_capacity(
                times=[time(8), time(20)],
                course_days=2,
                reported_doses_taken_before_tracking=2,
                accounted_occurrence_count=2,
            ),
            4,
        )

    def test_past_and_today_start_dates(self):
        for start in (date(2026, 10, 1), date(2026, 10, 5)):
            with self.subTest(start_date=start):
                occurrences = generate_future_occurrences(
                    start_date=start,
                    course_days=1,
                    times=[time(8)],
                    reported_doses_taken_before_tracking=0,
                    accounted_occurrence_count=0,
                    reminder_tracking_started_at=local_datetime(
                        2026, 10, 5, 7
                    ).astimezone(UTC),
                    reference_at=local_datetime(2026, 10, 5, 7),
                    timezone_name="Asia/Seoul",
                )

                self.assertEqual(
                    occurrences,
                    (local_datetime(2026, 10, 5, 8),),
                )

    def test_future_start_date_has_no_earlier_occurrence(self):
        tracking = local_datetime(2026, 10, 5)
        elapsed = count_elapsed_occurrences(
            start_date=date(2026, 10, 10),
            times=[time(8), time(20)],
            reminder_tracking_started_at=tracking.astimezone(UTC),
            reference_at=tracking,
            timezone_name="Asia/Seoul",
        )
        occurrences = generate_future_occurrences(
            start_date=date(2026, 10, 10),
            course_days=1,
            times=[time(8), time(20)],
            reported_doses_taken_before_tracking=0,
            accounted_occurrence_count=0,
            reminder_tracking_started_at=tracking.astimezone(UTC),
            reference_at=tracking,
            timezone_name="Asia/Seoul",
        )

        self.assertEqual(elapsed, 0)
        self.assertEqual(occurrences[0], local_datetime(2026, 10, 10, 8))

    def test_large_materialized_result_is_rejected_safely(self):
        tracking = local_datetime(2026, 10, 5)

        with self.assertRaises(MedicationScheduleCalculationError):
            generate_future_occurrences(
                start_date=date(2026, 10, 5),
                course_days=100_001,
                times=[time(9)],
                reported_doses_taken_before_tracking=0,
                accounted_occurrence_count=0,
                reminder_tracking_started_at=tracking.astimezone(UTC),
                reference_at=tracking,
                timezone_name="Asia/Seoul",
            )

    def test_exact_boundary_is_not_elapsed_and_is_future(self):
        tracking = local_datetime(2026, 10, 5, 14)
        elapsed = count_elapsed_occurrences(
            start_date=date(2026, 10, 5),
            times=[time(14)],
            reminder_tracking_started_at=tracking.astimezone(UTC),
            reference_at=tracking,
            timezone_name="Asia/Seoul",
        )
        occurrences = generate_future_occurrences(
            start_date=date(2026, 10, 5),
            course_days=1,
            times=[time(14)],
            reported_doses_taken_before_tracking=0,
            accounted_occurrence_count=0,
            reminder_tracking_started_at=tracking.astimezone(UTC),
            reference_at=tracking,
            timezone_name="Asia/Seoul",
        )

        self.assertEqual(elapsed, 0)
        self.assertEqual(occurrences, (tracking,))

    def test_elapsed_interval_excludes_reference_at(self):
        elapsed = count_elapsed_occurrences(
            start_date=date(2026, 10, 5),
            times=[time(14), time(20)],
            reminder_tracking_started_at=local_datetime(
                2026, 10, 5, 14
            ).astimezone(UTC),
            reference_at=local_datetime(2026, 10, 5, 20),
            timezone_name="Asia/Seoul",
        )

        self.assertEqual(elapsed, 1)

    def test_afternoon_start_produces_exactly_nine_occurrences(self):
        self.assertEqual(
            self._initial_nine_occurrences(),
            (
                local_datetime(2026, 10, 5, 14),
                local_datetime(2026, 10, 5, 20),
                local_datetime(2026, 10, 6, 8),
                local_datetime(2026, 10, 6, 14),
                local_datetime(2026, 10, 6, 20),
                local_datetime(2026, 10, 7, 8),
                local_datetime(2026, 10, 7, 14),
                local_datetime(2026, 10, 7, 20),
                local_datetime(2026, 10, 8, 8),
            ),
        )

    def test_no_occurrence_exists_after_the_ninth_result(self):
        occurrences = self._initial_nine_occurrences()

        self.assertEqual(len(occurrences), 9)
        self.assertNotIn(local_datetime(2026, 10, 8, 14), occurrences)

    def test_elapsed_before_middle_edit_is_four(self):
        elapsed = count_elapsed_occurrences(
            start_date=date(2026, 10, 5),
            times=[time(8), time(14), time(20)],
            reminder_tracking_started_at=local_datetime(
                2026, 10, 5, 14
            ).astimezone(UTC),
            reference_at=local_datetime(2026, 10, 6, 15),
            timezone_name="Asia/Seoul",
        )

        self.assertEqual(elapsed, 4)

    def test_three_times_after_middle_edit_produce_five(self):
        edit_time = local_datetime(2026, 10, 6, 15)
        occurrences = generate_future_occurrences(
            start_date=date(2026, 10, 5),
            course_days=3,
            times=[time(9), time(15), time(21)],
            reported_doses_taken_before_tracking=0,
            accounted_occurrence_count=4,
            reminder_tracking_started_at=edit_time.astimezone(UTC),
            reference_at=edit_time,
            timezone_name="Asia/Seoul",
        )

        self.assertEqual(
            occurrences,
            (
                local_datetime(2026, 10, 6, 15),
                local_datetime(2026, 10, 6, 21),
                local_datetime(2026, 10, 7, 9),
                local_datetime(2026, 10, 7, 15),
                local_datetime(2026, 10, 7, 21),
            ),
        )

    def test_two_times_after_middle_edit_produce_two(self):
        edit_time = local_datetime(2026, 10, 6, 15)
        occurrences = generate_future_occurrences(
            start_date=date(2026, 10, 5),
            course_days=3,
            times=[time(9), time(21)],
            reported_doses_taken_before_tracking=0,
            accounted_occurrence_count=4,
            reminder_tracking_started_at=edit_time.astimezone(UTC),
            reference_at=edit_time,
            timezone_name="Asia/Seoul",
        )

        self.assertEqual(
            occurrences,
            (
                local_datetime(2026, 10, 6, 21),
                local_datetime(2026, 10, 7, 9),
            ),
        )

    def test_occurrences_are_timezone_aware(self):
        occurrence = self._initial_nine_occurrences()[0]

        self.assertIsNotNone(occurrence.tzinfo)
        self.assertIsNotNone(occurrence.utcoffset())
        self.assertEqual(occurrence.tzinfo, SEOUL)

    def test_nonexistent_dst_time_is_shifted_forward(self):
        tracking = local_datetime(
            2026,
            3,
            8,
            timezone=NEW_YORK,
        )
        occurrences = generate_future_occurrences(
            start_date=date(2026, 3, 8),
            course_days=1,
            times=[time(2, 30)],
            reported_doses_taken_before_tracking=0,
            accounted_occurrence_count=0,
            reminder_tracking_started_at=tracking.astimezone(UTC),
            reference_at=tracking,
            timezone_name="America/New_York",
        )

        self.assertEqual(
            occurrences,
            (local_datetime(2026, 3, 8, 3, 30, timezone=NEW_YORK),),
        )

    def test_ambiguous_dst_time_uses_first_fold(self):
        tracking = local_datetime(
            2026,
            11,
            1,
            timezone=NEW_YORK,
        )
        occurrences = generate_future_occurrences(
            start_date=date(2026, 11, 1),
            course_days=1,
            times=[time(1, 30)],
            reported_doses_taken_before_tracking=0,
            accounted_occurrence_count=0,
            reminder_tracking_started_at=tracking.astimezone(UTC),
            reference_at=tracking,
            timezone_name="America/New_York",
        )

        self.assertEqual(occurrences[0].fold, 0)
        self.assertEqual(
            occurrences[0].astimezone(UTC),
            datetime(2026, 11, 1, 5, 30, tzinfo=UTC),
        )

    def test_unknown_timezone_is_rejected(self):
        with self.assertRaises(MedicationScheduleCalculationError):
            generate_future_occurrences(
                start_date=date(2026, 10, 5),
                course_days=1,
                times=[time(9)],
                reported_doses_taken_before_tracking=0,
                accounted_occurrence_count=0,
                reminder_tracking_started_at=datetime(
                    2026, 10, 5, tzinfo=UTC
                ),
                reference_at=datetime(2026, 10, 5, tzinfo=UTC),
                timezone_name="Invalid/Timezone",
            )

    def test_naive_reference_datetime_is_rejected(self):
        with self.assertRaises(MedicationScheduleCalculationError):
            count_elapsed_occurrences(
                start_date=date(2026, 10, 5),
                times=[time(9)],
                reminder_tracking_started_at=datetime(
                    2026, 10, 5, tzinfo=UTC
                ),
                reference_at=datetime(2026, 10, 5),
                timezone_name="Asia/Seoul",
            )

    def _summary(self, *, reference_at, reported=0, accounted=0):
        return calculate_occurrence_summary(
            start_date=date(2026, 10, 5),
            course_days=3,
            times=[time(8), time(14), time(20)],
            reported_doses_taken_before_tracking=reported,
            accounted_occurrence_count=accounted,
            reminder_tracking_started_at=local_datetime(
                2026, 10, 5, 14
            ).astimezone(UTC),
            reference_at=reference_at,
            timezone_name="Asia/Seoul",
        )

    def _initial_nine_occurrences(self):
        tracking = local_datetime(2026, 10, 5, 14)
        return generate_future_occurrences(
            start_date=date(2026, 10, 5),
            course_days=3,
            times=[time(8), time(14), time(20)],
            reported_doses_taken_before_tracking=0,
            accounted_occurrence_count=0,
            reminder_tracking_started_at=tracking.astimezone(UTC),
            reference_at=tracking,
            timezone_name="Asia/Seoul",
        )


if __name__ == "__main__":
    unittest.main()
