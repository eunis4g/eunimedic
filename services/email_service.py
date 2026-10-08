import os
import smtplib
import ssl
from email.message import EmailMessage


class EmailServiceError(Exception):
    pass


def _get_boolean_setting(name):

    value = os.getenv(name, "").strip().lower()

    if value in {"1", "true", "yes", "on"}:
        return True

    if value in {"0", "false", "no", "off"}:
        return False

    raise EmailServiceError(f"{name} 환경변수 설정이 올바르지 않습니다.")


def _send_plain_text_email(
    *,
    to_email,
    subject,
    body,
    message_id,
    failure_message,
):
    mail_server = os.getenv("MAIL_SERVER", "").strip()
    mail_port_text = os.getenv("MAIL_PORT", "").strip()
    mail_username = os.getenv("MAIL_USERNAME", "").strip()
    mail_password = os.getenv("MAIL_PASSWORD", "")
    mail_from = os.getenv("MAIL_FROM", "").strip()

    if not mail_server or not mail_port_text or not mail_from:
        raise EmailServiceError("SMTP 환경변수가 설정되어 있지 않습니다.")

    try:
        mail_port = int(mail_port_text)
    except ValueError as error:
        raise EmailServiceError("MAIL_PORT 설정이 올바르지 않습니다.") from error

    use_tls = _get_boolean_setting("MAIL_USE_TLS")
    use_ssl = _get_boolean_setting("MAIL_USE_SSL")

    if use_tls and use_ssl:
        raise EmailServiceError(
            "MAIL_USE_TLS와 MAIL_USE_SSL을 동시에 사용할 수 없습니다."
        )

    if bool(mail_username) != bool(mail_password):
        raise EmailServiceError(
            "SMTP 사용자 이름과 비밀번호 설정을 함께 확인해주세요."
        )

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = mail_from
    message["To"] = to_email

    if message_id is not None:
        message["Message-ID"] = message_id

    message.set_content(body)

    context = ssl.create_default_context()

    try:
        if use_ssl:
            smtp_client = smtplib.SMTP_SSL(
                mail_server,
                mail_port,
                timeout=10,
                context=context,
            )
        else:
            smtp_client = smtplib.SMTP(
                mail_server,
                mail_port,
                timeout=10,
            )

        with smtp_client:
            if use_tls:
                smtp_client.starttls(context=context)

            if mail_username:
                smtp_client.login(mail_username, mail_password)

            smtp_client.send_message(message)
    except (OSError, smtplib.SMTPException) as error:
        raise EmailServiceError(failure_message) from error

    return message_id


def send_email(to_email, subject, body, message_id=None):
    """Send a plain-text email with the project's existing SMTP settings."""

    return _send_plain_text_email(
        to_email=to_email,
        subject=subject,
        body=body,
        message_id=message_id,
        failure_message="이메일 발송에 실패했습니다.",
    )


def send_verification_email(email, verification_code):

    _send_plain_text_email(
        to_email=email,
        subject="Medicine Web 이메일 인증번호",
        body=(
            "Medicine Web 이메일 인증 안내\n\n"
            f"인증번호: {verification_code}\n"
            "인증번호는 5분 동안 유효합니다.\n\n"
            "본인이 요청하지 않았다면 이 메일을 무시해주세요."
        ),
        message_id=None,
        failure_message="인증 이메일 발송에 실패했습니다.",
    )
