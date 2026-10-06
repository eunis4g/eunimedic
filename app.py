import hashlib
import hmac
import logging
import math
import os
import re
import secrets
from datetime import date, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import uuid4
from xml.etree.ElementTree import ParseError
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import nh3

from dotenv import load_dotenv
from flask import abort, Flask, flash, redirect, render_template, request, url_for
from flask_login import (
    LoginManager,
    current_user,
    login_required,
    login_user,
    logout_user,
)
from flask_migrate import Migrate
from flask_wtf.csrf import CSRFProtect
from markupsafe import Markup
from requests import RequestException
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import selectinload
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from api.medicine_api import (
    search_medicine,
    parse_medicine_response,
    get_medicine_detail,
    parse_medicine_detail_response,
    get_medicine_ingredients,
    parse_medicine_ingredient_response,
    get_easy_drug_info,
    parse_easy_drug_response,
    get_pill_identification,
    parse_pill_identification_response,
)
from models import (
    Medicine,
    MedicationSchedule,
    MedicationTime,
    PendingRegistration,
    User,
    UserMedicine,
    db,
    utc_now,
)
from services.email_service import EmailServiceError, send_verification_email
from services.medication_schedule_service import (
    MedicationScheduleCalculationError,
    calculate_last_scheduled_occurrence,
    calculate_occurrence_summary,
    normalize_medication_times,
    validate_plan_capacity,
)


load_dotenv()

app = Flask(__name__)

SECRET_KEY = os.getenv("SECRET_KEY")
EMAIL_VERIFICATION_SECRET = os.getenv("EMAIL_VERIFICATION_SECRET")

if not SECRET_KEY:
    raise RuntimeError("SECRET_KEY 환경변수가 설정되어 있지 않습니다.")

if not EMAIL_VERIFICATION_SECRET:
    raise RuntimeError(
        "EMAIL_VERIFICATION_SECRET 환경변수가 설정되어 있지 않습니다."
    )

app.config["SECRET_KEY"] = SECRET_KEY
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

login_manager = LoginManager()
login_manager.login_view = "login"
login_manager.login_message = "로그인이 필요합니다."
login_manager.login_message_category = "error"
login_manager.init_app(app)

csrf = CSRFProtect()
csrf.init_app(app)

BASE_DIR = Path(__file__).resolve().parent
DATABASE_PATH = BASE_DIR / "medicine.db"

app.config["SQLALCHEMY_DATABASE_URI"] = (
    "sqlite:///" + DATABASE_PATH.as_posix()
)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db.init_app(app)
migrate = Migrate(app, db)


@login_manager.user_loader
def load_user(user_id):

    try:
        normalized_user_id = int(user_id)
    except (TypeError, ValueError):
        return None

    return db.session.get(User, normalized_user_id)


UPLOAD_FOLDER = Path(app.static_folder) / "uploads"
ALLOWED_IMAGE_EXTENSIONS = {"jpg", "jpeg", "png"}
ALLOWED_IMAGE_MIME_TYPES = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
}
ALLOWED_SPECIAL_CHARACTERS = "!@#$%^&*()_-+=?.,"
VERIFICATION_CODE_LIFETIME = timedelta(minutes=5)
PENDING_REGISTRATION_LIFETIME = timedelta(minutes=30)
RESEND_COOLDOWN = timedelta(seconds=60)
MAX_VERIFICATION_ATTEMPTS = 5
MAX_RESEND_COUNT = 4
MEDICATION_INTAKE_TIMINGS = {
    "before_meal",
    "after_meal",
    "regardless_of_meal",
}
MEDICATION_INTAKE_TIMING_LABELS = {
    "before_meal": "식전",
    "after_meal": "식후",
    "regardless_of_meal": "식사와 관계없이",
}
MEDICATION_DOSE_UNITS = {"정", "캡슐", "포"}
MEDICATION_DOSE_AMOUNT_PATTERN = re.compile(
    r"^[0-9]+(?:\.[0-9]+)?$"
)
MEDICATION_TIME_PATTERN = re.compile(
    r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$"
)


class VerificationTokenLogFilter(logging.Filter):

    verification_path_pattern = re.compile(
        r"(/verify-email/)[^/?\s]+"
    )

    @classmethod
    def redact_token(cls, value):

        return cls.verification_path_pattern.sub(
            r"\1[REDACTED]",
            value,
        )

    def filter(self, record):

        if isinstance(record.msg, str):
            record.msg = self.redact_token(record.msg)

        if isinstance(record.args, tuple):
            record.args = tuple(
                self.redact_token(argument)
                if isinstance(argument, str)
                else argument
                for argument in record.args
            )

        return True


logging.getLogger("werkzeug").addFilter(VerificationTokenLogFilter())


ALLOWED_TAGS = {
    "p", "br", "div", "span",
    "table", "caption", "colgroup", "col",
    "thead", "tbody", "tfoot", "tr", "td", "th",
    "ul", "ol", "li",
    "strong", "b", "em", "i", "u", "sup", "sub",
}

ALLOWED_ATTRIBUTES = {
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan", "scope"},
    "col": {"span"},
    "colgroup": {"span"},
    "ol": {"start"},
    "li": {"value"},
}

HTML_CLEANER = nh3.Cleaner(
    tags=ALLOWED_TAGS,
    attributes=ALLOWED_ATTRIBUTES,
    clean_content_tags={
        "script", "style", "iframe", "object",
        "embed", "form", "svg", "math",
    },
)


@app.template_filter("sanitize_html")
def sanitize_html(value):

    cleaned_html = HTML_CLEANER.clean(value or "")

    return Markup(cleaned_html)


def is_valid_email(email):

    if any(character.isspace() for character in email):
        return False

    if email.count("@") != 1:
        return False

    local_part, domain = email.rsplit("@", 1)

    if not local_part or not domain:
        return False

    if "." not in domain or domain.startswith(".") or domain.endswith("."):
        return False

    return True


def as_utc(value):

    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)

    return value.astimezone(timezone.utc)


def get_owned_active_user_medicine(user_medicine_id):

    user_medicine = db.session.scalar(
        db.select(UserMedicine).where(
            UserMedicine.user_medicine_id == user_medicine_id,
            UserMedicine.user_id == current_user.user_id,
            UserMedicine.is_active.is_(True),
        )
    )

    if user_medicine is None:
        abort(404)

    return user_medicine


def get_active_medication_schedule(user_medicine_id):

    return db.session.scalar(
        db.select(MedicationSchedule).where(
            MedicationSchedule.user_medicine_id == user_medicine_id,
            MedicationSchedule.is_active.is_(True),
        )
    )


def get_owned_medication_schedule(schedule_id):

    schedule = db.session.scalar(
        db.select(MedicationSchedule)
        .options(
            selectinload(MedicationSchedule.times),
            selectinload(
                MedicationSchedule.user_medicine
            ).selectinload(UserMedicine.medicine),
        )
        .join(MedicationSchedule.user_medicine)
        .where(
            MedicationSchedule.schedule_id == schedule_id,
            UserMedicine.user_id == current_user.user_id,
        )
    )

    if schedule is None:
        abort(404)

    return schedule


def get_owned_editable_medication_schedule(schedule_id):

    schedule = db.session.scalar(
        db.select(MedicationSchedule)
        .join(MedicationSchedule.user_medicine)
        .where(
            MedicationSchedule.schedule_id == schedule_id,
            MedicationSchedule.is_active.is_(True),
            UserMedicine.user_id == current_user.user_id,
            UserMedicine.is_active.is_(True),
        )
    )

    if schedule is None:
        abort(404)

    return schedule


def serialize_schedule_version(value):

    return as_utc(value).isoformat(timespec="microseconds")


def claim_medication_schedule_edit(
    schedule,
    *,
    old_updated_at,
    edit_time,
    deactivate=False,
):

    values = {"updated_at": edit_time}

    if deactivate:
        values["is_active"] = False

    result = db.session.execute(
        update(MedicationSchedule)
        .where(
            MedicationSchedule.schedule_id == schedule.schedule_id,
            MedicationSchedule.is_active.is_(True),
            MedicationSchedule.updated_at == old_updated_at,
        )
        .values(**values)
        .execution_options(synchronize_session=False)
    )

    return result.rowcount == 1


def calculate_stored_schedule_summary(
    schedule,
    *,
    reference_at,
    timezone_name,
):

    return calculate_occurrence_summary(
        start_date=schedule.start_date,
        course_days=schedule.course_days,
        times=(
            medication_time.time_of_day
            for medication_time in schedule.times
        ),
        reported_doses_taken_before_tracking=(
            schedule.reported_doses_taken_before_tracking
        ),
        accounted_occurrence_count=(
            schedule.accounted_occurrence_count
        ),
        reminder_tracking_started_at=as_utc(
            schedule.reminder_tracking_started_at
        ),
        reference_at=reference_at,
        timezone_name=timezone_name,
    )


def calculate_stored_schedule_last_occurrence(
    schedule,
    *,
    timezone_name,
):

    return calculate_last_scheduled_occurrence(
        start_date=schedule.start_date,
        course_days=schedule.course_days,
        times=(
            medication_time.time_of_day
            for medication_time in schedule.times
        ),
        reported_doses_taken_before_tracking=(
            schedule.reported_doses_taken_before_tracking
        ),
        accounted_occurrence_count=(
            schedule.accounted_occurrence_count
        ),
        reminder_tracking_started_at=as_utc(
            schedule.reminder_tracking_started_at
        ),
        timezone_name=timezone_name,
    )


def get_schedule_history_status(user_medicine, schedule, summary):

    if summary.remaining == 0:
        return "planned_completed", "예정된 복용 계획 완료"

    if schedule.is_active:
        if not user_medicine.is_active:
            return "removed_interrupted", "내 복용약에서 제거되어 중단된 계획"

        if user_medicine.schedule_setup_pending:
            return "pending_replacement", "새 복용 설정 대기 중인 이전 계획"

        return "current", "현재 복용 중"

    return "past", "과거 복용 설정"


def build_schedule_history_entry(
    user_medicine,
    schedule,
    *,
    reference_at,
    timezone_name,
):

    medication_times = tuple(
        sorted(
            medication_time.time_of_day
            for medication_time in schedule.times
        )
    )

    try:
        summary = calculate_stored_schedule_summary(
            schedule,
            reference_at=reference_at,
            timezone_name=timezone_name,
        )
    except MedicationScheduleCalculationError:
        status = "error"
        status_label = "복용 설정 확인 필요"
        last_scheduled_occurrence = None
    else:
        status, status_label = get_schedule_history_status(
            user_medicine,
            schedule,
            summary,
        )

        try:
            last_scheduled_occurrence = (
                calculate_stored_schedule_last_occurrence(
                    schedule,
                    timezone_name=timezone_name,
                )
            )
        except MedicationScheduleCalculationError:
            last_scheduled_occurrence = None

    return {
        "schedule": schedule,
        "status": status,
        "status_label": status_label,
        "intake_timing_label": (
            MEDICATION_INTAKE_TIMING_LABELS.get(
                schedule.intake_timing,
                "복용 시점 확인 필요",
            )
        ),
        "daily_frequency": len(medication_times),
        "medication_times": medication_times,
        "last_scheduled_occurrence": last_scheduled_occurrence,
    }


def get_medication_schedule_form_data():

    return {
        "dose_amount_text": request.form.get("dose_amount_text", ""),
        "dose_unit_text": request.form.get("dose_unit_text", ""),
        "intake_timing": request.form.get("intake_timing", ""),
        "daily_frequency": request.form.get("daily_frequency", ""),
        "medication_times": request.form.getlist("medication_times"),
        "start_date": request.form.get("start_date", ""),
        "course_days": request.form.get("course_days", ""),
        "reported_doses_taken_before_tracking": request.form.get(
            "reported_doses_taken_before_tracking",
            "",
        ),
    }


def parse_schedule_integer(value, *, field_label, minimum, errors):

    normalized_value = value.strip()

    if not normalized_value.isdigit():
        errors.append(f"{field_label}은(는) 정수로 입력해주세요.")
        return None

    try:
        parsed_value = int(normalized_value)
    except ValueError:
        errors.append(f"{field_label}은(는) 정수로 입력해주세요.")
        return None

    if parsed_value < minimum:
        errors.append(
            f"{field_label}은(는) {minimum} 이상이어야 합니다."
        )
        return None

    return parsed_value


def validate_medication_schedule_form(
    form_data,
    *,
    accounted_occurrence_count=0,
):

    errors = []
    validated_data = {}
    dose_amount_text = form_data["dose_amount_text"].strip()
    dose_unit_text = form_data["dose_unit_text"]

    if not dose_amount_text:
        errors.append("복용량을 입력해주세요.")
    elif len(dose_amount_text) > 100:
        errors.append("복용량은 100자 이하로 입력해주세요.")
    else:
        try:
            dose_amount = Decimal(dose_amount_text)
        except InvalidOperation:
            dose_amount = None

        if (
            not MEDICATION_DOSE_AMOUNT_PATTERN.fullmatch(
                dose_amount_text
            )
            or dose_amount is None
            or not dose_amount.is_finite()
            or dose_amount <= 0
        ):
            errors.append("복용량은 0보다 큰 숫자로 입력해주세요.")
        else:
            validated_data["dose_amount_text"] = dose_amount_text

    if dose_unit_text in MEDICATION_DOSE_UNITS:
        validated_data["dose_unit_text"] = dose_unit_text
    else:
        errors.append("복용 단위를 올바르게 선택해주세요.")

    intake_timing = form_data["intake_timing"]

    if intake_timing not in MEDICATION_INTAKE_TIMINGS:
        errors.append("식사와의 관계를 올바르게 선택해주세요.")
    else:
        validated_data["intake_timing"] = intake_timing

    daily_frequency = parse_schedule_integer(
        form_data["daily_frequency"],
        field_label="하루 복용 횟수",
        minimum=1,
        errors=errors,
    )
    medication_time_values = form_data["medication_times"]
    parsed_times = []
    times_are_valid = True

    if (
        daily_frequency is not None
        and len(medication_time_values) != daily_frequency
    ):
        errors.append("하루 복용 횟수와 복용 시간 개수가 일치해야 합니다.")
        times_are_valid = False

    for medication_time_value in medication_time_values:
        normalized_time = medication_time_value.strip()

        if not MEDICATION_TIME_PATTERN.fullmatch(normalized_time):
            errors.append("복용 시간은 HH:MM 형식으로 입력해주세요.")
            times_are_valid = False
            continue

        parsed_times.append(time.fromisoformat(normalized_time))

    if not medication_time_values:
        errors.append("복용 시간을 한 개 이상 입력해주세요.")
        times_are_valid = False

    if times_are_valid:
        try:
            validated_data["medication_times"] = (
                normalize_medication_times(parsed_times)
            )
        except MedicationScheduleCalculationError:
            errors.append("중복되지 않은 복용 시간을 입력해주세요.")

    try:
        validated_data["start_date"] = date.fromisoformat(
            form_data["start_date"].strip()
        )
    except ValueError:
        errors.append("복용 시작일을 올바르게 입력해주세요.")

    course_days = parse_schedule_integer(
        form_data["course_days"],
        field_label="복용 일수",
        minimum=1,
        errors=errors,
    )
    reported_doses = parse_schedule_integer(
        form_data["reported_doses_taken_before_tracking"],
        field_label="알림 설정 전에 이미 복용한 횟수",
        minimum=0,
        errors=errors,
    )

    if course_days is not None:
        validated_data["course_days"] = course_days

    if reported_doses is not None:
        validated_data["reported_doses_taken_before_tracking"] = (
            reported_doses
        )

    if (
        "medication_times" in validated_data
        and course_days is not None
        and reported_doses is not None
    ):
        try:
            validate_plan_capacity(
                times=validated_data["medication_times"],
                course_days=course_days,
                reported_doses_taken_before_tracking=reported_doses,
                accounted_occurrence_count=accounted_occurrence_count,
            )
        except MedicationScheduleCalculationError:
            errors.append(
                "총 예정 복용 횟수는 이미 복용했다고 입력한 횟수와 "
                "계획에 반영된 횟수의 합보다 적을 수 없습니다."
            )

    return validated_data, errors


def hash_verification_token(token):

    message = f"verification-token:{token}".encode("utf-8")

    return hmac.new(
        EMAIL_VERIFICATION_SECRET.encode("utf-8"),
        message,
        hashlib.sha256,
    ).hexdigest()


def hash_verification_code(token, verification_code):

    message = (
        f"verification-code:{token}:{verification_code}"
    ).encode("utf-8")

    return hmac.new(
        EMAIL_VERIFICATION_SECRET.encode("utf-8"),
        message,
        hashlib.sha256,
    ).hexdigest()


def get_pending_registration(token):

    if not token or len(token) > 128:
        return None

    token_hash = hash_verification_token(token)

    return db.session.scalar(
        db.select(PendingRegistration).where(
            PendingRegistration.verification_token_hash == token_hash
        )
    )


def is_pending_registration_expired(pending_registration, current_time):

    pending_created_at = as_utc(pending_registration.created_at)

    return (
        current_time
        >= pending_created_at + PENDING_REGISTRATION_LIFETIME
    )


def mask_email(email):

    local_part, domain = email.rsplit("@", 1)
    visible_part = local_part[:2] if len(local_part) > 2 else local_part[:1]

    return f"{visible_part}***@{domain}"


def fetch_verified_medicine_detail(item_seq):

    response = get_medicine_detail(item_seq)
    response.raise_for_status()
    medicine = parse_medicine_detail_response(response)

    if medicine is None or medicine.get("item_seq") != item_seq:
        return None

    return medicine


@app.route("/register", methods=["GET", "POST"])
def register():

    if current_user.is_authenticated:
        return redirect(url_for("home"))

    username = ""
    email = ""

    if request.method == "POST":
        username = request.form.get("username", "")
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        password_confirm = request.form.get("password_confirm", "")

        if not username or not email or not password or not password_confirm:
            flash("모든 항목을 입력해주세요.", "error")
        elif len(username) < 4:
            flash("아이디는 4자 이상이어야 합니다.", "error")
        elif len(username) > 20:
            flash("아이디는 20자 이하여야 합니다.", "error")
        elif not all(
            ("a" <= character <= "z")
            or ("0" <= character <= "9")
            for character in username
        ):
            flash(
                "아이디는 영문 소문자와 숫자만 사용할 수 있습니다.",
                "error",
            )
        elif not any(
            "a" <= character <= "z"
            for character in username
        ):
            flash(
                "아이디에는 영문 소문자가 1개 이상 포함되어야 합니다.",
                "error",
            )
        elif len(email) > 320:
            flash("이메일은 320자 이하로 입력해주세요.", "error")
        elif not is_valid_email(email):
            flash("올바른 이메일 형식을 입력해주세요.", "error")
        elif len(password) < 8:
            flash("비밀번호는 8자 이상이어야 합니다.", "error")
        elif len(password) > 20:
            flash("비밀번호는 20자 이하여야 합니다.", "error")
        elif any(character.isspace() for character in password):
            flash("비밀번호에는 공백을 사용할 수 없습니다.", "error")
        elif any(
            not (
                ("A" <= character <= "Z")
                or ("a" <= character <= "z")
                or ("0" <= character <= "9")
                or character in ALLOWED_SPECIAL_CHARACTERS
            )
            for character in password
        ):
            flash(
                "비밀번호에는 영문자, 숫자와 안내된 특수문자만 사용할 수 있습니다.",
                "error",
            )
        elif not any(
            ("A" <= character <= "Z")
            or ("a" <= character <= "z")
            for character in password
        ):
            flash("비밀번호에는 영문자가 1개 이상 포함되어야 합니다.", "error")
        elif not any(
            "0" <= character <= "9"
            for character in password
        ):
            flash("비밀번호에는 숫자가 1개 이상 포함되어야 합니다.", "error")
        elif not any(
            character in ALLOWED_SPECIAL_CHARACTERS
            for character in password
        ):
            flash(
                "비밀번호에는 특수문자가 1개 이상 포함되어야 합니다.",
                "error",
            )
        elif password != password_confirm:
            flash("비밀번호와 비밀번호 확인이 일치하지 않습니다.", "error")
        else:
            existing_username = db.session.scalar(
                db.select(User).where(User.username == username)
            )
            existing_email = db.session.scalar(
                db.select(User).where(User.email == email)
            )

            if existing_username is not None:
                flash("이미 사용 중인 아이디입니다.", "error")
            elif existing_email is not None:
                flash("이미 가입된 이메일입니다.", "error")
            else:
                current_time = utc_now()
                pending_by_username = db.session.scalar(
                    db.select(PendingRegistration).where(
                        PendingRegistration.username == username
                    )
                )
                pending_by_email = db.session.scalar(
                    db.select(PendingRegistration).where(
                        PendingRegistration.email == email
                    )
                )

                pending_rows = {
                    pending.pending_registration_id: pending
                    for pending in (pending_by_username, pending_by_email)
                    if pending is not None
                }

                expired_pending_rows = [
                    pending
                    for pending in pending_rows.values()
                    if (
                        current_time >= as_utc(pending.expires_at)
                        or is_pending_registration_expired(
                            pending,
                            current_time,
                        )
                    )
                ]

                if expired_pending_rows:
                    for pending in expired_pending_rows:
                        db.session.delete(pending)

                    try:
                        db.session.commit()
                    except IntegrityError:
                        db.session.rollback()
                        flash(
                            "회원가입 요청을 처리할 수 없습니다. 다시 시도해주세요.",
                            "error",
                        )
                        return render_template(
                            "register.html",
                            username=username,
                            email=email,
                            allowed_special_characters=(
                                ALLOWED_SPECIAL_CHARACTERS
                            ),
                        )

                    pending_by_username = db.session.scalar(
                        db.select(PendingRegistration).where(
                            PendingRegistration.username == username
                        )
                    )
                    pending_by_email = db.session.scalar(
                        db.select(PendingRegistration).where(
                            PendingRegistration.email == email
                        )
                    )

                if (
                    pending_by_username is not None
                    or pending_by_email is not None
                ):
                    flash(
                        "이미 이메일 인증이 진행 중인 아이디 또는 이메일입니다.",
                        "error",
                    )
                else:
                    verification_code = (
                        f"{secrets.randbelow(1_000_000):06d}"
                    )
                    verification_token = secrets.token_urlsafe(32)
                    pending_registration = PendingRegistration(
                        username=username,
                        email=email,
                        password_hash=generate_password_hash(password),
                        verification_code_hash=hash_verification_code(
                            verification_token,
                            verification_code,
                        ),
                        verification_token_hash=hash_verification_token(
                            verification_token
                        ),
                        expires_at=(
                            current_time + VERIFICATION_CODE_LIFETIME
                        ),
                        attempt_count=0,
                        resend_count=0,
                        last_sent_at=current_time,
                    )
                    db.session.add(pending_registration)

                    try:
                        db.session.flush()
                        send_verification_email(email, verification_code)
                        db.session.commit()
                    except EmailServiceError:
                        db.session.rollback()
                        flash(
                            "인증 이메일을 발송하지 못했습니다. 잠시 후 다시 시도해주세요.",
                            "error",
                        )
                    except IntegrityError:
                        db.session.rollback()
                        flash(
                            "이미 사용 중이거나 인증이 진행 중인 아이디 또는 이메일입니다.",
                            "error",
                        )
                    else:
                        flash(
                            "인증번호를 이메일로 발송했습니다.",
                            "success",
                        )
                        return redirect(
                            url_for(
                                "verify_email",
                                token=verification_token,
                            )
                        )

    return render_template(
        "register.html",
        username=username,
        email=email,
        allowed_special_characters=ALLOWED_SPECIAL_CHARACTERS,
    )


@app.route("/verify-email/<token>", methods=["GET", "POST"])
def verify_email(token):

    if current_user.is_authenticated:
        return redirect(url_for("home"))

    pending_registration = get_pending_registration(token)

    if pending_registration is None:
        flash("유효하지 않은 이메일 인증 요청입니다.", "error")
        return redirect(url_for("register"))

    current_time = utc_now()

    if is_pending_registration_expired(
        pending_registration,
        current_time,
    ):
        db.session.delete(pending_registration)
        db.session.commit()
        flash("이메일 인증 절차가 만료되었습니다. 다시 가입해주세요.", "error")
        return redirect(url_for("register"))

    if request.method == "POST":
        verification_code = request.form.get("verification_code", "")

        if current_time > as_utc(pending_registration.expires_at):
            flash(
                "인증번호가 만료되었습니다. 다시 인증번호를 요청해주세요.",
                "error",
            )
        elif pending_registration.attempt_count >= MAX_VERIFICATION_ATTEMPTS:
            flash(
                "인증번호 입력 횟수를 초과했습니다. 다시 인증번호를 요청해주세요.",
                "error",
            )
        else:
            submitted_code_hash = hash_verification_code(
                token,
                verification_code,
            )
            valid_code_format = (
                len(verification_code) == 6
                and all(
                    "0" <= character <= "9"
                    for character in verification_code
                )
            )
            code_matches = (
                valid_code_format
                and hmac.compare_digest(
                    pending_registration.verification_code_hash,
                    submitted_code_hash,
                )
            )

            if not code_matches:
                pending_registration.attempt_count += 1
                db.session.commit()

                if (
                    pending_registration.attempt_count
                    >= MAX_VERIFICATION_ATTEMPTS
                ):
                    flash(
                        "인증번호 입력 횟수를 초과했습니다. 다시 인증번호를 요청해주세요.",
                        "error",
                    )
                else:
                    flash("인증번호가 올바르지 않습니다.", "error")
            else:
                existing_username = db.session.scalar(
                    db.select(User).where(
                        User.username == pending_registration.username
                    )
                )
                existing_email = db.session.scalar(
                    db.select(User).where(
                        User.email == pending_registration.email
                    )
                )

                if existing_username is not None or existing_email is not None:
                    flash(
                        "아이디 또는 이메일을 더 이상 사용할 수 없습니다. 다시 가입해주세요.",
                        "error",
                    )
                    return redirect(url_for("register"))

                user = User(
                    username=pending_registration.username,
                    email=pending_registration.email,
                    password_hash=pending_registration.password_hash,
                )
                db.session.add(user)
                db.session.delete(pending_registration)

                try:
                    db.session.commit()
                except IntegrityError:
                    db.session.rollback()
                    flash(
                        "아이디 또는 이메일을 더 이상 사용할 수 없습니다. 다시 가입해주세요.",
                        "error",
                    )
                    return redirect(url_for("register"))

                flash("이메일 인증과 회원가입이 완료되었습니다.", "success")
                return redirect(url_for("login"))

    resend_available_at = (
        as_utc(pending_registration.last_sent_at) + RESEND_COOLDOWN
    )
    resend_remaining_seconds = max(
        0,
        math.ceil(
            (resend_available_at - current_time).total_seconds()
        ),
    )

    return render_template(
        "verify_email.html",
        token=token,
        masked_email=mask_email(pending_registration.email),
        resend_remaining_seconds=resend_remaining_seconds,
    )


@app.route("/verify-email/<token>/resend", methods=["POST"])
def resend_verification_email(token):

    if current_user.is_authenticated:
        return redirect(url_for("home"))

    pending_registration = get_pending_registration(token)

    if pending_registration is None:
        flash("유효하지 않은 이메일 인증 요청입니다.", "error")
        return redirect(url_for("register"))

    current_time = utc_now()

    if is_pending_registration_expired(
        pending_registration,
        current_time,
    ):
        db.session.delete(pending_registration)
        db.session.commit()
        flash("이메일 인증 절차가 만료되었습니다. 다시 가입해주세요.", "error")
        return redirect(url_for("register"))

    if pending_registration.resend_count >= MAX_RESEND_COUNT:
        flash(
            "인증번호 재전송 가능 횟수를 초과했습니다. 다시 가입해주세요.",
            "error",
        )
        return redirect(url_for("verify_email", token=token))

    last_sent_at = as_utc(pending_registration.last_sent_at)
    next_resend_at = last_sent_at + RESEND_COOLDOWN

    if current_time < next_resend_at:
        remaining_seconds = math.ceil(
            (next_resend_at - current_time).total_seconds()
        )
        flash(
            f"인증번호는 {remaining_seconds}초 후에 다시 요청할 수 있습니다.",
            "error",
        )
        return redirect(url_for("verify_email", token=token))

    verification_code = f"{secrets.randbelow(1_000_000):06d}"
    pending_registration.verification_code_hash = hash_verification_code(
        token,
        verification_code,
    )
    pending_registration.expires_at = (
        current_time + VERIFICATION_CODE_LIFETIME
    )
    pending_registration.attempt_count = 0
    pending_registration.resend_count += 1
    pending_registration.last_sent_at = current_time

    try:
        db.session.flush()
        send_verification_email(
            pending_registration.email,
            verification_code,
        )
        db.session.commit()
    except EmailServiceError:
        db.session.rollback()
        flash(
            "인증 이메일을 발송하지 못했습니다. 잠시 후 다시 시도해주세요.",
            "error",
        )
    else:
        flash("새 인증번호를 이메일로 발송했습니다.", "success")

    return redirect(url_for("verify_email", token=token))


@app.route("/login", methods=["GET", "POST"])
def login():

    if current_user.is_authenticated:
        return redirect(url_for("home"))

    username = ""

    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        user = db.session.scalar(
            db.select(User).where(User.username == username)
        )

        if (
            user is not None
            and user.is_active
            and check_password_hash(user.password_hash, password)
        ):
            login_user(user)
            flash("로그인되었습니다.", "success")
            return redirect(url_for("home"))

        flash("아이디 또는 비밀번호가 올바르지 않습니다.", "error")

    return render_template("login.html", username=username)


@app.route("/logout", methods=["POST"])
@login_required
def logout():

    logout_user()
    flash("로그아웃되었습니다.", "success")

    return redirect(url_for("home"))


@app.route("/my-medicines", methods=["GET"])
@login_required
def my_medicines():
    view_time = utc_now()
    user_medicines = db.session.scalars(
        db.select(UserMedicine)
        .options(
            selectinload(UserMedicine.medicine),
            selectinload(UserMedicine.schedules).selectinload(
                MedicationSchedule.times
            ),
        )
        .where(UserMedicine.user_id == current_user.user_id)
        .order_by(
            UserMedicine.registered_at.desc(),
            UserMedicine.user_medicine_id.desc(),
        )
    ).all()
    current_medications = []
    past_medications = []

    for user_medicine in user_medicines:
        schedules = tuple(user_medicine.schedules)
        active_schedule = next(
            (schedule for schedule in schedules if schedule.is_active),
            None,
        )
        has_history = bool(schedules)

        if user_medicine.is_active:
            if user_medicine.schedule_setup_pending:
                current_medications.append(
                    {
                        "user_medicine": user_medicine,
                        "status": "pending",
                        "schedule_id": None,
                        "has_history": has_history,
                    }
                )
                continue

            if active_schedule is not None:
                try:
                    summary = calculate_stored_schedule_summary(
                        active_schedule,
                        reference_at=view_time,
                        timezone_name=current_user.timezone,
                    )
                except MedicationScheduleCalculationError:
                    current_medications.append(
                        {
                            "user_medicine": user_medicine,
                            "status": "error",
                            "schedule_id": active_schedule.schedule_id,
                            "has_history": has_history,
                        }
                    )
                    continue

                if summary.remaining > 0:
                    current_medications.append(
                        {
                            "user_medicine": user_medicine,
                            "status": "active",
                            "schedule_id": active_schedule.schedule_id,
                            "has_history": has_history,
                        }
                    )
                    continue

            if not schedules:
                current_medications.append(
                    {
                        "user_medicine": user_medicine,
                        "status": "pending",
                        "schedule_id": None,
                        "has_history": False,
                    }
                )
                continue

        if schedules:
            latest_schedule = max(
                schedules,
                key=lambda schedule: (
                    as_utc(schedule.created_at),
                    schedule.schedule_id,
                ),
            )
            past_medications.append(
                {
                    "user_medicine": user_medicine,
                    "latest_schedule": latest_schedule,
                }
            )

    past_medications.sort(
        key=lambda item: (
            as_utc(item["latest_schedule"].created_at),
            item["user_medicine"].user_medicine_id,
        ),
        reverse=True,
    )

    return render_template(
        "my_medicines.html",
        current_medications=current_medications,
        past_medications=past_medications,
    )


@app.route(
    "/my-medicines/<int:user_medicine_id>/history",
    methods=["GET"],
)
@login_required
def medication_history(user_medicine_id):
    view_time = utc_now()
    user_medicine = db.session.scalar(
        db.select(UserMedicine)
        .options(
            selectinload(UserMedicine.medicine),
            selectinload(UserMedicine.schedules).selectinload(
                MedicationSchedule.times
            ),
        )
        .where(
            UserMedicine.user_medicine_id == user_medicine_id,
            UserMedicine.user_id == current_user.user_id,
        )
    )

    if user_medicine is None:
        abort(404)

    schedules = sorted(
        user_medicine.schedules,
        key=lambda schedule: (
            as_utc(schedule.created_at),
            schedule.schedule_id,
        ),
        reverse=True,
    )
    schedule_entries = [
        build_schedule_history_entry(
            user_medicine,
            schedule,
            reference_at=view_time,
            timezone_name=current_user.timezone,
        )
        for schedule in schedules
    ]
    has_current_schedule = any(
        entry["status"] == "current"
        for entry in schedule_entries
    )

    for entry in schedule_entries:
        entry["can_restart"] = (
            not has_current_schedule
            and entry["status"] != "current"
        )

    return render_template(
        "medication_history.html",
        user_medicine=user_medicine,
        schedule_entries=schedule_entries,
    )


@app.route(
    "/my-medicines/<int:user_medicine_id>/schedule/new",
    methods=["GET", "POST"],
)
@login_required
def new_medication_schedule(user_medicine_id):

    user_medicine = get_owned_active_user_medicine(user_medicine_id)
    operation_time = utc_now()
    active_schedule = get_active_medication_schedule(user_medicine_id)

    if (
        active_schedule is not None
        and not user_medicine.schedule_setup_pending
    ):
        try:
            active_summary = calculate_stored_schedule_summary(
                active_schedule,
                reference_at=operation_time,
                timezone_name=current_user.timezone,
            )
        except MedicationScheduleCalculationError:
            flash(
                "복용 설정 상태를 확인할 수 없습니다. 다시 시도해주세요.",
                "error",
            )
            return redirect(url_for("my_medicines"))

        if active_summary.remaining > 0:
            flash("이미 진행 중인 복용 설정이 있습니다.", "error")
            return redirect(url_for("my_medicines"))

    if request.method == "GET":
        try:
            user_timezone = ZoneInfo(current_user.timezone)
        except (ZoneInfoNotFoundError, TypeError, ValueError):
            flash(
                "시간대 정보를 확인할 수 없습니다. 다시 시도해주세요.",
                "error",
            )
            return redirect(url_for("my_medicines"))

        form_data = {
            "dose_amount_text": "",
            "dose_unit_text": "",
            "intake_timing": "regardless_of_meal",
            "daily_frequency": "1",
            "medication_times": ["09:00"],
            "start_date": operation_time.astimezone(
                user_timezone
            ).date().isoformat(),
            "course_days": "1",
            "reported_doses_taken_before_tracking": "0",
        }

        return render_template(
            "medication_schedule_form.html",
            user_medicine=user_medicine,
            form_data=form_data,
            errors=[],
            mode="create",
            form_action=url_for(
                "new_medication_schedule",
                user_medicine_id=user_medicine_id,
            ),
            heading="복용 설정",
            submit_label="저장",
            schedule_version=None,
        )

    form_data = get_medication_schedule_form_data()
    validated_data, errors = validate_medication_schedule_form(
        form_data,
        accounted_occurrence_count=0,
    )

    if not errors:
        try:
            new_summary = calculate_occurrence_summary(
                start_date=validated_data["start_date"],
                course_days=validated_data["course_days"],
                times=validated_data["medication_times"],
                reported_doses_taken_before_tracking=(
                    validated_data[
                        "reported_doses_taken_before_tracking"
                    ]
                ),
                accounted_occurrence_count=0,
                reminder_tracking_started_at=operation_time,
                reference_at=operation_time,
                timezone_name=current_user.timezone,
            )
        except MedicationScheduleCalculationError:
            errors.append(
                "복용 설정을 계산할 수 없습니다. 입력값과 시간대를 확인해주세요."
            )

    if errors:
        return (
            render_template(
                "medication_schedule_form.html",
                user_medicine=user_medicine,
                form_data=form_data,
                errors=errors,
                mode="create",
                form_action=url_for(
                    "new_medication_schedule",
                    user_medicine_id=user_medicine_id,
                ),
                heading="복용 설정",
                submit_label="저장",
                schedule_version=None,
            ),
            400,
        )

    if active_schedule is not None:
        active_schedule.is_active = False

        try:
            db.session.flush()
        except SQLAlchemyError:
            db.session.rollback()
            flash(
                "복용 설정을 저장하지 못했습니다. 다시 시도해주세요.",
                "error",
            )
            return redirect(url_for("my_medicines"))

    schedule = MedicationSchedule(
        user_medicine_id=user_medicine.user_medicine_id,
        intake_timing=validated_data["intake_timing"],
        dose_amount_text=validated_data["dose_amount_text"],
        dose_unit_text=validated_data["dose_unit_text"],
        instructions=None,
        start_date=validated_data["start_date"],
        end_date=None,
        course_days=validated_data["course_days"],
        reported_doses_taken_before_tracking=(
            validated_data["reported_doses_taken_before_tracking"]
        ),
        reminder_tracking_started_at=operation_time,
        accounted_occurrence_count=0,
        monday=True,
        tuesday=True,
        wednesday=True,
        thursday=True,
        friday=True,
        saturday=True,
        sunday=True,
        is_active=new_summary.remaining > 0,
    )
    schedule.times.extend(
        MedicationTime(time_of_day=medication_time)
        for medication_time in validated_data["medication_times"]
    )
    db.session.add(schedule)
    user_medicine.schedule_setup_pending = False

    try:
        db.session.flush()
        db.session.commit()
    except (IntegrityError, SQLAlchemyError):
        db.session.rollback()
        flash(
            "복용 설정을 저장하지 못했습니다. 다시 시도해주세요.",
            "error",
        )
        return redirect(url_for("my_medicines"))

    if new_summary.remaining == 0:
        flash("복용 기록이 저장되었습니다.", "success")
    else:
        flash("복용 설정이 저장되었습니다.", "success")

    return redirect(url_for("my_medicines"))


@app.route(
    "/medication-schedules/<int:schedule_id>/restart",
    methods=["GET", "POST"],
)
@login_required
def restart_medication_schedule(schedule_id):

    restart_time = utc_now()
    source_schedule = get_owned_medication_schedule(schedule_id)
    user_medicine = source_schedule.user_medicine
    active_schedule = get_active_medication_schedule(
        user_medicine.user_medicine_id
    )

    if (
        user_medicine.is_active
        and not user_medicine.schedule_setup_pending
        and active_schedule is not None
    ):
        try:
            active_summary = calculate_stored_schedule_summary(
                active_schedule,
                reference_at=restart_time,
                timezone_name=current_user.timezone,
            )
        except MedicationScheduleCalculationError:
            flash(
                "복용 설정 상태를 확인할 수 없습니다. 다시 시도해주세요.",
                "error",
            )
            return redirect(url_for("my_medicines"))

        if active_summary.remaining > 0:
            flash("현재 진행 중인 복용 일정이 있습니다.", "error")
            return redirect(url_for("my_medicines"))

    if request.method == "GET":
        try:
            user_timezone = ZoneInfo(current_user.timezone)
        except (ZoneInfoNotFoundError, TypeError, ValueError):
            flash(
                "시간대 정보를 확인할 수 없습니다. 다시 시도해주세요.",
                "error",
            )
            return redirect(url_for("my_medicines"))

        source_times = normalize_medication_times(
            medication_time.time_of_day
            for medication_time in source_schedule.times
        )
        form_data = {
            "dose_amount_text": source_schedule.dose_amount_text,
            "dose_unit_text": source_schedule.dose_unit_text,
            "intake_timing": source_schedule.intake_timing,
            "daily_frequency": str(len(source_times)),
            "medication_times": [
                medication_time.strftime("%H:%M")
                for medication_time in source_times
            ],
            "start_date": restart_time.astimezone(
                user_timezone
            ).date().isoformat(),
            "course_days": "",
            "reported_doses_taken_before_tracking": "0",
        }

        return render_template(
            "medication_schedule_form.html",
            user_medicine=user_medicine,
            form_data=form_data,
            errors=[],
            mode="restart",
            form_action=url_for(
                "restart_medication_schedule",
                schedule_id=schedule_id,
            ),
            heading="다시 복용하기",
            submit_label="새 복용 일정 시작",
            schedule_version=None,
        )

    form_data = get_medication_schedule_form_data()
    validated_data, errors = validate_medication_schedule_form(
        form_data,
        accounted_occurrence_count=0,
    )

    if not errors:
        try:
            new_summary = calculate_occurrence_summary(
                start_date=validated_data["start_date"],
                course_days=validated_data["course_days"],
                times=validated_data["medication_times"],
                reported_doses_taken_before_tracking=(
                    validated_data[
                        "reported_doses_taken_before_tracking"
                    ]
                ),
                accounted_occurrence_count=0,
                reminder_tracking_started_at=restart_time,
                reference_at=restart_time,
                timezone_name=current_user.timezone,
            )
        except MedicationScheduleCalculationError:
            errors.append(
                "복용 설정을 계산할 수 없습니다. "
                "입력값과 시간대를 확인해주세요."
            )
        else:
            if new_summary.remaining == 0:
                errors.append(
                    "새 복용 과정에는 남은 예정 복용 횟수가 있어야 합니다."
                )

    if errors:
        return (
            render_template(
                "medication_schedule_form.html",
                user_medicine=user_medicine,
                form_data=form_data,
                errors=errors,
                mode="restart",
                form_action=url_for(
                    "restart_medication_schedule",
                    schedule_id=schedule_id,
                ),
                heading="다시 복용하기",
                submit_label="새 복용 일정 시작",
                schedule_version=None,
            ),
            400,
        )

    if active_schedule is not None:
        active_schedule.is_active = False

    try:
        db.session.flush()

        user_medicine.is_active = True
        user_medicine.schedule_setup_pending = False
        new_schedule = MedicationSchedule(
            user_medicine_id=user_medicine.user_medicine_id,
            intake_timing=validated_data["intake_timing"],
            dose_amount_text=validated_data["dose_amount_text"],
            dose_unit_text=validated_data["dose_unit_text"],
            instructions=None,
            start_date=validated_data["start_date"],
            end_date=None,
            course_days=validated_data["course_days"],
            reported_doses_taken_before_tracking=(
                validated_data[
                    "reported_doses_taken_before_tracking"
                ]
            ),
            reminder_tracking_started_at=restart_time,
            accounted_occurrence_count=0,
            monday=True,
            tuesday=True,
            wednesday=True,
            thursday=True,
            friday=True,
            saturday=True,
            sunday=True,
            is_active=True,
        )
        new_schedule.times.extend(
            MedicationTime(time_of_day=medication_time)
            for medication_time in validated_data["medication_times"]
        )
        db.session.add(new_schedule)
        db.session.flush()
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flash(
            "복용 일정이 다른 요청에서 변경되었습니다. "
            "최신 내용을 확인해주세요.",
            "error",
        )
        return redirect(url_for("my_medicines"))
    except SQLAlchemyError:
        db.session.rollback()
        flash(
            "새 복용 일정을 시작하지 못했습니다. 다시 시도해주세요.",
            "error",
        )
        return redirect(url_for("my_medicines"))

    flash("새 복용 일정이 시작되었습니다.", "success")
    return redirect(url_for("my_medicines"))


@app.route(
    "/medication-schedules/<int:schedule_id>/edit",
    methods=["GET", "POST"],
)
@login_required
def edit_medication_schedule(schedule_id):

    operation_time = utc_now()
    schedule = get_owned_editable_medication_schedule(schedule_id)
    user_medicine = schedule.user_medicine

    try:
        old_times = normalize_medication_times(
            medication_time.time_of_day
            for medication_time in schedule.times
        )
        old_snapshot = {
            "start_date": schedule.start_date,
            "course_days": schedule.course_days,
            "times": old_times,
            "reported_doses_taken_before_tracking": (
                schedule.reported_doses_taken_before_tracking
            ),
            "accounted_occurrence_count": (
                schedule.accounted_occurrence_count
            ),
            "reminder_tracking_started_at": as_utc(
                schedule.reminder_tracking_started_at
            ),
            "updated_at": schedule.updated_at,
        }
        old_summary = calculate_occurrence_summary(
            start_date=old_snapshot["start_date"],
            course_days=old_snapshot["course_days"],
            times=old_snapshot["times"],
            reported_doses_taken_before_tracking=(
                old_snapshot[
                    "reported_doses_taken_before_tracking"
                ]
            ),
            accounted_occurrence_count=(
                old_snapshot["accounted_occurrence_count"]
            ),
            reminder_tracking_started_at=(
                old_snapshot["reminder_tracking_started_at"]
            ),
            reference_at=operation_time,
            timezone_name=current_user.timezone,
        )
        current_schedule_version = serialize_schedule_version(
            old_snapshot["updated_at"]
        )
    except MedicationScheduleCalculationError:
        flash(
            "복용 설정 상태를 확인할 수 없습니다. 다시 시도해주세요.",
            "error",
        )
        return redirect(url_for("my_medicines"))

    if request.method == "GET":
        if old_summary.remaining == 0:
            flash("이미 완료된 복용 설정입니다.", "info")
            return redirect(url_for("my_medicines"))

        form_data = {
            "dose_amount_text": schedule.dose_amount_text,
            "dose_unit_text": schedule.dose_unit_text,
            "intake_timing": schedule.intake_timing,
            "daily_frequency": str(len(old_times)),
            "medication_times": [
                medication_time.strftime("%H:%M")
                for medication_time in old_times
            ],
            "start_date": schedule.start_date.isoformat(),
            "course_days": str(schedule.course_days),
            "reported_doses_taken_before_tracking": str(
                schedule.reported_doses_taken_before_tracking
            ),
        }

        return render_template(
            "medication_schedule_form.html",
            user_medicine=user_medicine,
            form_data=form_data,
            errors=[],
            mode="edit",
            form_action=url_for(
                "edit_medication_schedule",
                schedule_id=schedule_id,
            ),
            heading="복용 설정 수정",
            submit_label="수정 저장",
            schedule_version=current_schedule_version,
        )

    submitted_schedule_version = request.form.get(
        "schedule_version",
        "",
    )

    if submitted_schedule_version != current_schedule_version:
        db.session.rollback()
        flash(
            "다른 요청에서 복용 설정이 변경되었습니다. "
            "최신 내용을 확인해주세요.",
            "error",
        )
        return redirect(url_for("my_medicines"))

    if old_summary.remaining == 0:
        try:
            claimed = claim_medication_schedule_edit(
                schedule,
                old_updated_at=old_snapshot["updated_at"],
                edit_time=operation_time,
                deactivate=True,
            )

            if not claimed:
                db.session.rollback()
                flash(
                    "다른 요청에서 복용 설정이 변경되었습니다. "
                    "최신 내용을 확인해주세요.",
                    "error",
                )
                return redirect(url_for("my_medicines"))

            db.session.commit()
        except SQLAlchemyError:
            db.session.rollback()
            flash(
                "복용 설정을 완료 처리하지 못했습니다. "
                "다시 시도해주세요.",
                "error",
            )
            return redirect(url_for("my_medicines"))

        flash("이미 완료된 복용 설정입니다.", "info")
        return redirect(url_for("my_medicines"))

    new_accounted_occurrence_count = (
        old_snapshot["accounted_occurrence_count"]
        + old_summary.elapsed_in_current_segment
    )
    form_data = get_medication_schedule_form_data()
    validated_data, errors = validate_medication_schedule_form(
        form_data,
        accounted_occurrence_count=(
            new_accounted_occurrence_count
        ),
    )

    if not errors:
        try:
            new_summary = calculate_occurrence_summary(
                start_date=validated_data["start_date"],
                course_days=validated_data["course_days"],
                times=validated_data["medication_times"],
                reported_doses_taken_before_tracking=(
                    validated_data[
                        "reported_doses_taken_before_tracking"
                    ]
                ),
                accounted_occurrence_count=(
                    new_accounted_occurrence_count
                ),
                reminder_tracking_started_at=operation_time,
                reference_at=operation_time,
                timezone_name=current_user.timezone,
            )
        except MedicationScheduleCalculationError:
            errors.append(
                "복용 설정을 계산할 수 없습니다. "
                "입력값과 시간대를 확인해주세요."
            )

    if errors:
        return (
            render_template(
                "medication_schedule_form.html",
                user_medicine=user_medicine,
                form_data=form_data,
                errors=errors,
                mode="edit",
                form_action=url_for(
                    "edit_medication_schedule",
                    schedule_id=schedule_id,
                ),
                heading="복용 설정 수정",
                submit_label="수정 저장",
                schedule_version=current_schedule_version,
            ),
            400,
        )

    try:
        claimed = claim_medication_schedule_edit(
            schedule,
            old_updated_at=old_snapshot["updated_at"],
            edit_time=operation_time,
        )

        if not claimed:
            db.session.rollback()
            flash(
                "다른 요청에서 복용 설정이 변경되었습니다. "
                "최신 내용을 확인해주세요.",
                "error",
            )
            return redirect(url_for("my_medicines"))

        schedule.dose_amount_text = validated_data["dose_amount_text"]
        schedule.dose_unit_text = validated_data["dose_unit_text"]
        schedule.intake_timing = validated_data["intake_timing"]
        schedule.start_date = validated_data["start_date"]
        schedule.course_days = validated_data["course_days"]
        schedule.reported_doses_taken_before_tracking = (
            validated_data["reported_doses_taken_before_tracking"]
        )
        schedule.accounted_occurrence_count = (
            new_accounted_occurrence_count
        )
        schedule.reminder_tracking_started_at = operation_time
        schedule.is_active = new_summary.remaining > 0
        schedule.updated_at = operation_time

        schedule.times.clear()
        db.session.flush()
        schedule.times.extend(
            MedicationTime(time_of_day=medication_time)
            for medication_time in validated_data["medication_times"]
        )
        db.session.flush()
        db.session.commit()
    except (IntegrityError, SQLAlchemyError):
        db.session.rollback()
        flash(
            "복용 설정을 수정하지 못했습니다. 다시 시도해주세요.",
            "error",
        )
        return redirect(url_for("my_medicines"))

    if new_summary.remaining == 0:
        flash(
            "복용 설정을 저장했지만 남은 복용 계획이 없어 "
            "완료되었습니다.",
            "success",
        )
    else:
        flash("복용 설정이 수정되었습니다.", "success")

    return redirect(url_for("my_medicines"))


@app.route("/my-medicines/add", methods=["GET"])
@login_required
def search_my_medicine():

    medicine_name = request.args.get("medicine_name", "").strip()
    medicines = []
    search_performed = False
    search_error = False

    if medicine_name:
        search_performed = True

        try:
            response = search_medicine(medicine_name)
            response.raise_for_status()
            medicines = parse_medicine_response(response)[:10]
        except (RequestException, ParseError):
            search_error = True

    return render_template(
        "add_my_medicine.html",
        medicine_name=medicine_name,
        medicines=medicines,
        search_performed=search_performed,
        search_error=search_error,
    )


@app.route("/my-medicines/add/<item_seq>", methods=["GET"])
@login_required
def add_my_medicine_detail(item_seq):

    try:
        medicine = fetch_verified_medicine_detail(item_seq)
    except (RequestException, ParseError):
        medicine = None

    if medicine is None:
        flash(
            "의약품 정보를 불러오지 못했습니다. 잠시 후 다시 시도해주세요.",
            "error",
        )
        return redirect(url_for("search_my_medicine"))

    try:
        pill_response = get_pill_identification(item_seq)
        pill_response.raise_for_status()
        pill_identifications = parse_pill_identification_response(
            pill_response
        )
    except (RequestException, ParseError):
        pill_identifications = []

    return render_template(
        "add_my_medicine_detail.html",
        medicine=medicine,
        pill_identifications=pill_identifications,
    )


@app.route("/my-medicines/add/<item_seq>", methods=["POST"])
@login_required
def register_my_medicine(item_seq):

    user_medicine = db.session.scalar(
        db.select(UserMedicine).where(
            UserMedicine.user_id == current_user.user_id,
            UserMedicine.medicine_item_seq == item_seq,
        )
    )

    if user_medicine is not None and user_medicine.is_active:
        flash("이미 내 복용약에 등록된 약입니다.", "error")
        return redirect(url_for("my_medicines"))

    medicine = db.session.get(Medicine, item_seq)

    if medicine is None:
        try:
            medicine_data = fetch_verified_medicine_detail(item_seq)
        except (RequestException, ParseError):
            medicine_data = None

        if medicine_data is None or not medicine_data.get("item_name"):
            db.session.rollback()
            flash(
                "의약품 정보를 확인할 수 없어 등록하지 못했습니다.",
                "error",
            )
            return redirect(url_for("search_my_medicine"))

        medicine = Medicine(
            item_seq=medicine_data["item_seq"],
            item_name=medicine_data["item_name"],
            entp_name=medicine_data.get("entp_name"),
        )
        db.session.add(medicine)

    if user_medicine is None:
        user_medicine = UserMedicine(
            user_id=current_user.user_id,
            medicine_item_seq=item_seq,
            registration_source="search",
            is_active=True,
            schedule_setup_pending=True,
        )
        db.session.add(user_medicine)
    else:
        user_medicine.is_active = True
        user_medicine.schedule_setup_pending = True
        user_medicine.registration_source = "search"

    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flash(
            "내 복용약 등록을 처리하지 못했습니다. 다시 시도해주세요.",
            "error",
        )
        return redirect(url_for("my_medicines"))

    flash("내 복용약에 등록했습니다.", "success")
    return redirect(url_for("my_medicines"))


@app.route(
    "/my-medicines/<int:user_medicine_id>/deactivate",
    methods=["POST"],
)
@login_required
def deactivate_my_medicine(user_medicine_id):

    user_medicine = db.session.scalar(
        db.select(UserMedicine).where(
            UserMedicine.user_medicine_id == user_medicine_id,
            UserMedicine.user_id == current_user.user_id,
        )
    )

    if user_medicine is None:
        abort(404)

    if not user_medicine.is_active:
        flash("이미 내 복용약에서 제거된 약입니다.", "info")
        return redirect(url_for("my_medicines"))

    user_medicine.is_active = False
    user_medicine.schedule_setup_pending = False

    try:
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        flash(
            "내 복용약 제거를 처리하지 못했습니다. 다시 시도해주세요.",
            "error",
        )
        return redirect(url_for("my_medicines"))

    flash("내 복용약에서 제거했습니다.", "success")
    return redirect(url_for("my_medicines"))


@app.route("/", methods=["GET", "POST"])
def home():

    medicine_name = ""
    medicines = []

    if request.method == "POST":

        medicine_name = request.form["medicine_name"].strip()

        print("검색어:", medicine_name)

        if medicine_name == "":
            medicines = []

        else :
            response = search_medicine(medicine_name)

            print("요청 URL:", response.url)
            print("상태 코드:", response.status_code)
            
            medicines = parse_medicine_response(response)

            print("검색결과")
            print(medicines)


    return render_template(
        "index.html",
        medicine_name=medicine_name,
        medicines=medicines
    )


@app.route("/identify", methods=["POST"])
def identify():

    pill_image = request.files.get("pill_image")

    if pill_image is None or pill_image.filename == "":
        return render_template(
            "index.html",
            medicine_name="",
            medicines=[],
            upload_error="알약 사진을 선택해주세요."
        ), 400

    safe_filename = secure_filename(pill_image.filename)

    if "." not in safe_filename:
        return render_template(
            "index.html",
            medicine_name="",
            medicines=[],
            upload_error="jpg, jpeg, png 이미지 파일만 업로드할 수 있습니다."
        ), 400

    extension = safe_filename.rsplit(".", 1)[1].lower()

    if extension not in ALLOWED_IMAGE_EXTENSIONS:
        return render_template(
            "index.html",
            medicine_name="",
            medicines=[],
            upload_error="jpg, jpeg, png 이미지 파일만 업로드할 수 있습니다."
        ), 400

    if pill_image.mimetype != ALLOWED_IMAGE_MIME_TYPES[extension]:
        return render_template(
            "index.html",
            medicine_name="",
            medicines=[],
            upload_error="jpg, jpeg, png 이미지 파일만 업로드할 수 있습니다."
        ), 400

    file_signature = pill_image.stream.read(8)
    pill_image.stream.seek(0)

    if extension in {"jpg", "jpeg"}:
        has_valid_signature = file_signature.startswith(b"\xff\xd8\xff")
    else:
        has_valid_signature = file_signature == b"\x89PNG\r\n\x1a\n"

    if not has_valid_signature:
        return render_template(
            "index.html",
            medicine_name="",
            medicines=[],
            upload_error="jpg, jpeg, png 이미지 파일만 업로드할 수 있습니다."
        ), 400

    stored_filename = secure_filename(f"{uuid4().hex}.{extension}")

    UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)
    pill_image.save(UPLOAD_FOLDER / stored_filename)

    return render_template(
        "index.html",
        medicine_name="",
        medicines=[],
        upload_success="알약 사진이 정상적으로 업로드되었습니다."
    )


@app.route("/medicine/<item_seq>")
def medicine_detail(item_seq):

    print("선택한 약 ITEM_SEQ:", item_seq)

    response = get_medicine_detail(item_seq)

    print("상세 상태 코드:", response.status_code)

    medicine = parse_medicine_detail_response(response)

    ingredient_response = get_medicine_ingredients(item_seq)

    print("주성분 상태 코드:", ingredient_response.status_code)

    ingredients = parse_medicine_ingredient_response(ingredient_response)

    print("주성분 파싱 결과")
    print(ingredients)

    easy_drug_response = get_easy_drug_info(item_seq)

    print("e약은요 상태 코드:", easy_drug_response.status_code)

    easy_drug_info = parse_easy_drug_response(easy_drug_response)

    pill_response = get_pill_identification(item_seq)

    print("낱알식별 상태 코드:", pill_response.status_code)

    pill_identifications = parse_pill_identification_response(pill_response)

    return render_template(
        "medicine_detail.html",
        item_seq=item_seq,
        medicine=medicine,
        ingredients=ingredients,
        easy_drug_info=easy_drug_info,
        pill_identifications=pill_identifications
    )

if __name__ == "__main__":
    app.run(debug=True)
