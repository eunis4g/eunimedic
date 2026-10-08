from datetime import date
from enum import Enum


MINIMUM_REGISTRATION_AGE = 14


class AgeEligibility(str, Enum):
    AGE_14_OR_OVER = "AGE_14_OR_OVER"
    UNDER_14 = "UNDER_14"


class AgeEligibilityError(ValueError):
    """Raised when age eligibility cannot be determined safely."""


def parse_verified_birth_date(value: str) -> date:
    """Parse a verified YYYYMMDD value without retaining the source text."""

    if (
        not isinstance(value, str)
        or len(value) != 8
        or not all("0" <= character <= "9" for character in value)
    ):
        raise AgeEligibilityError(
            "Verified birth date must use the YYYYMMDD format."
        )

    try:
        return date(
            int(value[0:4]),
            int(value[4:6]),
            int(value[6:8]),
        )
    except ValueError as error:
        raise AgeEligibilityError(
            "Verified birth date is not a valid calendar date."
        ) from None


def check_age_eligibility(
    *,
    birth_date: date,
    reference_date: date,
) -> AgeEligibility:
    """Return registration eligibility as of an explicit reference date."""

    if type(birth_date) is not date or type(reference_date) is not date:
        raise AgeEligibilityError(
            "Birth date and reference date must be date values."
        )

    if birth_date > reference_date:
        raise AgeEligibilityError(
            "Verified birth date cannot be in the future."
        )

    eligibility_date = _minimum_age_eligibility_date(birth_date)

    if reference_date >= eligibility_date:
        return AgeEligibility.AGE_14_OR_OVER

    return AgeEligibility.UNDER_14


def _minimum_age_eligibility_date(birth_date: date) -> date:
    eligibility_year = birth_date.year + MINIMUM_REGISTRATION_AGE

    try:
        return birth_date.replace(year=eligibility_year)
    except ValueError:
        # A February 29 birthday has no same calendar date in a non-leap
        # eligibility year. Use March 1 so the policy never admits a user
        # one day earlier at the February 28 boundary.
        return date(eligibility_year, 3, 1)
