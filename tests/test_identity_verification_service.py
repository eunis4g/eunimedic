import hashlib
import hmac
import inspect
import tempfile
import unittest
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import UUID

from flask_migrate import upgrade
from sqlalchemy import create_engine, func, select, text, update
from sqlalchemy.orm import Session

import app as app_module
from models import IdentityVerificationSession, User, db
from services.age_eligibility_service import AgeEligibility
from services.fake_identity_verification_provider import (
    FakeIdentityVerificationProvider,
)
from services.identity_verification_provider import (
    IdentityVerificationProvider,
    IdentityVerificationProviderError,
    IdentityVerificationProviderErrorCode,
    IdentityVerificationProviderName,
    VerifiedIdentityResult,
)
from services.identity_verification_service import (
    IdentityVerificationServiceError,
    IdentityVerificationServiceErrorCode,
    _claim_completion,
    _finalize_verified_completion,
    _handle_provider_completion_error,
    _release_completion_claim,
    complete_identity_verification,
    start_identity_verification,
)


MIGRATION_HEAD = "f3a7c9e1b2d4"
MIGRATIONS_DIRECTORY = str(
    Path(__file__).resolve().parents[1] / "migrations"
)
ACTION_TIME = datetime(2026, 10, 8, tzinfo=UTC)
SESSION_TTL = timedelta(minutes=10)
HMAC_KEY = b"synthetic-identity-hmac-key"
CLAIM_TOKEN = "a" * 32
OTHER_CLAIM_TOKEN = "b" * 32


def stored_as_utc(value):
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)

    return value.astimezone(UTC)


class ProviderSpy(IdentityVerificationProvider):

    def __init__(
        self,
        delegate,
        *,
        before_start=None,
        before_get=None,
        get_error=None,
        result_transform=None,
    ):
        self.delegate = delegate
        self.before_start = before_start
        self.before_get = before_get
        self.get_error = get_error
        self.result_transform = result_transform
        self.start_call_count = 0
        self.get_call_count = 0

    @property
    def provider(self):
        return self.delegate.provider

    def start_verification(self):
        self.start_call_count += 1
        if self.before_start is not None:
            self.before_start()
        return self.delegate.start_verification()

    def get_verified_identity(self, *, provider_transaction_id):
        self.get_call_count += 1
        if self.before_get is not None:
            self.before_get(provider_transaction_id)
        if self.get_error is not None:
            raise self.get_error

        result = self.delegate.get_verified_identity(
            provider_transaction_id=provider_transaction_id
        )
        if self.result_transform is not None:
            return self.result_transform(result)
        return result


class IdentityVerificationServiceTest(unittest.TestCase):

    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = (
            Path(self.temporary_directory.name) / "identity-service.db"
        )
        self.application_context = app_module.app.app_context()
        self.application_context.push()
        db.session.remove()
        self.original_engine = db.engines[None]
        self.test_engine = create_engine(
            "sqlite:///" + self.database_path.as_posix()
        )
        db.engines[None] = self.test_engine
        upgrade(
            directory=MIGRATIONS_DIRECTORY,
            revision=MIGRATION_HEAD,
        )
        self.public_id_counter = 0
        self.transaction_id_counter = 0

    def tearDown(self):
        db.session.remove()
        db.engines[None] = self.original_engine
        self.test_engine.dispose()
        self.application_context.pop()
        self.temporary_directory.cleanup()

    def make_provider(
        self,
        *,
        birth_date=date(2000, 1, 1),
        identity_subject="subject-one",
        start_error_code=None,
        verification_error_code=None,
        **spy_options,
    ):
        def transaction_id_factory():
            self.transaction_id_counter += 1
            return f"provider-transaction-{self.transaction_id_counter}"

        fake = FakeIdentityVerificationProvider(
            provider=IdentityVerificationProviderName.TOSS,
            verified_birth_date=birth_date,
            identity_subject=identity_subject,
            transaction_id_factory=transaction_id_factory,
            start_error_code=start_error_code,
            verification_error_code=verification_error_code,
        )
        return ProviderSpy(fake, **spy_options)

    def next_public_id(self):
        self.public_id_counter += 1
        return (
            "00000000-0000-4000-8000-"
            f"{self.public_id_counter:012d}"
        )

    def start(self, provider, *, action_time=ACTION_TIME, ttl=SESSION_TTL):
        public_id = self.next_public_id()
        result = start_identity_verification(
            db.session,
            provider=provider,
            action_time=action_time,
            session_ttl=ttl,
            verification_session_id_factory=lambda: public_id,
        )
        return result

    def complete(
        self,
        start_result,
        provider,
        *,
        action_time=ACTION_TIME + timedelta(minutes=1),
        key=HMAC_KEY,
        claim_token=CLAIM_TOKEN,
    ):
        return complete_identity_verification(
            db.session,
            verification_session_id=(
                start_result.verification_session_id
            ),
            provider=provider,
            action_time=action_time,
            identity_subject_hmac_key=key,
            claim_token_factory=lambda: claim_token,
        )

    def get_session(self, public_id):
        db.session.expire_all()
        return db.session.scalar(
            select(IdentityVerificationSession).where(
                IdentityVerificationSession.verification_session_id
                == public_id
            )
        )

    def test_start_creates_pending_session_and_returns_public_handoff(self):
        provider = self.make_provider()

        result = self.start(provider)
        stored = self.get_session(result.verification_session_id)

        self.assertEqual(
            result.verification_session_id,
            "00000000-0000-4000-8000-000000000001",
        )
        self.assertEqual(
            result.authentication_url,
            "fake://identity-verification/provider-transaction-1",
        )
        self.assertEqual(
            result.provider_transaction_id,
            "provider-transaction-1",
        )
        self.assertEqual(result.expires_at, ACTION_TIME + SESSION_TTL)
        self.assertEqual(stored.status, "pending")
        self.assertEqual(stored.provider, "toss")
        self.assertEqual(stored.purpose, "android_registration")
        self.assertEqual(
            stored.provider_transaction_id,
            "provider-transaction-1",
        )
        self.assertEqual(
            result.provider_transaction_id,
            stored.provider_transaction_id,
        )
        self.assertIsNone(stored.completion_claim_token)
        self.assertEqual(provider.start_call_count, 1)
        self.assertNotIn(
            "identity_verification_session_id",
            result.__dataclass_fields__,
        )
        self.assertEqual(
            set(result.__dataclass_fields__),
            {
                "verification_session_id",
                "provider_transaction_id",
                "authentication_url",
                "expires_at",
            },
        )
        self.assertNotIn("completion_claim_token", result.__dataclass_fields__)
        self.assertNotIn("identity_subject_digest", result.__dataclass_fields__)
        with self.test_engine.connect() as connection:
            persisted_text = repr(
                tuple(
                    connection.execute(
                        text("SELECT * FROM identity_verification_sessions")
                    ).one()
                )
            )
        self.assertNotIn(result.authentication_url, persisted_text)

    def test_completion_does_not_accept_client_provider_transaction_id(self):
        parameters = inspect.signature(
            complete_identity_verification
        ).parameters

        self.assertNotIn("provider_transaction_id", parameters)

    def test_start_commits_session_before_provider_call(self):
        observations = []
        public_id = self.next_public_id()

        def observe_start():
            with Session(self.test_engine) as other_session:
                stored = other_session.scalar(
                    select(IdentityVerificationSession).where(
                        IdentityVerificationSession.verification_session_id
                        == public_id
                    )
                )
                observations.append(
                    (
                        stored is not None,
                        stored.status if stored is not None else None,
                        (
                            stored.provider_transaction_id
                            if stored is not None
                            else "missing"
                        ),
                    )
                )

        provider = self.make_provider(before_start=observe_start)
        start_identity_verification(
            db.session,
            provider=provider,
            action_time=ACTION_TIME,
            session_ttl=SESSION_TTL,
            verification_session_id_factory=lambda: public_id,
        )

        self.assertEqual(observations, [(True, "pending", None)])

    def test_start_default_public_identifier_is_uuid4(self):
        provider = self.make_provider()

        result = start_identity_verification(
            db.session,
            provider=provider,
            action_time=ACTION_TIME,
            session_ttl=SESSION_TTL,
        )

        parsed = UUID(result.verification_session_id)
        self.assertEqual(parsed.version, 4)
        self.assertEqual(str(parsed), result.verification_session_id)

    def test_start_provider_failure_is_sanitized_and_terminal(self):
        provider = self.make_provider(
            start_error_code=(
                IdentityVerificationProviderErrorCode.PROVIDER_UNAVAILABLE
            )
        )

        with self.assertRaises(IdentityVerificationServiceError) as context:
            self.start(provider)

        self.assertIs(
            context.exception.code,
            IdentityVerificationServiceErrorCode.PROVIDER_UNAVAILABLE,
        )
        stored = db.session.scalar(select(IdentityVerificationSession))
        self.assertEqual(stored.status, "failed")
        self.assertEqual(stored.failure_code, "provider_unavailable")
        self.assertIsNone(stored.provider_transaction_id)
        self.assertNotIn("provider-transaction", repr(context.exception))

    def test_start_unexpected_provider_error_is_sanitized_as_invalid_response(self):
        def raise_unexpected():
            raise RuntimeError("raw-provider-payload")

        provider = self.make_provider(before_start=raise_unexpected)

        with self.assertRaises(IdentityVerificationServiceError) as context:
            self.start(provider)

        self.assertIs(
            context.exception.code,
            IdentityVerificationServiceErrorCode.INVALID_PROVIDER_RESPONSE,
        )
        self.assertNotIn("raw-provider-payload", repr(context.exception))
        stored = db.session.scalar(select(IdentityVerificationSession))
        self.assertEqual(stored.status, "failed")
        self.assertEqual(stored.failure_code, "invalid_provider_response")

    def test_start_rejects_nonpositive_ttl_without_provider_call(self):
        for ttl in (timedelta(0), timedelta(seconds=-1)):
            with self.subTest(ttl=ttl):
                provider = self.make_provider()
                with self.assertRaises(ValueError):
                    self.start(provider, ttl=ttl)
                self.assertEqual(provider.start_call_count, 0)

        self.assertEqual(
            db.session.scalar(
                select(func.count()).select_from(IdentityVerificationSession)
            ),
            0,
        )

    def test_start_rejects_naive_action_time_without_provider_call(self):
        provider = self.make_provider()

        with self.assertRaises(ValueError):
            self.start(
                provider,
                action_time=datetime(2026, 10, 8),
            )

        self.assertEqual(provider.start_call_count, 0)

    def test_complete_claim_is_committed_before_provider_lookup(self):
        observations = []

        def observe_get(provider_transaction_id):
            with Session(self.test_engine) as other_session:
                stored = other_session.scalar(
                    select(IdentityVerificationSession).where(
                        IdentityVerificationSession.provider_transaction_id
                        == provider_transaction_id
                    )
                )
                observations.append(
                    (
                        stored.completion_claim_token,
                        stored.completion_claimed_at is not None,
                    )
                )

        provider = self.make_provider(before_get=observe_get)
        started = self.start(provider)

        self.complete(started, provider)

        self.assertEqual(observations, [(CLAIM_TOKEN, True)])

    def test_complete_verifies_adult_and_clears_claim(self):
        provider = self.make_provider(
            birth_date=date(2000, 1, 1),
            identity_subject="stable-subject",
        )
        started = self.start(provider)

        result = self.complete(started, provider)
        stored = self.get_session(started.verification_session_id)

        self.assertEqual(result.status, "verified")
        self.assertIs(
            result.age_eligibility,
            AgeEligibility.AGE_14_OR_OVER,
        )
        self.assertEqual(stored.status, "verified")
        self.assertEqual(stored.age_eligibility, "AGE_14_OR_OVER")
        self.assertEqual(len(stored.identity_subject_digest), 64)
        self.assertTrue(
            all(
                character in "0123456789abcdef"
                for character in stored.identity_subject_digest
            )
        )
        self.assertEqual(
            stored_as_utc(stored.verified_at),
            ACTION_TIME + timedelta(minutes=1),
        )
        self.assertIsNone(stored.completion_claim_token)
        self.assertIsNone(stored.completion_claimed_at)
        self.assertIsNone(stored.failure_code)

    def test_under_14_is_a_successful_verified_result(self):
        provider = self.make_provider(birth_date=date(2012, 10, 9))
        started = self.start(provider)

        result = self.complete(started, provider)
        stored = self.get_session(started.verification_session_id)

        self.assertEqual(result.status, "verified")
        self.assertIs(result.age_eligibility, AgeEligibility.UNDER_14)
        self.assertEqual(stored.status, "verified")
        self.assertEqual(stored.age_eligibility, "UNDER_14")

    def test_age_boundary_uses_asia_seoul_calendar_date(self):
        before_provider = self.make_provider(
            birth_date=date(2012, 10, 9),
            identity_subject="before-boundary",
        )
        before_started = self.start(
            before_provider,
            action_time=datetime(2026, 10, 8, 14, 50, tzinfo=UTC),
        )
        before = self.complete(
            before_started,
            before_provider,
            action_time=datetime(2026, 10, 8, 14, 59, tzinfo=UTC),
        )

        after_provider = self.make_provider(
            birth_date=date(2012, 10, 9),
            identity_subject="after-boundary",
        )
        after_started = self.start(
            after_provider,
            action_time=datetime(2026, 10, 8, 15, tzinfo=UTC),
        )
        after = self.complete(
            after_started,
            after_provider,
            action_time=datetime(2026, 10, 8, 15, 1, tzinfo=UTC),
        )

        self.assertIs(before.age_eligibility, AgeEligibility.UNDER_14)
        self.assertIs(
            after.age_eligibility,
            AgeEligibility.AGE_14_OR_OVER,
        )

    def test_hmac_uses_documented_domain_provider_and_subject_format(self):
        subject = "provider-subject-123"
        provider = self.make_provider(identity_subject=subject)
        started = self.start(provider)

        self.complete(started, provider)
        stored = self.get_session(started.verification_session_id)
        expected = hmac.new(
            HMAC_KEY,
            b"medicine-web.identity-subject.v1\x00toss\x00"
            + subject.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        self.assertEqual(stored.identity_subject_digest, expected)

    def test_same_subject_is_stable_and_different_subject_changes_digest(self):
        first_provider = self.make_provider(identity_subject="same-subject")
        first_started = self.start(first_provider)
        self.complete(first_started, first_provider)
        first_digest = self.get_session(
            first_started.verification_session_id
        ).identity_subject_digest

        second_provider = self.make_provider(identity_subject="same-subject")
        second_started = self.start(second_provider)
        self.complete(second_started, second_provider)
        second_digest = self.get_session(
            second_started.verification_session_id
        ).identity_subject_digest

        third_provider = self.make_provider(identity_subject="other-subject")
        third_started = self.start(third_provider)
        self.complete(third_started, third_provider)
        third_digest = self.get_session(
            third_started.verification_session_id
        ).identity_subject_digest

        self.assertEqual(first_digest, second_digest)
        self.assertNotEqual(first_digest, third_digest)

    def test_raw_identity_birth_date_and_secret_are_never_persisted(self):
        raw_subject = "raw-identity-subject-never-store"
        birth_date = date(2001, 2, 3)
        provider = self.make_provider(
            birth_date=birth_date,
            identity_subject=raw_subject,
        )
        started = self.start(provider)

        result = self.complete(started, provider)
        with self.test_engine.connect() as connection:
            stored_row = connection.execute(
                text("SELECT * FROM identity_verification_sessions")
            ).one()
        persisted_text = repr(tuple(stored_row))

        self.assertNotIn(raw_subject, persisted_text)
        self.assertNotIn(birth_date.isoformat(), persisted_text)
        self.assertNotIn(HMAC_KEY.decode("ascii"), persisted_text)
        self.assertNotIn(raw_subject, repr(result))
        self.assertNotIn(birth_date.isoformat(), repr(result))
        self.assertNotIn(HMAC_KEY.decode("ascii"), repr(result))

    def test_verified_completion_is_idempotent_without_provider_recall(self):
        provider = self.make_provider()
        started = self.start(provider)

        first = self.complete(started, provider)
        second = self.complete(started, provider, claim_token=OTHER_CLAIM_TOKEN)

        self.assertEqual(first, second)
        self.assertEqual(provider.get_call_count, 1)

    def test_atomic_claim_allows_only_one_database_session(self):
        provider = self.make_provider()
        started = self.start(provider)
        claim_time = ACTION_TIME + timedelta(minutes=1)

        with Session(self.test_engine) as first_session:
            first_claimed = _claim_completion(
                first_session,
                verification_session_id=started.verification_session_id,
                claim_token=CLAIM_TOKEN,
                action_time=claim_time,
            )

        with Session(self.test_engine) as second_session:
            second_claimed = _claim_completion(
                second_session,
                verification_session_id=started.verification_session_id,
                claim_token=OTHER_CLAIM_TOKEN,
                action_time=claim_time,
            )

        self.assertTrue(first_claimed)
        self.assertFalse(second_claimed)
        self.assertEqual(provider.get_call_count, 0)

    def test_losing_completion_request_never_calls_provider(self):
        provider = self.make_provider()
        started = self.start(provider)
        with Session(self.test_engine) as owner_session:
            self.assertTrue(
                _claim_completion(
                    owner_session,
                    verification_session_id=(
                        started.verification_session_id
                    ),
                    claim_token=CLAIM_TOKEN,
                    action_time=ACTION_TIME + timedelta(minutes=1),
                )
            )

        with self.assertRaises(IdentityVerificationServiceError) as context:
            self.complete(
                started,
                provider,
                claim_token=OTHER_CLAIM_TOKEN,
            )

        self.assertIs(
            context.exception.code,
            IdentityVerificationServiceErrorCode.COMPLETION_IN_PROGRESS,
        )
        self.assertEqual(provider.get_call_count, 0)

    def test_wrong_token_cannot_finalize_or_release_claim(self):
        provider = self.make_provider()
        started = self.start(provider)
        claim_time = ACTION_TIME + timedelta(minutes=1)
        self.assertTrue(
            _claim_completion(
                db.session,
                verification_session_id=started.verification_session_id,
                claim_token=CLAIM_TOKEN,
                action_time=claim_time,
            )
        )

        self.assertFalse(
            _finalize_verified_completion(
                db.session,
                verification_session_id=started.verification_session_id,
                claim_token=OTHER_CLAIM_TOKEN,
                action_time=claim_time,
                age_eligibility=AgeEligibility.AGE_14_OR_OVER,
                identity_subject_digest="c" * 64,
            )
        )
        self.assertFalse(
            _release_completion_claim(
                db.session,
                verification_session_id=started.verification_session_id,
                claim_token=OTHER_CLAIM_TOKEN,
                action_time=claim_time,
            )
        )
        stored = self.get_session(started.verification_session_id)
        self.assertEqual(stored.status, "pending")
        self.assertEqual(stored.completion_claim_token, CLAIM_TOKEN)

    def test_expired_session_never_calls_provider_and_becomes_expired(self):
        provider = self.make_provider()
        started = self.start(provider, ttl=timedelta(minutes=1))

        with self.assertRaises(IdentityVerificationServiceError) as context:
            self.complete(
                started,
                provider,
                action_time=ACTION_TIME + timedelta(minutes=1),
            )

        self.assertIs(
            context.exception.code,
            IdentityVerificationServiceErrorCode.VERIFICATION_EXPIRED,
        )
        stored = self.get_session(started.verification_session_id)
        self.assertEqual(stored.status, "expired")
        self.assertEqual(stored.failure_code, "session_expired")
        self.assertIsNone(stored.completion_claim_token)
        self.assertEqual(provider.get_call_count, 0)

    def test_provider_unavailable_releases_claim_and_remains_retryable(self):
        unavailable = IdentityVerificationProviderError(
            IdentityVerificationProviderErrorCode.PROVIDER_UNAVAILABLE
        )
        provider = self.make_provider(get_error=unavailable)
        started = self.start(provider)

        with self.assertRaises(IdentityVerificationServiceError) as context:
            self.complete(started, provider)

        self.assertIs(
            context.exception.code,
            IdentityVerificationServiceErrorCode.PROVIDER_UNAVAILABLE,
        )
        stored = self.get_session(started.verification_session_id)
        self.assertEqual(stored.status, "pending")
        self.assertEqual(stored.failure_code, "provider_unavailable")
        self.assertIsNone(stored.completion_claim_token)
        self.assertIsNone(stored.completion_claimed_at)

        provider.get_error = None
        result = self.complete(
            started,
            provider,
            claim_token=OTHER_CLAIM_TOKEN,
        )
        stored = self.get_session(started.verification_session_id)
        self.assertEqual(result.status, "verified")
        self.assertIsNone(stored.failure_code)
        self.assertEqual(provider.get_call_count, 2)

    def test_provider_pending_releases_claim_clears_failure_and_retries(self):
        unavailable = IdentityVerificationProviderError(
            IdentityVerificationProviderErrorCode.PROVIDER_UNAVAILABLE
        )
        pending = IdentityVerificationProviderError(
            IdentityVerificationProviderErrorCode.VERIFICATION_PENDING
        )
        provider = self.make_provider(get_error=unavailable)
        started = self.start(provider)

        with self.assertRaises(IdentityVerificationServiceError):
            self.complete(started, provider)
        self.assertEqual(
            self.get_session(started.verification_session_id).failure_code,
            "provider_unavailable",
        )

        provider.get_error = pending
        with self.assertRaises(IdentityVerificationServiceError) as context:
            self.complete(
                started,
                provider,
                claim_token=OTHER_CLAIM_TOKEN,
            )

        self.assertIs(
            context.exception.code,
            IdentityVerificationServiceErrorCode.VERIFICATION_NOT_COMPLETED,
        )
        stored = self.get_session(started.verification_session_id)
        self.assertEqual(stored.status, "pending")
        self.assertEqual(
            stored.provider_transaction_id,
            started.provider_transaction_id,
        )
        self.assertIsNone(stored.failure_code)
        self.assertIsNone(stored.age_eligibility)
        self.assertIsNone(stored.identity_subject_digest)
        self.assertIsNone(stored.verified_at)
        self.assertIsNone(stored.completion_claim_token)
        self.assertIsNone(stored.completion_claimed_at)
        self.assertEqual(
            db.session.scalar(select(func.count()).select_from(User)),
            0,
        )
        error_text = repr(context.exception)
        self.assertNotIn(started.provider_transaction_id, error_text)
        self.assertNotIn(OTHER_CLAIM_TOKEN, error_text)
        self.assertNotIn("CE3102", error_text)

        provider.get_error = None
        result = self.complete(
            started,
            provider,
            claim_token="c" * 32,
        )

        self.assertEqual(result.status, "verified")
        self.assertEqual(provider.get_call_count, 3)

    def test_provider_pending_cannot_release_another_owners_claim(self):
        provider = self.make_provider()
        started = self.start(provider)
        claim_time = ACTION_TIME + timedelta(minutes=1)
        self.assertTrue(
            _claim_completion(
                db.session,
                verification_session_id=started.verification_session_id,
                claim_token=CLAIM_TOKEN,
                action_time=claim_time,
            )
        )

        with self.assertRaises(IdentityVerificationServiceError) as context:
            _handle_provider_completion_error(
                db.session,
                verification_session_id=started.verification_session_id,
                claim_token=OTHER_CLAIM_TOKEN,
                action_time=claim_time,
                provider_error_code=(
                    IdentityVerificationProviderErrorCode.VERIFICATION_PENDING
                ),
            )

        self.assertIs(
            context.exception.code,
            IdentityVerificationServiceErrorCode.COMPLETION_CONFLICT,
        )
        stored = self.get_session(started.verification_session_id)
        self.assertEqual(stored.status, "pending")
        self.assertEqual(stored.completion_claim_token, CLAIM_TOKEN)
        self.assertEqual(
            stored_as_utc(stored.completion_claimed_at),
            claim_time,
        )

    def test_explicit_provider_failure_becomes_failed_and_releases_claim(self):
        provider = self.make_provider(
            verification_error_code=(
                IdentityVerificationProviderErrorCode.VERIFICATION_FAILED
            )
        )
        started = self.start(provider)

        with self.assertRaises(IdentityVerificationServiceError) as context:
            self.complete(started, provider)

        self.assertIs(
            context.exception.code,
            IdentityVerificationServiceErrorCode.VERIFICATION_FAILED,
        )
        stored = self.get_session(started.verification_session_id)
        self.assertEqual(stored.status, "failed")
        self.assertEqual(stored.failure_code, "verification_failed")
        self.assertIsNone(stored.completion_claim_token)

    def test_age_restricted_becomes_terminal_age_requirement_failure(self):
        age_restricted = IdentityVerificationProviderError(
            IdentityVerificationProviderErrorCode.AGE_RESTRICTED
        )
        provider = self.make_provider(get_error=age_restricted)
        started = self.start(provider)

        with self.assertRaises(IdentityVerificationServiceError) as context:
            self.complete(started, provider)

        self.assertIs(
            context.exception.code,
            IdentityVerificationServiceErrorCode.AGE_REQUIREMENT_NOT_MET,
        )
        stored = self.get_session(started.verification_session_id)
        self.assertEqual(stored.status, "failed")
        self.assertEqual(stored.failure_code, "age_restricted")
        self.assertEqual(
            stored.provider_transaction_id,
            started.provider_transaction_id,
        )
        self.assertIsNone(stored.age_eligibility)
        self.assertIsNone(stored.identity_subject_digest)
        self.assertIsNone(stored.verified_at)
        self.assertIsNone(stored.completion_claim_token)
        self.assertIsNone(stored.completion_claimed_at)
        self.assertEqual(provider.get_call_count, 1)

        with self.assertRaises(IdentityVerificationServiceError) as repeated:
            self.complete(
                started,
                provider,
                claim_token=OTHER_CLAIM_TOKEN,
            )

        self.assertIs(
            repeated.exception.code,
            IdentityVerificationServiceErrorCode.AGE_REQUIREMENT_NOT_MET,
        )
        self.assertEqual(provider.get_call_count, 1)

    def test_provider_expiry_becomes_expired_and_releases_claim(self):
        provider = self.make_provider(
            verification_error_code=(
                IdentityVerificationProviderErrorCode.VERIFICATION_EXPIRED
            )
        )
        started = self.start(provider)

        with self.assertRaises(IdentityVerificationServiceError) as context:
            self.complete(started, provider)

        self.assertIs(
            context.exception.code,
            IdentityVerificationServiceErrorCode.VERIFICATION_EXPIRED,
        )
        stored = self.get_session(started.verification_session_id)
        self.assertEqual(stored.status, "expired")
        self.assertEqual(stored.failure_code, "verification_expired")
        self.assertIsNone(stored.completion_claim_token)

    def test_transaction_mismatch_is_terminal_invalid_provider_response(self):
        def mismatch(result):
            return VerifiedIdentityResult(
                provider=result.provider,
                provider_transaction_id="different-transaction",
                birth_date=result.birth_date,
                identity_subject=result.identity_subject,
            )

        provider = self.make_provider(result_transform=mismatch)
        started = self.start(provider)

        with self.assertRaises(IdentityVerificationServiceError) as context:
            self.complete(started, provider)

        self.assertIs(
            context.exception.code,
            IdentityVerificationServiceErrorCode.INVALID_PROVIDER_RESPONSE,
        )
        stored = self.get_session(started.verification_session_id)
        self.assertEqual(stored.status, "failed")
        self.assertEqual(stored.failure_code, "invalid_provider_response")
        self.assertIsNone(stored.identity_subject_digest)
        self.assertIsNone(stored.completion_claim_token)

    def test_unexpected_provider_exception_releases_owned_claim_and_reraises(self):
        provider = self.make_provider(get_error=RuntimeError("unexpected"))
        started = self.start(provider)

        with self.assertRaisesRegex(RuntimeError, "unexpected"):
            self.complete(started, provider)

        stored = self.get_session(started.verification_session_id)
        self.assertEqual(stored.status, "pending")
        self.assertIsNone(stored.completion_claim_token)
        self.assertIsNone(stored.completion_claimed_at)

    def test_completion_rejects_missing_session_provider_mismatch_and_naive_time(self):
        provider = self.make_provider()
        missing_id = self.next_public_id()

        with self.assertRaises(IdentityVerificationServiceError) as missing:
            complete_identity_verification(
                db.session,
                verification_session_id=missing_id,
                provider=provider,
                action_time=ACTION_TIME,
                identity_subject_hmac_key=HMAC_KEY,
            )
        self.assertIs(
            missing.exception.code,
            IdentityVerificationServiceErrorCode.SESSION_NOT_FOUND,
        )

        started = self.start(provider)
        db.session.execute(text("PRAGMA ignore_check_constraints=ON"))
        try:
            db.session.execute(
                update(IdentityVerificationSession)
                .where(
                    IdentityVerificationSession.verification_session_id
                    == started.verification_session_id
                )
                .values(provider="other")
                .execution_options(synchronize_session=False)
            )
            db.session.commit()
        finally:
            db.session.execute(text("PRAGMA ignore_check_constraints=OFF"))
            db.session.commit()
        with self.assertRaises(IdentityVerificationServiceError) as mismatch:
            self.complete(started, provider)
        self.assertIs(
            mismatch.exception.code,
            IdentityVerificationServiceErrorCode.PROVIDER_MISMATCH,
        )
        self.assertEqual(provider.get_call_count, 0)

        with self.assertRaises(ValueError):
            complete_identity_verification(
                db.session,
                verification_session_id=started.verification_session_id,
                provider=provider,
                action_time=datetime(2026, 10, 8),
                identity_subject_hmac_key=HMAC_KEY,
            )

    def test_invalid_hmac_key_is_rejected_before_claim_or_provider_call(self):
        for invalid_key in (b"", "not-bytes"):
            with self.subTest(invalid_key_type=type(invalid_key)):
                provider = self.make_provider()
                started = self.start(provider)
                with self.assertRaises(ValueError):
                    self.complete(started, provider, key=invalid_key)

                stored = self.get_session(started.verification_session_id)
                self.assertEqual(stored.status, "pending")
                self.assertIsNone(stored.completion_claim_token)
                self.assertEqual(provider.get_call_count, 0)

    def test_consumed_session_is_not_completed_or_queried_again(self):
        provider = self.make_provider()
        started = self.start(provider)
        self.complete(started, provider)
        db.session.execute(
            update(IdentityVerificationSession)
            .where(
                IdentityVerificationSession.verification_session_id
                == started.verification_session_id
            )
            .values(
                status="consumed",
                consumed_at=ACTION_TIME + timedelta(minutes=2),
            )
            .execution_options(synchronize_session=False)
        )
        db.session.commit()

        with self.assertRaises(IdentityVerificationServiceError) as context:
            self.complete(started, provider, claim_token=OTHER_CLAIM_TOKEN)

        self.assertIs(
            context.exception.code,
            IdentityVerificationServiceErrorCode.SESSION_CONSUMED,
        )
        self.assertEqual(provider.get_call_count, 1)

    def test_results_and_stable_errors_do_not_expose_sensitive_values(self):
        subject = "sensitive-identity-subject"
        provider = self.make_provider(identity_subject=subject)
        started = self.start(provider)
        authentication_url = started.authentication_url
        provider_transaction_id = started.provider_transaction_id
        completed = self.complete(started, provider)
        stored = self.get_session(started.verification_session_id)

        self.assertNotIn(authentication_url, repr(started))
        self.assertNotIn(provider_transaction_id, repr(started))
        self.assertNotIn(subject, repr(completed))
        self.assertNotIn(stored.identity_subject_digest, repr(completed))
        self.assertNotIn(stored.provider_transaction_id, repr(completed))
        self.assertNotIn(HMAC_KEY.decode("ascii"), repr(completed))

        failed_provider = self.make_provider(
            identity_subject=subject,
            verification_error_code=(
                IdentityVerificationProviderErrorCode.VERIFICATION_FAILED
            ),
        )
        failed_started = self.start(failed_provider)
        with self.assertRaises(IdentityVerificationServiceError) as context:
            self.complete(
                failed_started,
                failed_provider,
                key=b"another-synthetic-secret",
                claim_token=OTHER_CLAIM_TOKEN,
            )
        error_text = repr(context.exception)
        self.assertNotIn(subject, error_text)
        self.assertNotIn("another-synthetic-secret", error_text)
        self.assertNotIn("provider-transaction", error_text)

    def test_orchestration_does_not_create_users_or_consume_verified_session(self):
        provider = self.make_provider()
        started = self.start(provider)

        self.complete(started, provider)

        stored = self.get_session(started.verification_session_id)
        self.assertEqual(stored.status, "verified")
        self.assertIsNone(stored.consumed_at)
        self.assertEqual(
            db.session.scalar(select(func.count()).select_from(User)),
            0,
        )


if __name__ == "__main__":
    unittest.main()
