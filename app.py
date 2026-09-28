from flask import Flask, render_template, request
import requests

app = Flask(__name__)

API_URL = "https://apis.data.go.kr/1471000/DrugPrdtPrmsnInfoService08/getDrugPrdtPrmsnInq08"

@app.route("/", methods=["GET", "POST"])
def home():

    medicine_name = ""
    result = None

    if request.method == "POST":

        medicine_name = request.form["medicine_name"]

        print("검색어:", medicine_name)

        params = {
            "serviceKey": "bbfcf6e1ac71f694ecdf54acde341498153c84ae9bb4e378c33b55534eeb134e",
            "pageNo": "1",
            "numOfRows": "10",
            "type": "xml",
            "item_name": medicine_name
        }

        response = requests.get(
            API_URL,
            params=params
        )

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