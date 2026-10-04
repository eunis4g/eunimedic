from pathlib import Path
from uuid import uuid4

import nh3

from flask import Flask, render_template, request
from flask_migrate import Migrate
from markupsafe import Markup
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
from models import db



app = Flask(__name__)


BASE_DIR = Path(__file__).resolve().parent
DATABASE_PATH = BASE_DIR / "medicine.db"

app.config["SQLALCHEMY_DATABASE_URI"] = (
    "sqlite:///" + DATABASE_PATH.as_posix()
)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db.init_app(app)
migrate = Migrate(app, db)


UPLOAD_FOLDER = Path(app.static_folder) / "uploads"
ALLOWED_IMAGE_EXTENSIONS = {"jpg", "jpeg", "png"}
ALLOWED_IMAGE_MIME_TYPES = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
}


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
