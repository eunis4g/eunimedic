ALLOWED_SPECIAL_CHARACTERS = "!@#$%^&*()_-+=?.,"


class AccountValidationError(ValueError):
    """Describe an account-field validation failure without HTTP concerns."""

    def __init__(self, *, field, code, message):
        super().__init__(message)
        self.field = field
        self.code = code


def normalize_email(email):
    """Apply the email normalization used by account registration."""

    return email.strip().lower()


def validate_username(username):
    """Validate a username and return it unchanged."""

    if not username:
        raise AccountValidationError(
            field="username",
            code="USERNAME_REQUIRED",
            message="모든 항목을 입력해주세요.",
        )

    if len(username) < 4:
        raise AccountValidationError(
            field="username",
            code="USERNAME_INVALID",
            message="아이디는 4자 이상이어야 합니다.",
        )

    if len(username) > 20:
        raise AccountValidationError(
            field="username",
            code="USERNAME_INVALID",
            message="아이디는 20자 이하여야 합니다.",
        )

    if not all(
        ("a" <= character <= "z")
        or ("0" <= character <= "9")
        for character in username
    ):
        raise AccountValidationError(
            field="username",
            code="USERNAME_INVALID",
            message="아이디는 영문 소문자와 숫자만 사용할 수 있습니다.",
        )

    if not any("a" <= character <= "z" for character in username):
        raise AccountValidationError(
            field="username",
            code="USERNAME_INVALID",
            message="아이디에는 영문 소문자가 1개 이상 포함되어야 합니다.",
        )

    return username


def normalize_and_validate_email(email):
    """Normalize an email address, validate it, and return the result."""

    normalized_email = normalize_email(email)

    if not normalized_email:
        raise AccountValidationError(
            field="email",
            code="EMAIL_REQUIRED",
            message="모든 항목을 입력해주세요.",
        )

    if len(normalized_email) > 320:
        raise AccountValidationError(
            field="email",
            code="EMAIL_INVALID",
            message="이메일은 320자 이하로 입력해주세요.",
        )

    if any(character.isspace() for character in normalized_email):
        raise AccountValidationError(
            field="email",
            code="EMAIL_INVALID",
            message="올바른 이메일 형식을 입력해주세요.",
        )

    if normalized_email.count("@") != 1:
        raise AccountValidationError(
            field="email",
            code="EMAIL_INVALID",
            message="올바른 이메일 형식을 입력해주세요.",
        )

    local_part, domain = normalized_email.rsplit("@", 1)

    if not local_part or not domain:
        raise AccountValidationError(
            field="email",
            code="EMAIL_INVALID",
            message="올바른 이메일 형식을 입력해주세요.",
        )

    if (
        "." not in domain
        or domain.startswith(".")
        or domain.endswith(".")
    ):
        raise AccountValidationError(
            field="email",
            code="EMAIL_INVALID",
            message="올바른 이메일 형식을 입력해주세요.",
        )

    return normalized_email


def validate_password(password):
    """Validate a registration password and return it unchanged."""

    if not password:
        raise AccountValidationError(
            field="password",
            code="PASSWORD_REQUIRED",
            message="모든 항목을 입력해주세요.",
        )

    if len(password) < 8:
        raise AccountValidationError(
            field="password",
            code="PASSWORD_INVALID",
            message="비밀번호는 8자 이상이어야 합니다.",
        )

    if len(password) > 20:
        raise AccountValidationError(
            field="password",
            code="PASSWORD_INVALID",
            message="비밀번호는 20자 이하여야 합니다.",
        )

    if any(character.isspace() for character in password):
        raise AccountValidationError(
            field="password",
            code="PASSWORD_INVALID",
            message="비밀번호에는 공백을 사용할 수 없습니다.",
        )

    if any(
        not (
            ("A" <= character <= "Z")
            or ("a" <= character <= "z")
            or ("0" <= character <= "9")
            or character in ALLOWED_SPECIAL_CHARACTERS
        )
        for character in password
    ):
        raise AccountValidationError(
            field="password",
            code="PASSWORD_INVALID",
            message=(
                "비밀번호에는 영문자, 숫자와 안내된 특수문자만 "
                "사용할 수 있습니다."
            ),
        )

    if not any(
        ("A" <= character <= "Z")
        or ("a" <= character <= "z")
        for character in password
    ):
        raise AccountValidationError(
            field="password",
            code="PASSWORD_INVALID",
            message="비밀번호에는 영문자가 1개 이상 포함되어야 합니다.",
        )

    if not any("0" <= character <= "9" for character in password):
        raise AccountValidationError(
            field="password",
            code="PASSWORD_INVALID",
            message="비밀번호에는 숫자가 1개 이상 포함되어야 합니다.",
        )

    if not any(
        character in ALLOWED_SPECIAL_CHARACTERS
        for character in password
    ):
        raise AccountValidationError(
            field="password",
            code="PASSWORD_INVALID",
            message="비밀번호에는 특수문자가 1개 이상 포함되어야 합니다.",
        )

    return password
