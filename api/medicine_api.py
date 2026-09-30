import os
import requests
from dotenv import load_dotenv


load_dotenv()

API_KEY = os.getenv("API_KEY")

API_URL = "https://apis.data.go.kr/1471000/DrugPrdtPrmsnInfoService08/getDrugPrdtPrmsnInq08"


def search_medicine(medicine_name):

    params = {
        "serviceKey": API_KEY,
        "pageNo": "1",
        "numOfRows": "10",
        "type": "xml",
        "item_name": medicine_name
    }

    response = requests.get(
        API_URL,
        params=params
    )

    return response