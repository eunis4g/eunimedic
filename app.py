from flask import Flask, render_template, request

from api.medicine_api import search_medicine


app = Flask(__name__)


@app.route("/", methods=["GET", "POST"])
def home():

    medicine_name = ""
    result = None

    if request.method == "POST":

        medicine_name = request.form["medicine_name"].strip()

        print("검색어:", medicine_name)

        if medicine_name == "":
            result = "검색 결과 없음"

        else :
            response = search_medicine(medicine_name)

            print("요청 URL:", response.url)
            print("상태 코드:", response.status_code)
            print("API 응답:")
            print(response.text)

            result = response.text

    return render_template(
        "index.html",
        medicine_name=medicine_name,
        result=result
    )


if __name__ == "__main__":
    app.run(debug=True)