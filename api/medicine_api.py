import os
import requests
import xml.etree.ElementTree as ET

from dotenv import load_dotenv


load_dotenv()

API_KEY = os.getenv("API_KEY")

API_URL = "https://apis.data.go.kr/1471000/DrugPrdtPrmsnInfoService08/getDrugPrdtPrmsnInq08"
DETAIL_API_URL = "https://apis.data.go.kr/1471000/DrugPrdtPrmsnInfoService08/getDrugPrdtPrmsnDtlInq08"
INGREDIENT_API_URL = "https://apis.data.go.kr/1471000/DrugPrdtPrmsnInfoService08/getDrugPrdtMcpnDtlInq08"
EASY_DRUG_API_URL = "https://apis.data.go.kr/1471000/DrbEasyDrugInfoService/getDrbEasyDrugList"
PILL_IDENTIFICATION_API_URL = "https://apis.data.go.kr/1471000/MdcinGrnIdntfcInfoService03/getMdcinGrnIdntfcInfoList03"


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


def get_medicine_detail(item_seq):

    params = {
        "serviceKey": API_KEY,
        "pageNo": "1",
        "numOfRows": "1",
        "type": "xml",
        "item_seq": item_seq
    }

    response = requests.get(DETAIL_API_URL, params=params)

    return response


def _extract_text(element):

    if element is None:
        return ""

    text_parts = []

    for text in element.itertext():
        cleaned_text = text.strip()

        if cleaned_text:
            text_parts.append(cleaned_text)

    return " ".join(text_parts)


def parse_medicine_detail_response(response):

    root = ET.fromstring(response.text)

    total_count = root.findtext("./body/totalCount")

    if total_count == "0":
        return None

    item = root.find("./body/items/item")

    if item is None:
        return None

    medicine = {
        "item_seq": item.findtext("ITEM_SEQ"),
        "item_name": item.findtext("ITEM_NAME"),
        "entp_name": item.findtext("ENTP_NAME"),
        "item_permit_date": item.findtext("ITEM_PERMIT_DATE"),
        "etc_otc_code": item.findtext("ETC_OTC_CODE"),
        "chart": item.findtext("CHART"),
        "material_name": item.findtext("MATERIAL_NAME"),
        "ee_doc_data": _extract_text(item.find("EE_DOC_DATA")),
        "ud_doc_data": _extract_text(item.find("UD_DOC_DATA")),
        "nb_doc_data": _extract_text(item.find("NB_DOC_DATA")),
        "storage_method": item.findtext("STORAGE_METHOD"),
        "valid_term": item.findtext("VALID_TERM"),
        "pack_unit": item.findtext("PACK_UNIT"),
    }

    return medicine


def get_medicine_ingredients(item_seq):

    params = {
        "serviceKey": API_KEY,
        "pageNo": "1",
        "numOfRows": "100",
        "type": "xml",
        "Item_seq": item_seq
    }

    response = requests.get(INGREDIENT_API_URL, params=params)

    return response


def parse_medicine_ingredient_response(response):

    root = ET.fromstring(response.text)

    total_count = root.findtext("./body/totalCount")

    if total_count == "0":
        return []

    ingredients = []

    items = root.findall("./body/items/item")

    for item in items:

        ingredient = {}

        for element in item:
            ingredient[element.tag.lower()] = _extract_text(element)

        ingredients.append(ingredient)

    return ingredients


def get_easy_drug_info(item_seq):

    params = {
        "ServiceKey": API_KEY,
        "pageNo": "1",
        "numOfRows": "1",
        "type": "xml",
        "itemSeq": item_seq
    }

    response = requests.get(EASY_DRUG_API_URL, params=params)

    return response


def parse_easy_drug_response(response):

    root = ET.fromstring(response.text)

    total_count = root.findtext("./body/totalCount")

    if total_count == "0":
        return None

    item = root.find("./body/items/item")

    if item is None:
        return None

    easy_drug_info = {
        "item_seq": _extract_text(item.find("itemSeq")),
        "item_name": _extract_text(item.find("itemName")),
        "entp_name": _extract_text(item.find("entpName")),
        "efficacy": _extract_text(item.find("efcyQesitm")),
        "use_method": _extract_text(item.find("useMethodQesitm")),
        "warning": _extract_text(item.find("atpnWarnQesitm")),
        "precautions": _extract_text(item.find("atpnQesitm")),
        "interactions": _extract_text(item.find("intrcQesitm")),
        "side_effects": _extract_text(item.find("seQesitm")),
        "storage_method": _extract_text(item.find("depositMethodQesitm")),
        "item_image": _extract_text(item.find("itemImage")),
        "open_date": _extract_text(item.find("openDe")),
        "update_date": _extract_text(item.find("updateDe")),
    }

    return easy_drug_info


def get_pill_identification(item_seq):

    params = {
        "serviceKey": API_KEY,
        "pageNo": "1",
        "numOfRows": "10",
        "type": "xml",
        "item_seq": item_seq
    }

    response = requests.get(PILL_IDENTIFICATION_API_URL, params=params)

    return response


def parse_pill_identification_response(response):

    root = ET.fromstring(response.text)

    total_count = root.findtext("./body/totalCount")

    if total_count == "0":
        return []

    pill_identifications = []

    items = root.findall("./body/items/item")

    for item in items:

        pill_identification = {
            "item_seq": _extract_text(item.find("ITEM_SEQ")),
            "item_name": _extract_text(item.find("ITEM_NAME")),
            "entp_name": _extract_text(item.find("ENTP_NAME")),
            "item_image": _extract_text(item.find("ITEM_IMAGE")),
            "form_code_name": _extract_text(item.find("FORM_CODE_NAME")),
            "drug_shape": _extract_text(item.find("DRUG_SHAPE")),
            "color_class1": _extract_text(item.find("COLOR_CLASS1")),
            "color_class2": _extract_text(item.find("COLOR_CLASS2")),
            "print_front": _extract_text(item.find("PRINT_FRONT")),
            "print_back": _extract_text(item.find("PRINT_BACK")),
            "line_front": _extract_text(item.find("LINE_FRONT")),
            "line_back": _extract_text(item.find("LINE_BACK")),
            "leng_long": _extract_text(item.find("LENG_LONG")),
            "leng_short": _extract_text(item.find("LENG_SHORT")),
            "thick": _extract_text(item.find("THICK")),
        }

        pill_identifications.append(pill_identification)

    return pill_identifications
