import hashlib
import hmac
import logging
import math
import os
import re
import secrets
from datetime import timedelta, timezone
from pathlib import Path
from uuid import uuid4

import nh3

from dotenv import load_dotenv
from flask import Flask, flash, redirect, render_template, request, url_for
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
from sqlalchemy.exc import IntegrityError
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
from models import PendingRegistration, User, db, utc_now
from services.email_service import EmailServiceError, send_verification_email


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

    return render_template(
        "verify_email.html",
        token=token,
        masked_email=mask_email(pending_registration.email),
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
