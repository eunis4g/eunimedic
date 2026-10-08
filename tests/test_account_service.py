import unittest

from services.account_service import (
    ALLOWED_SPECIAL_CHARACTERS,
    AccountValidationError,
    normalize_and_validate_email,
    normalize_email,
    validate_password,
    validate_username,
)


class AccountServiceTest(unittest.TestCase):

    def assert_validation_error(
        self,
        function,
        value,
        *,
        field,
        code,
        message,
    ):
        with self.assertRaises(AccountValidationError) as context:
            function(value)

        self.assertEqual(context.exception.field, field)
        self.assertEqual(context.exception.code, code)
        self.assertEqual(str(context.exception), message)
        if value:
            self.assertNotIn(value, repr(context.exception))

    def test_username_valid_values_are_returned_without_normalization(self):
        self.assertEqual(validate_username("a123"), "a123")
        maximum_username = "a" + ("1" * 19)
        self.assertEqual(
            validate_username(maximum_username),
            maximum_username,
        )

    def test_username_required_and_length_boundaries(self):
        cases = (
            (
                "",
                "USERNAME_REQUIRED",
                "모든 항목을 입력해주세요.",
            ),
            (
                "a12",
                "USERNAME_INVALID",
                "아이디는 4자 이상이어야 합니다.",
            ),
            (
                "a" + ("1" * 20),
                "USERNAME_INVALID",
                "아이디는 20자 이하여야 합니다.",
            ),
        )

        for value, code, message in cases:
            with self.subTest(value=value):
                self.assert_validation_error(
                    validate_username,
                    value,
                    field="username",
                    code=code,
                    message=message,
                )

    def test_username_rejects_invalid_characters_and_digit_only_values(self):
        cases = (
            (
                "Member01",
                "아이디는 영문 소문자와 숫자만 사용할 수 있습니다.",
            ),
            (
                "user_01",
                "아이디는 영문 소문자와 숫자만 사용할 수 있습니다.",
            ),
            (
                "1234",
                "아이디에는 영문 소문자가 1개 이상 포함되어야 합니다.",
            ),
        )

        for value, message in cases:
            with self.subTest(value=value):
                self.assert_validation_error(
                    validate_username,
                    value,
                    field="username",
                    code="USERNAME_INVALID",
                    message=message,
                )

    def test_username_does_not_trim_outer_whitespace(self):
        self.assert_validation_error(
            validate_username,
            " user01 ",
            field="username",
            code="USERNAME_INVALID",
            message="아이디는 영문 소문자와 숫자만 사용할 수 있습니다.",
        )

    def test_email_is_trimmed_and_lowercased(self):
        self.assertEqual(
            normalize_email("  MEMBER01@EXAMPLE.COM  "),
            "member01@example.com",
        )
        self.assertEqual(
            normalize_and_validate_email("  MEMBER01@EXAMPLE.COM  "),
            "member01@example.com",
        )

    def test_email_required_and_length_boundaries(self):
        maximum_email = ("a" * 308) + "@example.com"
        self.assertEqual(len(maximum_email), 320)
        self.assertEqual(
            normalize_and_validate_email(maximum_email),
            maximum_email,
        )

        cases = (
            (
                "   ",
                "EMAIL_REQUIRED",
                "모든 항목을 입력해주세요.",
            ),
            (
                ("a" * 309) + "@example.com",
                "EMAIL_INVALID",
                "이메일은 320자 이하로 입력해주세요.",
            ),
        )

        for value, code, message in cases:
            with self.subTest(length=len(value)):
                self.assert_validation_error(
                    normalize_and_validate_email,
                    value,
                    field="email",
                    code=code,
                    message=message,
                )

    def test_email_rejects_the_existing_invalid_formats(self):
        invalid_emails = (
            "member example@example.com",
            "member.example.com",
            "member@@example.com",
            "@example.com",
            "member@",
            "member@examplecom",
            "member@.example.com",
            "member@example.com.",
        )

        for value in invalid_emails:
            with self.subTest(value=value):
                self.assert_validation_error(
                    normalize_and_validate_email,
                    value,
                    field="email",
                    code="EMAIL_INVALID",
                    message="올바른 이메일 형식을 입력해주세요.",
                )

    def test_password_valid_values_are_returned_without_normalization(self):
        minimum_password = "Aa12345!"
        maximum_password = "Aa1!" + ("x" * 16)

        self.assertEqual(
            validate_password(minimum_password),
            minimum_password,
        )
        self.assertEqual(len(maximum_password), 20)
        self.assertEqual(
            validate_password(maximum_password),
            maximum_password,
        )

    def test_password_required_and_length_boundaries(self):
        cases = (
            (
                "",
                "PASSWORD_REQUIRED",
                "모든 항목을 입력해주세요.",
            ),
            (
                "Aa123!x",
                "PASSWORD_INVALID",
                "비밀번호는 8자 이상이어야 합니다.",
            ),
            (
                "Aa1!" + ("x" * 17),
                "PASSWORD_INVALID",
                "비밀번호는 20자 이하여야 합니다.",
            ),
        )

        for value, code, message in cases:
            with self.subTest(length=len(value)):
                self.assert_validation_error(
                    validate_password,
                    value,
                    field="password",
                    code=code,
                    message=message,
                )

    def test_password_rejects_whitespace_and_disallowed_characters(self):
        cases = (
            (
                "Safe 1!a",
                "비밀번호에는 공백을 사용할 수 없습니다.",
            ),
            (
                "SafePass1/",
                "비밀번호에는 영문자, 숫자와 안내된 특수문자만 "
                "사용할 수 있습니다.",
            ),
        )

        for value, message in cases:
            with self.subTest(value=value):
                self.assert_validation_error(
                    validate_password,
                    value,
                    field="password",
                    code="PASSWORD_INVALID",
                    message=message,
                )

    def test_password_requires_letter_digit_and_allowed_special_character(self):
        cases = (
            (
                "1234567!",
                "비밀번호에는 영문자가 1개 이상 포함되어야 합니다.",
            ),
            (
                "SafePass!",
                "비밀번호에는 숫자가 1개 이상 포함되어야 합니다.",
            ),
            (
                "SafePass1",
                "비밀번호에는 특수문자가 1개 이상 포함되어야 합니다.",
            ),
        )

        for value, message in cases:
            with self.subTest(value=value):
                self.assert_validation_error(
                    validate_password,
                    value,
                    field="password",
                    code="PASSWORD_INVALID",
                    message=message,
                )

    def test_allowed_special_character_policy_is_unchanged(self):
        self.assertEqual(
            ALLOWED_SPECIAL_CHARACTERS,
            "!@#$%^&*()_-+=?.,",
        )


if __name__ == "__main__":
    unittest.main()
