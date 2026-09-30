import os
import requests
import xml.etree.ElementTree as ET

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

    response = requests.get(API_URL, params=params)

    return response


def parse_medicine_response(response):

    root = ET.fromstring(response.text)

    total_count = root.findtext("./body/totalCount")

    if total_count == "0":
        return []

    medicines = []

    items = root.findall("./body/items/item")

    for item in items:

        medicine = {
            "item_seq": item.findtext("ITEM_SEQ"),
            "item_name": item.findtext("ITEM_NAME"),
            "entp_name": item.findtext("ENTP_NAME"),
        }

        medicines.append(medicine)

    return medicines