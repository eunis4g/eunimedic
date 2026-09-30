from flask import Flask, render_template, request

from api.medicine_api import search_medicine, parse_medicine_response



app = Flask(__name__)


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


@app.route("/medicine/<item_seq>")
def medicine_detail(item_seq):

    print("선택한 약 ITEM_SEQ:", item_seq)

    return render_template(
        "medicine_detail.html",
        item_seq=item_seq
    )

if __name__ == "__main__":
    app.run(debug=True)