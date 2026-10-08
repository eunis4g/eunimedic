import re
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from flask import g
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from werkzeug.security import check_password_hash, generate_password_hash

import app as app_module
from models import PendingRegistration, User, db
from services.email_service import EmailServiceError


AUTH_TIME = datetime(2026, 10, 8, 6, 0, tzinfo=UTC)
VALID_PASSWORD = "SafePass1!"


class WebAuthRouteTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.temporary_directory = tempfile.TemporaryDirectory()
        cls.database_path = (
            Path(cls.temporary_directory.name) / "web-auth-route-test.db"
        )
        cls.application = app_module.app
        cls.original_testing = cls.application.config["TESTING"]
        cls.application.config.update(TESTING=True)

        with cls.application.app_context():
            db.session.remove()
            cls.original_engine = db.engines[None]
            cls.test_engine = create_engine(
                "sqlite:///" + cls.database_path.as_posix()
            )
            db.engines[None] = cls.test_engine

    @classmethod
    def tearDownClass(cls):
        with cls.application.app_context():
            db.session.remove()
            db.drop_all()
            db.session.remove()
            db.engines[None] = cls.original_engine

        cls.application.config["TESTING"] = cls.original_testing
        cls.test_engine.dispose()
        cls.temporary_directory.cleanup()

    def setUp(self):
        self.application_context = self.application.app_context()
        self.application_context.push()
        db.session.remove()
        db.drop_all()
        db.create_all()
        self.client = self.application.test_client()
        g.pop("csrf_token", None)

    def tearDown(self):
        db.session.remove()
        self.application_context.pop()

    def get_csrf_token(self, url):
        response = self.client.get(url)
        match = re.search(
            rb'name="csrf_token"\s+value="([^"]+)"',
            response.data,
        )

        self.assertIsNotNone(match)
        return match.group(1).decode("utf-8")

    def post_with_csrf(self, url, data, *, csrf_url=None):
        payload = dict(data)
        payload["csrf_token"] = self.get_csrf_token(csrf_url or url)

        return self.client.post(url, data=payload)

    def valid_registration_data(self, **overrides):
        data = {
            "username": "member01",
            "email": "member01@example.com",
            "password": VALID_PASSWORD,
            "password_confirm": VALID_PASSWORD,
        }
        data.update(overrides)
        return data

    def add_user(
        self,
        *,
        username="member01",
        email="member01@example.com",
        password=VALID_PASSWORD,
        is_active=True,
    ):
        user = User(
            username=username,
            email=email,
            password_hash=generate_password_hash(password),
            is_active=is_active,
        )
        db.session.add(user)
        db.session.commit()
        return user

    def add_pending_registration(
        self,
        *,
        token="pending-secret-token",
        code="123456",
        username="pending01",
        email="pending01@example.com",
        password=VALID_PASSWORD,
        created_at=None,
        expires_at=None,
        attempt_count=0,
        resend_count=0,
        last_sent_at=None,
    ):
        created_at = created_at or (AUTH_TIME - timedelta(minutes=1))
        pending = PendingRegistration(
            username=username,
            email=email,
            password_hash=generate_password_hash(password),
            verification_code_hash=app_module.hash_verification_code(
                token,
                code,
            ),
            verification_token_hash=app_module.hash_verification_token(
                token
            ),
            expires_at=expires_at or (AUTH_TIME + timedelta(minutes=5)),
            attempt_count=attempt_count,
            resend_count=resend_count,
            last_sent_at=(
                last_sent_at
                or (AUTH_TIME - app_module.RESEND_COOLDOWN)
            ),
            created_at=created_at,
            updated_at=created_at,
        )
        db.session.add(pending)
        db.session.commit()
        return pending

    def log_in_session(self, user):
        with self.client.session_transaction() as session:
            session["_user_id"] = str(user.user_id)
            session["_fresh"] = True

        g.pop("_login_user", None)

    def reset_test_database(self):
        db.session.remove()
        db.drop_all()
        db.create_all()
        self.client = self.application.test_client()
        g.pop("_login_user", None)
        g.pop("csrf_token", None)

    def test_auth_policy_constants_and_session_cookie_settings(self):
        self.assertEqual(
            app_module.VERIFICATION_CODE_LIFETIME,
            timedelta(minutes=5),
        )
        self.assertEqual(
            app_module.PENDING_REGISTRATION_LIFETIME,
            timedelta(minutes=30),
        )
        self.assertEqual(
            app_module.RESEND_COOLDOWN,
            timedelta(seconds=60),
        )
        self.assertEqual(app_module.MAX_VERIFICATION_ATTEMPTS, 5)
        self.assertEqual(app_module.MAX_RESEND_COUNT, 4)
        self.assertTrue(self.application.config["SESSION_COOKIE_HTTPONLY"])
        self.assertEqual(
            self.application.config["SESSION_COOKIE_SAMESITE"],
            "Lax",
        )
        self.assertFalse(self.application.config["SESSION_COOKIE_SECURE"])

        response = self.client.get("/login")
        set_cookie = response.headers.get("Set-Cookie", "")

        self.assertIn("HttpOnly", set_cookie)
        self.assertIn("SameSite=Lax", set_cookie)
        self.assertNotIn("Secure", set_cookie)

    def test_register_creates_only_hashed_pending_registration_and_sends_mail(self):
        submitted_email = "  MEMBER01@EXAMPLE.COM  "

        with patch.object(
            app_module,
            "send_verification_email",
        ) as send_verification_email:
            response = self.post_with_csrf(
                "/register",
                self.valid_registration_data(email=submitted_email),
            )

        self.assertEqual(response.status_code, 302)
        self.assertRegex(
            response.headers["Location"],
            r"/verify-email/[^/]+$",
        )
        self.assertEqual(db.session.query(User).count(), 0)
        self.assertEqual(db.session.query(PendingRegistration).count(), 1)

        pending = db.session.scalar(db.select(PendingRegistration))
        verification_token = response.headers["Location"].rsplit("/", 1)[1]
        sent_email, verification_code = (
            send_verification_email.call_args.args
        )

        self.assertEqual(pending.username, "member01")
        self.assertEqual(pending.email, "member01@example.com")
        self.assertEqual(sent_email, "member01@example.com")
        self.assertRegex(verification_code, r"^[0-9]{6}$")
        self.assertNotEqual(pending.password_hash, VALID_PASSWORD)
        self.assertTrue(
            check_password_hash(pending.password_hash, VALID_PASSWORD)
        )
        self.assertNotEqual(
            pending.verification_code_hash,
            verification_code,
        )
        self.assertNotEqual(
            pending.verification_token_hash,
            verification_token,
        )
        self.assertEqual(
            pending.verification_code_hash,
            app_module.hash_verification_code(
                verification_token,
                verification_code,
            ),
        )
        self.assertEqual(
            pending.verification_token_hash,
            app_module.hash_verification_token(verification_token),
        )
        send_verification_email.assert_called_once()

    def test_register_validation_rejects_invalid_fields_without_writes(self):
        invalid_cases = {
            "missing_username": {"username": ""},
            "missing_email": {"email": ""},
            "missing_password": {"password": ""},
            "missing_confirmation": {"password_confirm": ""},
            "short_username": {"username": "abc"},
            "long_username": {"username": "a" * 21},
            "uppercase_username": {"username": "Member01"},
            "username_without_letter": {"username": "1234"},
            "invalid_email": {"email": "not-an-email"},
            "long_email": {"email": f"{'a' * 310}@example.com"},
            "short_password": {
                "password": "Aa1!aaa",
                "password_confirm": "Aa1!aaa",
            },
            "long_password": {
                "password": "Aa1!" + ("a" * 17),
                "password_confirm": "Aa1!" + ("a" * 17),
            },
            "password_with_space": {
                "password": "Safe 1!a",
                "password_confirm": "Safe 1!a",
            },
            "password_with_disallowed_character": {
                "password": "SafePass1!/",
                "password_confirm": "SafePass1!/",
            },
            "password_without_letter": {
                "password": "1234567!",
                "password_confirm": "1234567!",
            },
            "password_without_digit": {
                "password": "SafePass!",
                "password_confirm": "SafePass!",
            },
            "password_without_special": {
                "password": "SafePass1",
                "password_confirm": "SafePass1",
            },
            "password_confirmation_mismatch": {
                "password_confirm": "OtherPass1!",
            },
        }

        with patch.object(
            app_module,
            "send_verification_email",
        ) as send_verification_email:
            for label, overrides in invalid_cases.items():
                with self.subTest(label=label):
                    response = self.post_with_csrf(
                        "/register",
                        self.valid_registration_data(**overrides),
                    )

                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(db.session.query(User).count(), 0)
                    self.assertEqual(
                        db.session.query(PendingRegistration).count(),
                        0,
                    )

        send_verification_email.assert_not_called()

    def test_register_rejects_existing_user_username_and_email(self):
        self.add_user(username="taken01", email="taken@example.com")
        cases = (
            {
                "username": "taken01",
                "email": "fresh01@example.com",
            },
            {
                "username": "fresh01",
                "email": "taken@example.com",
            },
        )

        with patch.object(
            app_module,
            "send_verification_email",
        ) as send_verification_email:
            for overrides in cases:
                with self.subTest(overrides=overrides):
                    response = self.post_with_csrf(
                        "/register",
                        self.valid_registration_data(**overrides),
                    )
                    self.assertEqual(response.status_code, 200)

        self.assertEqual(db.session.query(User).count(), 1)
        self.assertEqual(db.session.query(PendingRegistration).count(), 0)
        send_verification_email.assert_not_called()

    def test_register_rejects_pending_username_and_email_conflicts(self):
        self.add_pending_registration(
            token="pending-one",
            username="pending01",
            email="first-pending@example.com",
        )
        self.add_pending_registration(
            token="pending-two",
            username="pending02",
            email="second-pending@example.com",
        )
        cases = (
            {
                "username": "pending01",
                "email": "fresh01@example.com",
            },
            {
                "username": "fresh01",
                "email": "second-pending@example.com",
            },
        )

        with patch.object(
            app_module,
            "send_verification_email",
        ) as send_verification_email:
            with patch.object(
                app_module,
                "utc_now",
                return_value=AUTH_TIME,
            ):
                for overrides in cases:
                    with self.subTest(overrides=overrides):
                        response = self.post_with_csrf(
                            "/register",
                            self.valid_registration_data(**overrides),
                        )
                        self.assertEqual(response.status_code, 200)

        self.assertEqual(db.session.query(User).count(), 0)
        self.assertEqual(db.session.query(PendingRegistration).count(), 2)
        send_verification_email.assert_not_called()

    def test_register_smtp_failure_rolls_back_pending_registration(self):
        with patch.object(
            app_module,
            "send_verification_email",
            side_effect=EmailServiceError("forced test failure"),
        ) as send_verification_email:
            response = self.post_with_csrf(
                "/register",
                self.valid_registration_data(),
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(db.session.query(User).count(), 0)
        self.assertEqual(db.session.query(PendingRegistration).count(), 0)
        send_verification_email.assert_called_once()

    def test_verify_success_creates_one_user_removes_pending_and_requires_login(self):
        token = "verification-success-token"
        code = "123456"
        pending = self.add_pending_registration(token=token, code=code)
        original_password_hash = pending.password_hash

        with patch.object(app_module, "utc_now", return_value=AUTH_TIME):
            response = self.post_with_csrf(
                f"/verify-email/{token}",
                {"verification_code": code},
            )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/login"))
        self.assertEqual(db.session.query(User).count(), 1)
        self.assertEqual(db.session.query(PendingRegistration).count(), 0)

        user = db.session.scalar(db.select(User))
        self.assertEqual(user.username, pending.username)
        self.assertEqual(user.email, pending.email)
        self.assertEqual(user.password_hash, original_password_hash)
        self.assertTrue(check_password_hash(user.password_hash, VALID_PASSWORD))

        with self.client.session_transaction() as session:
            self.assertNotIn("_user_id", session)

        protected_response = self.client.get("/today-medications")
        self.assertEqual(protected_response.status_code, 302)
        self.assertIn("/login", protected_response.headers["Location"])

        csrf_token = self.get_csrf_token("/login")
        repeated_response = self.client.post(
            f"/verify-email/{token}",
            data={
                "csrf_token": csrf_token,
                "verification_code": code,
            },
        )
        self.assertEqual(repeated_response.status_code, 302)
        self.assertTrue(
            repeated_response.headers["Location"].endswith("/register")
        )
        self.assertEqual(db.session.query(User).count(), 1)

    def test_verify_rejects_wrong_codes_increments_and_enforces_attempt_limit(self):
        token = "attempt-limit-token"
        code = "123456"
        pending = self.add_pending_registration(token=token, code=code)

        with patch.object(app_module, "utc_now", return_value=AUTH_TIME):
            csrf_token = self.get_csrf_token(f"/verify-email/{token}")

            for expected_attempt_count in range(1, 6):
                response = self.client.post(
                    f"/verify-email/{token}",
                    data={
                        "csrf_token": csrf_token,
                        "verification_code": "000000",
                    },
                )
                self.assertEqual(response.status_code, 200)
                db.session.refresh(pending)
                self.assertEqual(
                    pending.attempt_count,
                    expected_attempt_count,
                )

            blocked_response = self.client.post(
                f"/verify-email/{token}",
                data={
                    "csrf_token": csrf_token,
                    "verification_code": code,
                },
            )

        self.assertEqual(blocked_response.status_code, 200)
        db.session.refresh(pending)
        self.assertEqual(pending.attempt_count, 5)
        self.assertEqual(db.session.query(User).count(), 0)
        self.assertEqual(db.session.query(PendingRegistration).count(), 1)

    def test_verification_code_expiration_boundary_is_strictly_greater_than(self):
        cases = (
            ("just_before", timedelta(microseconds=1), True),
            ("exact_expiration", timedelta(0), True),
            ("just_after", -timedelta(microseconds=1), False),
        )

        for index, (label, expiry_delta, should_succeed) in enumerate(cases):
            with self.subTest(label=label):
                token = f"code-boundary-token-{index}"
                code = "123456"
                pending = self.add_pending_registration(
                    token=token,
                    code=code,
                    username=f"boundary{index}",
                    email=f"boundary{index}@example.com",
                    expires_at=AUTH_TIME + expiry_delta,
                )

                with patch.object(
                    app_module,
                    "utc_now",
                    return_value=AUTH_TIME,
                ):
                    response = self.post_with_csrf(
                        f"/verify-email/{token}",
                        {"verification_code": code},
                    )

                if should_succeed:
                    self.assertEqual(response.status_code, 302)
                    self.assertEqual(db.session.query(User).count(), 1)
                    self.assertEqual(
                        db.session.query(PendingRegistration).count(),
                        0,
                    )
                else:
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(db.session.query(User).count(), 0)
                    self.assertEqual(
                        db.session.query(PendingRegistration).count(),
                        1,
                    )
                    db.session.refresh(pending)
                    self.assertEqual(pending.attempt_count, 0)

                self.reset_test_database()

    def test_pending_registration_expiration_is_separate_and_half_open(self):
        cases = (
            (
                "just_before",
                AUTH_TIME
                - app_module.PENDING_REGISTRATION_LIFETIME
                + timedelta(microseconds=1),
                False,
            ),
            (
                "exact_expiration",
                AUTH_TIME - app_module.PENDING_REGISTRATION_LIFETIME,
                True,
            ),
            (
                "just_after",
                AUTH_TIME
                - app_module.PENDING_REGISTRATION_LIFETIME
                - timedelta(microseconds=1),
                True,
            ),
        )

        for index, (label, created_at, should_expire) in enumerate(cases):
            with self.subTest(label=label):
                token = f"pending-boundary-token-{index}"
                self.add_pending_registration(
                    token=token,
                    username=f"pending{index}",
                    email=f"pending-boundary-{index}@example.com",
                    created_at=created_at,
                    expires_at=AUTH_TIME + timedelta(minutes=5),
                )

                with patch.object(
                    app_module,
                    "utc_now",
                    return_value=AUTH_TIME,
                ):
                    response = self.client.get(
                        f"/verify-email/{token}"
                    )

                if should_expire:
                    self.assertEqual(response.status_code, 302)
                    self.assertTrue(
                        response.headers["Location"].endswith("/register")
                    )
                    self.assertEqual(
                        db.session.query(PendingRegistration).count(),
                        0,
                    )
                else:
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(
                        db.session.query(PendingRegistration).count(),
                        1,
                    )

                self.reset_test_database()

    def test_expired_code_does_not_delete_unexpired_pending_registration(self):
        token = "expired-code-active-pending"
        pending = self.add_pending_registration(
            token=token,
            expires_at=AUTH_TIME - timedelta(seconds=1),
            created_at=AUTH_TIME - timedelta(minutes=5),
        )

        with patch.object(app_module, "utc_now", return_value=AUTH_TIME):
            response = self.post_with_csrf(
                f"/verify-email/{token}",
                {"verification_code": "123456"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(db.session.query(User).count(), 0)
        self.assertEqual(db.session.query(PendingRegistration).count(), 1)
        db.session.refresh(pending)
        self.assertEqual(pending.attempt_count, 0)

    def test_verify_duplicate_user_race_does_not_create_or_remove_rows(self):
        token = "duplicate-user-token"
        pending = self.add_pending_registration(
            token=token,
            username="raceuser",
            email="race-pending@example.com",
        )
        existing_user = self.add_user(
            username="raceuser",
            email="existing@example.com",
        )

        with patch.object(app_module, "utc_now", return_value=AUTH_TIME):
            response = self.post_with_csrf(
                f"/verify-email/{token}",
                {"verification_code": "123456"},
            )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/register"))
        self.assertEqual(db.session.query(User).count(), 1)
        self.assertEqual(db.session.query(PendingRegistration).count(), 1)
        self.assertEqual(db.session.get(User, existing_user.user_id).username, "raceuser")
        self.assertIsNotNone(
            db.session.get(
                PendingRegistration,
                pending.pending_registration_id,
            )
        )

    def test_resend_at_cooldown_boundary_rotates_code_and_resets_attempts(self):
        token = "resend-success-token"
        pending = self.add_pending_registration(
            token=token,
            attempt_count=4,
            resend_count=1,
            last_sent_at=AUTH_TIME - app_module.RESEND_COOLDOWN,
        )
        old_code_hash = pending.verification_code_hash
        old_token_hash = pending.verification_token_hash

        with (
            patch.object(app_module, "utc_now", return_value=AUTH_TIME),
            patch.object(app_module.secrets, "randbelow", return_value=654321),
            patch.object(
                app_module,
                "send_verification_email",
            ) as send_verification_email,
        ):
            response = self.post_with_csrf(
                f"/verify-email/{token}/resend",
                {},
                csrf_url=f"/verify-email/{token}",
            )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            response.headers["Location"].endswith(
                f"/verify-email/{token}"
            )
        )
        db.session.refresh(pending)
        self.assertEqual(pending.attempt_count, 0)
        self.assertEqual(pending.resend_count, 2)
        self.assertEqual(app_module.as_utc(pending.last_sent_at), AUTH_TIME)
        self.assertEqual(
            app_module.as_utc(pending.expires_at),
            AUTH_TIME + app_module.VERIFICATION_CODE_LIFETIME,
        )
        self.assertNotEqual(pending.verification_code_hash, old_code_hash)
        self.assertEqual(pending.verification_token_hash, old_token_hash)
        self.assertEqual(
            pending.verification_code_hash,
            app_module.hash_verification_code(token, "654321"),
        )
        send_verification_email.assert_called_once_with(
            pending.email,
            "654321",
        )

    def test_resend_rejects_request_before_cooldown_without_changes(self):
        token = "resend-cooldown-token"
        last_sent_at = (
            AUTH_TIME
            - app_module.RESEND_COOLDOWN
            + timedelta(microseconds=1)
        )
        pending = self.add_pending_registration(
            token=token,
            attempt_count=3,
            resend_count=2,
            last_sent_at=last_sent_at,
        )
        old_values = (
            pending.verification_code_hash,
            pending.expires_at,
            pending.attempt_count,
            pending.resend_count,
            pending.last_sent_at,
        )

        with (
            patch.object(app_module, "utc_now", return_value=AUTH_TIME),
            patch.object(
                app_module,
                "send_verification_email",
            ) as send_verification_email,
        ):
            response = self.post_with_csrf(
                f"/verify-email/{token}/resend",
                {},
                csrf_url=f"/verify-email/{token}",
            )

        self.assertEqual(response.status_code, 302)
        db.session.refresh(pending)
        self.assertEqual(
            (
                pending.verification_code_hash,
                pending.expires_at,
                pending.attempt_count,
                pending.resend_count,
                pending.last_sent_at,
            ),
            old_values,
        )
        send_verification_email.assert_not_called()

    def test_resend_limit_is_enforced_without_smtp(self):
        token = "resend-limit-token"
        pending = self.add_pending_registration(
            token=token,
            resend_count=app_module.MAX_RESEND_COUNT,
            last_sent_at=AUTH_TIME - timedelta(minutes=10),
        )

        with (
            patch.object(app_module, "utc_now", return_value=AUTH_TIME),
            patch.object(
                app_module,
                "send_verification_email",
            ) as send_verification_email,
        ):
            response = self.post_with_csrf(
                f"/verify-email/{token}/resend",
                {},
                csrf_url=f"/verify-email/{token}",
            )

        self.assertEqual(response.status_code, 302)
        db.session.refresh(pending)
        self.assertEqual(pending.resend_count, 4)
        send_verification_email.assert_not_called()

    def test_resend_smtp_failure_rolls_back_rotated_code_and_counters(self):
        token = "resend-failure-token"
        pending = self.add_pending_registration(
            token=token,
            attempt_count=3,
            resend_count=1,
            last_sent_at=AUTH_TIME - timedelta(minutes=10),
        )
        old_values = (
            pending.verification_code_hash,
            pending.expires_at,
            pending.attempt_count,
            pending.resend_count,
            pending.last_sent_at,
        )

        with (
            patch.object(app_module, "utc_now", return_value=AUTH_TIME),
            patch.object(app_module.secrets, "randbelow", return_value=654321),
            patch.object(
                app_module,
                "send_verification_email",
                side_effect=EmailServiceError("forced test failure"),
            ) as send_verification_email,
        ):
            response = self.post_with_csrf(
                f"/verify-email/{token}/resend",
                {},
                csrf_url=f"/verify-email/{token}",
            )

        self.assertEqual(response.status_code, 302)
        db.session.refresh(pending)
        self.assertEqual(
            (
                pending.verification_code_hash,
                pending.expires_at,
                pending.attempt_count,
                pending.resend_count,
                pending.last_sent_at,
            ),
            old_values,
        )
        send_verification_email.assert_called_once()

    def test_login_uses_username_and_password_hash_and_sets_flask_session(self):
        user = self.add_user()

        response = self.post_with_csrf(
            "/login",
            {
                "username": user.username,
                "password": VALID_PASSWORD,
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/"))
        self.assertNotEqual(user.password_hash, VALID_PASSWORD)
        self.assertTrue(check_password_hash(user.password_hash, VALID_PASSWORD))

        with self.client.session_transaction() as session:
            self.assertEqual(session.get("_user_id"), str(user.user_id))
            self.assertTrue(session.get("_fresh"))

        self.assertNotIn(VALID_PASSWORD.encode("utf-8"), response.data)
        self.assertNotIn(user.password_hash.encode("utf-8"), response.data)

    def test_login_rejects_unknown_wrong_inactive_and_email_identifiers(self):
        active_user = self.add_user()
        inactive_user = self.add_user(
            username="inactive01",
            email="inactive@example.com",
            is_active=False,
        )
        cases = (
            ("unknown", "unknown01", VALID_PASSWORD),
            ("wrong_password", active_user.username, "WrongPass1!"),
            ("inactive", inactive_user.username, VALID_PASSWORD),
            ("email_identifier", active_user.email, VALID_PASSWORD),
        )

        for label, username, password in cases:
            with self.subTest(label=label):
                self.client = self.application.test_client()
                g.pop("_login_user", None)
                g.pop("csrf_token", None)
                response = self.post_with_csrf(
                    "/login",
                    {"username": username, "password": password},
                )

                self.assertEqual(response.status_code, 200)
                with self.client.session_transaction() as session:
                    self.assertNotIn("_user_id", session)
                self.assertNotIn(password.encode("utf-8"), response.data)

    def test_logout_is_post_only_login_required_and_clears_session(self):
        user = self.add_user()
        self.log_in_session(user)

        get_response = self.client.get("/logout")
        self.assertEqual(get_response.status_code, 405)

        missing_csrf_response = self.client.post("/logout")
        self.assertEqual(missing_csrf_response.status_code, 400)
        with self.client.session_transaction() as session:
            self.assertEqual(session.get("_user_id"), str(user.user_id))

        response = self.post_with_csrf(
            "/logout",
            {},
            csrf_url="/",
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/"))
        with self.client.session_transaction() as session:
            self.assertNotIn("_user_id", session)

        csrf_token = self.get_csrf_token("/login")
        anonymous_response = self.client.post(
            "/logout",
            data={"csrf_token": csrf_token},
        )
        self.assertEqual(anonymous_response.status_code, 302)
        self.assertIn("/login", anonymous_response.headers["Location"])

    def test_all_auth_post_routes_reject_missing_csrf_before_side_effects(self):
        user = self.add_user()
        token = "csrf-pending-token"
        pending = self.add_pending_registration(token=token)

        with patch.object(
            app_module,
            "send_verification_email",
        ) as send_verification_email:
            responses = {
                "register": self.client.post(
                    "/register",
                    data=self.valid_registration_data(
                        username="newmember",
                        email="newmember@example.com",
                    ),
                ),
                "login": self.client.post(
                    "/login",
                    data={
                        "username": user.username,
                        "password": VALID_PASSWORD,
                    },
                ),
                "verify": self.client.post(
                    f"/verify-email/{token}",
                    data={"verification_code": "123456"},
                ),
                "resend": self.client.post(
                    f"/verify-email/{token}/resend",
                    data={},
                ),
            }

            self.log_in_session(user)
            responses["logout"] = self.client.post("/logout")

        for route_name, response in responses.items():
            with self.subTest(route_name=route_name):
                self.assertEqual(response.status_code, 400)

        db.session.refresh(pending)
        self.assertEqual(pending.attempt_count, 0)
        self.assertEqual(pending.resend_count, 0)
        self.assertEqual(db.session.query(User).count(), 1)
        self.assertEqual(db.session.query(PendingRegistration).count(), 1)
        send_verification_email.assert_not_called()
        with self.client.session_transaction() as session:
            self.assertEqual(session.get("_user_id"), str(user.user_id))

    def test_user_and_pending_unique_constraints_protect_identity_fields(self):
        self.add_user(username="unique01", email="unique@example.com")

        duplicate_users = (
            User(
                username="unique01",
                email="other@example.com",
                password_hash="hash",
            ),
            User(
                username="other01",
                email="unique@example.com",
                password_hash="hash",
            ),
        )
        for duplicate_user in duplicate_users:
            with self.subTest(user=duplicate_user.username):
                db.session.add(duplicate_user)
                with self.assertRaises(IntegrityError):
                    db.session.commit()
                db.session.rollback()

        self.add_pending_registration(
            token="unique-pending-token",
            username="pending01",
            email="pending@example.com",
        )
        duplicate_pending_rows = (
            {
                "token": "duplicate-pending-username-token",
                "username": "pending01",
                "email": "other-pending@example.com",
            },
            {
                "token": "duplicate-pending-email-token",
                "username": "other02",
                "email": "pending@example.com",
            },
        )

        for values in duplicate_pending_rows:
            with self.subTest(values=values):
                duplicate = PendingRegistration(
                    username=values["username"],
                    email=values["email"],
                    password_hash="hash",
                    verification_code_hash=app_module.hash_verification_code(
                        values["token"],
                        "123456",
                    ),
                    verification_token_hash=(
                        app_module.hash_verification_token(values["token"])
                    ),
                    expires_at=AUTH_TIME + timedelta(minutes=5),
                    attempt_count=0,
                    resend_count=0,
                    last_sent_at=AUTH_TIME,
                )
                db.session.add(duplicate)
                with self.assertRaises(IntegrityError):
                    db.session.commit()
                db.session.rollback()

    def test_verification_token_is_redacted_from_werkzeug_log_paths(self):
        raw_path = "POST /verify-email/plain-secret-token/resend HTTP/1.1"
        redacted = app_module.VerificationTokenLogFilter.redact_token(
            raw_path
        )

        self.assertNotIn("plain-secret-token", redacted)
        self.assertIn("/verify-email/[REDACTED]/resend", redacted)


if __name__ == "__main__":
    unittest.main()
