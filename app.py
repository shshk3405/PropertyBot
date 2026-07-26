"""
PropertyBot v4.0 - 대시보드 · 입력 · 목록 · 지도 · 임장 체크리스트
"""
import os, re, time, json, hashlib, requests, xml.etree.ElementTree as ET
from datetime import datetime, date
import pandas as pd
import streamlit as st


# .env 파일 자동 로드
def _load_env():
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())
_load_env()

# ── 공용 HTTP 세션 (연결 재사용 + 자동 재시도) ──────────────
# Streamlit Cloud(해외 서버) → 한국 공공데이터 API 호출 시 간헐적 타임아웃 대응.
# 매 요청마다 새 TCP/TLS 연결을 맺는 대신 커넥션을 재사용하고,
# 일시적 실패(타임아웃 포함)는 지수 백오프로 최대 2회 자동 재시도한다.
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

def _build_session():
    s = requests.Session()
    retry = Retry(
        total=3, connect=4, read=2, backoff_factor=2,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["GET", "POST", "PATCH"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s

SESSION = _build_session()


def friendly_error(e):
    """예외를 사용자가 읽을 수 있는 짧은 한국어 메시지로 변환.
    (raw 스택트레이스·URL·API키 노출 방지 — 특히 네트워크 예외가 화면에 그대로 뜨는 걸 막기 위함)"""
    name = type(e).__name__
    if isinstance(e, requests.exceptions.ConnectTimeout):
        return "서버 연결 시간 초과 — 네트워크가 느리거나 해당 서버가 응답하지 않고 있어요. 잠시 후 다시 시도해주세요."
    if isinstance(e, requests.exceptions.ReadTimeout):
        return "응답 대기 시간 초과 — 서버가 느리게 응답하고 있어요. 잠시 후 다시 시도해주세요."
    if isinstance(e, requests.exceptions.ConnectionError):
        return "서버에 연결할 수 없어요 (네트워크 문제). 잠시 후 다시 시도해주세요."
    if isinstance(e, requests.exceptions.RequestException):
        return f"네트워크 오류가 발생했어요 ({name})."
    return f"오류가 발생했어요 ({name}: {str(e)[:120]})"

# ── 디자인 상수 ───────────────────────────────────────────
ACCENT = "#3b5bdb"
DEAL_COLORS = {"매매": "#e5484d", "전세": "#2f6feb", "월세": "#2f9e63"}
GAP_GOOD, GAP_BAD = "#1f9d57", "#e5484d"
STATUS_COLORS = {"검토중": "#8a8a93", "방문예정": ACCENT, "관심": "#c98a00", "방문완료": "#1f9d57"}
STATUS_OPTIONS = ["방문예정", "관심", "검토중", "방문완료"]  # 실제 노션 '상태' select 옵션과 동일하게 맞춤
CHECK_ITEMS = ["엘리베이터 2대 이상", "누수·결로 흔적 없음", "주차 여유", "인근 혐오시설 없음", "일조·조망 양호", "관리상태 양호"]

RTMS_ENDPOINTS = {
    ("아파트", "매매"): "RTMSDataSvcAptTradeDev/getRTMSDataSvcAptTradeDev",
    ("아파트", "전세"): "RTMSDataSvcAptRent/getRTMSDataSvcAptRent",
    ("아파트", "월세"): "RTMSDataSvcAptRent/getRTMSDataSvcAptRent",
    ("빌라·연립·다세대", "매매"): "RTMSDataSvcRHTrade/getRTMSDataSvcRHTrade",
    ("빌라·연립·다세대", "전세"): "RTMSDataSvcRHRent/getRTMSDataSvcRHRent",
    ("빌라·연립·다세대", "월세"): "RTMSDataSvcRHRent/getRTMSDataSvcRHRent",
}

# ── API 함수 ──────────────────────────────────────────────

def search_address(address, juso_key):
    try:
        # 이 API(business.juso.go.kr)는 공용 SESSION의 자동 재시도를 쓰지 않는다.
        # 짧은 시간에 같은 IP에서 재연결을 반복하면 오히려 서버 쪽에서 더 막힐 수 있어서,
        # 여기서는 순수 requests.get으로 딱 1번만 시도한다.
        r = requests.get("https://business.juso.go.kr/addrlink/addrLinkApi.do",
            params={"confmKey": juso_key, "currentPage": 1, "countPerPage": 10,
                    "keyword": address, "resultType": "json", "addInfoYn": "Y"}, timeout=25)
        data = r.json()
    except Exception as e:
        return None, friendly_error(e)
    common = data.get("results", {}).get("common", {})
    if common.get("errorCode") != "0":
        return None, f"도로명주소 API 에러: {common.get('errorMessage')}"
    juso_list = data.get("results", {}).get("juso", [])
    if not juso_list:
        return None, "주소 검색 결과 없음"
    j = juso_list[0]
    adm_cd = j.get("admCd", "")
    mt_yn = j.get("mtYn", "0")
    bun = str(j.get("lnbrMnnm", "0")).zfill(4)
    ji = str(j.get("lnbrSlno", "0")).zfill(4)
    return {
        "roadAddr": j.get("roadAddr"), "rn": j.get("rn"), "emdNm": j.get("emdNm"),
        "admCd": adm_cd, "sigunguCd": adm_cd[:5], "bjdongCd": adm_cd[5:10],
        "bun": bun, "ji": ji,
        "platGbCd": "1" if mt_yn == "1" else "0",
        "pnu": adm_cd + ("2" if mt_yn == "1" else "1") + bun + ji,
    }, None


def _fetch_bldg_endpoint(endpoint, j, bldg_key):
    """건축물대장 하위 엔드포인트 공통 호출. 반환: (item, error_msg, dong_count)"""
    try:
        r = SESSION.get(f"https://apis.data.go.kr/1613000/BldRgstHubService/{endpoint}",
            params={"serviceKey": bldg_key, "sigunguCd": j["sigunguCd"],
                    "bjdongCd": j["bjdongCd"], "platGbCd": j["platGbCd"],
                    "bun": j["bun"], "ji": j["ji"],
                    "_type": "json", "numOfRows": "10", "pageNo": "1"}, timeout=30)
        items = r.json().get("response", {}).get("body", {}).get("items", {})
    except Exception as e:
        return None, friendly_error(e), 0
    if not items:
        return None, "조회 결과 없음", 0
    item = items.get("item")
    dong_count = len(item) if isinstance(item, list) else (1 if item else 0)
    if isinstance(item, list):
        item = item[0] if item else None
    if not item:
        return None, "응답에 데이터 없음 (item 없음)", 0
    return item, None, dong_count


def get_building_info(j, bldg_key):
    """건축물대장 조회. 반환: (item, error_msg, note)
    1순위로 개별 동 표제부(getBrTitleInfo)를 시도하고, 결과가 없으면(주로 여러 동으로
    이뤄진 아파트 단지에서 동을 특정 안 했을 때 발생) 단지 전체 총괄표제부(getBrRecapTitleInfo)로
    자동 재시도한다."""
    item, err, dong_count = _fetch_bldg_endpoint("getBrTitleInfo", j, bldg_key)
    if item:
        note = None
        if dong_count > 1:
            note = (f"⚠️ 같은 지번에 건물 {dong_count}동이 조회됨 — 첫 번째 동 기준 정보입니다. "
                    "위반건축물 여부는 동마다 다를 수 있으니 정확한 동을 지정해 재확인이 필요합니다.")
        return item, None, note

    item2, err2, _ = _fetch_bldg_endpoint("getBrRecapTitleInfo", j, bldg_key)
    if item2:
        return item2, None, ("ℹ️ 개별 동 표제부에서 데이터를 못 찾아 단지 전체 총괄표제부 기준으로 "
                             "가져왔어요. 층수 등 동별 세부사항은 실제와 다를 수 있습니다.")

    _bun_disp = j["bun"].lstrip("0") or "0"
    _ji_disp = j["ji"].lstrip("0")
    _jibun_disp = f"{_bun_disp}-{_ji_disp}" if _ji_disp != "0" and _ji_disp else _bun_disp
    _mt = "산" if j.get("platGbCd") == "1" else ""
    return None, (f"건축물대장 조회 결과 없음 (표제부·총괄표제부 둘 다 데이터 없음). "
                  f"조회에 사용한 지번: {_mt}{_jibun_disp} (법정동코드 {j['bjdongCd']}) — "
                  f"정부24·세움터에서 이 지번으로 건축물대장이 실제 존재하는지 직접 확인해보세요. "
                  f"도로명주소 API가 반환한 지번이 실제 등록 지번과 다를 수 있어요."), None


def parse_violation(bldg):
    """위반건축물 여부 파싱. 'Y' / 'N' / None(정보없음·확인불가) 중 하나를 반환한다.
    API가 빈 값·누락 값을 줄 때 이를 '위반 없음(N)'으로 단정하지 않기 위한 헬퍼."""
    if not bldg:
        return None
    v = (bldg.get("vltnBldYn") or "").strip().upper()
    return v if v in ("Y", "N") else None


def map_type(purpose, max_floor=None):
    if not purpose: return None
    p = purpose.strip()
    if "아파트" in p: return "아파트"
    if "공동주택" in p:
        return "아파트" if (max_floor and max_floor >= 5) else "빌라·연립·다세대"
    if "연립" in p or "다세대" in p: return "빌라·연립·다세대"
    return "기타"


def geocode_address(road_addr, kakao_key):
    """도로명주소 → 위도/경도 (카카오 Geocoding)"""
    try:
        r = SESSION.get(
            "https://dapi.kakao.com/v2/local/search/address.json",
            params={"query": road_addr},
            headers={"Authorization": f"KakaoAK {kakao_key}"},
            timeout=20
        )
        resp_data = r.json()
        docs = resp_data.get("documents", [])
        if docs:
            return float(docs[0]["y"]), float(docs[0]["x"]), None
        err_code = resp_data.get("code")
        if err_code:
            return None, None, f"카카오 API 오류 {err_code}: {resp_data.get('msg','')}"
        return None, None, f"결과 없음 (쿼리: {road_addr[:40]})"
    except Exception as e:
        return None, None, friendly_error(e)


def get_market_price(j, mtype, deal_type, bldg_key, months=6, early_stop_n=20, time_budget=40):
    """실거래가 조회. time_budget(초)을 넘기면 데이터가 희소해도 그 시점까지 모은 것만으로 반환.
    (빌라·월세처럼 거래가 희소한 유형은 조기 종료 기준에 못 미쳐 6개월을 다 훑는 경우가 있는데,
    그때마다 매달 API가 느리면 전체가 지나치게 오래 걸리는 걸 막기 위함)"""
    endpoint = RTMS_ENDPOINTS.get((mtype, deal_type))
    if not endpoint or len(j["sigunguCd"]) != 5: return None
    url = f"https://apis.data.go.kr/1613000/{endpoint}"
    today = datetime.now()
    same_road, same_dong = [], []
    months_scanned = 0
    started = time.monotonic()

    for i in range(months):
        if time.monotonic() - started > time_budget:
            break
        yr, mo = today.year, today.month - i
        while mo <= 0: mo += 12; yr -= 1
        try:
            r = SESSION.get(url, params={"serviceKey": bldg_key, "LAWD_CD": j["sigunguCd"],
                "DEAL_YMD": f"{yr}{mo:02d}", "pageNo": "1", "numOfRows": "1000"}, timeout=15)
            root = ET.fromstring(r.content)
            rc = root.find(".//resultCode")
            if rc is None or rc.text not in ("00", "000"): continue
            months_scanned += 1
            for item in root.findall(".//item"):
                d = {c.tag: (c.text or "").strip() for c in item}
                try:
                    is_road = j["rn"] and j["rn"] in d.get("도로명", "")
                    is_dong = j["emdNm"] and j["emdNm"] in d.get("법정동", "")
                    if not is_road and not is_dong: continue
                    area = float(d.get("전용면적", 0))
                    if area <= 0: continue
                    pyeong = area / 3.3058
                    price = None
                    if deal_type == "매매":
                        a = d.get("거래금액", "").replace(",", "")
                        price = int(a) * 10000 / pyeong if a else None
                    elif deal_type == "전세":
                        if int(d.get("월세금액", "0").replace(",", "") or "0") > 0: continue
                        dep = d.get("보증금액", "").replace(",", "")
                        price = int(dep) * 10000 / pyeong if dep else None
                    elif deal_type == "월세":
                        w = int(d.get("월세금액", "0").replace(",", "") or "0")
                        price = w * 10000 / pyeong if w > 0 else None
                    if price and price > 0:
                        (same_road if is_road else same_dong).append(price)
                except Exception: continue
        except Exception: continue

        # 도로명 기준 표본이 충분히 모였고 최소 2개월은 훑었으면 이후 달은 조회하지 않음
        # (해외 서버 → 국내 API 호출 왕복이 느려서, 불필요한 호출을 줄이는 게 체감 속도에 큰 영향)
        if len(same_road) >= early_stop_n and months_scanned >= 2:
            break

    pool = same_road or same_dong
    if not pool: return None
    basis = "도로명" if same_road else "법정동"
    avg = int(sum(pool) / len(pool))
    return {"avg": avg, "count": len(pool),
            "basis": f"{deal_type} · 같은 {basis} {len(pool)}건 (최근 {months_scanned}개월 조회)"}


# ── 노션 스키마 인식 / 저장 / 수정 ────────────────────────

@st.cache_data(ttl=600)
def get_db_schema(notion_token, db_id):
    """노션 DB에 존재하는 속성명→타입 맵 (없는 컬럼 저장 시도 방지용)"""
    try:
        r = SESSION.get(f"https://api.notion.com/v1/databases/{db_id}",
            headers={"Authorization": f"Bearer {notion_token}", "Notion-Version": "2022-06-28"},
            timeout=30)
        props = r.json().get("properties", {})
        return {name: p.get("type") for name, p in props.items()}
    except Exception:
        return {}


def filter_props(props, schema):
    """스키마에 있는 속성만 남김 (스키마 조회 실패 시 그대로 반환)"""
    if not schema:
        return props
    return {k: v for k, v in props.items() if k in schema}


def update_notion_page(notion_token, page_id, props):
    SESSION.patch(
        f"https://api.notion.com/v1/pages/{page_id}",
        headers={"Authorization": f"Bearer {notion_token}",
                 "Notion-Version": "2022-06-28", "Content-Type": "application/json"},
        json={"properties": props}, timeout=25)


def relookup_and_update(notion_token, db_id, schema, row, juso_key, bldg_key):
    """기존 매물 1건을 재조회 → 건축물대장·실거래 시세를 다시 받아 노션 갱신.
    반환: (갱신된 필드 수, 사람이 읽을 메시지)"""
    addr = (row.get("주소") or "").strip()
    if not addr:
        return 0, "주소가 없어 재조회할 수 없어요."
    juso, err = search_address(addr, juso_key)
    if err or not juso:
        return 0, f"도로명주소 조회 실패: {err or '결과 없음'}"

    bldg, berr, bnote = get_building_info(juso, bldg_key)
    props, mtype = {}, None
    if juso.get("roadAddr"):
        props["도로명 주소"] = {"rich_text": [{"text": {"content": juso["roadAddr"]}}]}
    if juso.get("pnu"):
        props["PNU 코드"] = {"rich_text": [{"text": {"content": juso["pnu"]}}]}
    if bldg:
        use_apr = str(bldg.get("useAprDay", ""))
        if use_apr and len(use_apr) >= 4:
            props["준공년도"] = {"number": int(use_apr[:4])}
        if bldg.get("totArea"):
            props["연면적(㎡)"] = {"number": float(bldg["totArea"])}
        if bldg.get("bcRat"):
            props["건폐율(%)"] = {"number": float(bldg["bcRat"])}
        if bldg.get("vlRat"):
            props["용적률(%)"] = {"number": float(bldg["vlRat"])}
        if bldg.get("grndFlrCnt"):
            props["최고층수"] = {"number": int(bldg["grndFlrCnt"])}
        if bldg.get("mainPurpsCdNm"):
            purpose = str(bldg["mainPurpsCdNm"])
            props["주용도"] = {"rich_text": [{"text": {"content": purpose}}]}
            mtype = map_type(purpose, int(bldg.get("grndFlrCnt") or 0) or None)
            if mtype:
                props["매물유형"] = {"select": {"name": mtype}}
        vio = parse_violation(bldg)
        if vio is not None:
            props["위반건축물"] = {"checkbox": vio == "Y"}
        # vio가 None(정보없음/확인불가)이면 필드를 건드리지 않는다 —
        # "정보없음"을 "위반없음"으로 오기록하지 않기 위함.

    deal_type = row.get("거래방식")
    market = None
    if mtype and deal_type and (mtype, deal_type) in RTMS_ENDPOINTS:
        market = get_market_price(juso, mtype, deal_type, bldg_key)
    if market:
        props["최근 거래 평당가(원)"] = {"number": market["avg"]}
        props["비교 거래 건수"] = {"number": market["count"]}
        props["비교 기준"] = {"rich_text": [{"text": {"content": market["basis"]}}]}

    props = filter_props(props, schema)
    if not props:
        msg = "갱신할 정보를 찾지 못했어요."
        if berr:
            msg += f" (건축물대장: {berr})"
        return 0, msg
    update_notion_page(notion_token, row["page_id"], props)

    parts = []
    if market:
        parts.append(f"실거래 평당가 {fmt_eok_won(market['avg'])}")
    elif deal_type:
        parts.append("실거래 비교 없음")
    if bldg:
        parts.append("건축물대장 갱신")
    if bnote:
        parts.append("⚠️ 다동(多棟) 매칭 — 정확한 동 재확인 필요")
    return len(props), " · ".join(parts) or "갱신 완료"


def save_to_notion(notion, db_id, schema, name, address, deal_type, price, area, juso, bldg, market, monthly_rent=None, extra_details=None):
    """노션 DB에 새 페이지 생성.
    월세 거래의 경우 price는 보증금, monthly_rent는 매월 월세 금액을 의미한다.
    extra_details: {"방향": str, "관리비(만원)": int, "입주가능일": date|None,
                     "총주차대수": int, "세대당주차대수": int, "방수욕실수": str, "특징메모": str} 형태의 선택 필드."""
    props = {
        "매물명": {"title": [{"text": {"content": name}}]},
        "주소": {"rich_text": [{"text": {"content": address}}]},
    }
    if deal_type:
        props["거래방식"] = {"select": {"name": deal_type}}
    if price:
        props["호가(만원)"] = {"number": price}
    if deal_type == "월세" and monthly_rent:
        props["월세"] = {"number": monthly_rent}
    if area:
        props["전용면적(평)"] = {"number": float(area)}
    if extra_details:
        if extra_details.get("방향"):
            props["방향"] = {"rich_text": [{"text": {"content": extra_details["방향"]}}]}
        if extra_details.get("관리비(만원)"):
            props["관리비(만원)"] = {"number": extra_details["관리비(만원)"]}
        if extra_details.get("입주가능일"):
            props["입주가능일"] = {"date": {"start": extra_details["입주가능일"].isoformat()}}
        if extra_details.get("총주차대수"):
            props["총주차대수"] = {"number": extra_details["총주차대수"]}
        if extra_details.get("세대당주차대수"):
            props["세대당주차대수"] = {"number": extra_details["세대당주차대수"]}
        if extra_details.get("방수욕실수"):
            props["방수욕실수"] = {"rich_text": [{"text": {"content": extra_details["방수욕실수"]}}]}
        if extra_details.get("특징메모"):
            props["특징메모"] = {"rich_text": [{"text": {"content": extra_details["특징메모"]}}]}
    if juso:
        props["도로명 주소"] = {"rich_text": [{"text": {"content": juso["roadAddr"]}}]}
        props["PNU 코드"] = {"rich_text": [{"text": {"content": juso["pnu"]}}]}
    if bldg:
        use_apr = str(bldg.get("useAprDay", ""))
        if use_apr and len(use_apr) >= 4:
            props["준공년도"] = {"number": int(use_apr[:4])}
        if bldg.get("totArea"):
            props["연면적(㎡)"] = {"number": float(bldg["totArea"])}
        if bldg.get("bcRat"):
            props["건폐율(%)"] = {"number": float(bldg["bcRat"])}
        if bldg.get("vlRat"):
            props["용적률(%)"] = {"number": float(bldg["vlRat"])}
        if bldg.get("grndFlrCnt"):
            props["최고층수"] = {"number": int(bldg["grndFlrCnt"])}
        if bldg.get("mainPurpsCdNm"):
            purpose = str(bldg["mainPurpsCdNm"])
            props["주용도"] = {"rich_text": [{"text": {"content": purpose}}]}
            mtype = map_type(purpose, int(bldg.get("grndFlrCnt") or 0) or None)
            if mtype:
                props["매물유형"] = {"select": {"name": mtype}}
        vio = parse_violation(bldg)
        if vio is not None:
            props["위반건축물"] = {"checkbox": vio == "Y"}
        # vio가 None이면 저장하지 않음 (정보없음 → 위반없음 오기록 방지)
    if market:
        props["최근 거래 평당가(원)"] = {"number": market["avg"]}
        props["비교 거래 건수"] = {"number": market["count"]}
        props["비교 기준"] = {"rich_text": [{"text": {"content": market["basis"]}}]}
    # 신규 기본값 (스키마에 있을 때만)
    props["상태"] = {"select": {"name": "검토중"}}
    return notion.pages.create(parent={"database_id": db_id},
                               properties=filter_props(props, schema))


def load_notion_list(notion_token, db_id):
    """노션 DB 매물 목록 조회 (직접 HTTP).
    노션 API는 한 번에 최대 100건만 반환하므로, has_more가 true인 동안
    start_cursor를 이어받아 반복 호출한다."""
    rows = []
    _headers = {"Authorization": f"Bearer {notion_token}",
                "Notion-Version": "2022-06-28", "Content-Type": "application/json"}
    _body = {"sorts": [{"timestamp": "created_time", "direction": "descending"}], "page_size": 100}
    while True:
        resp = SESSION.post(
            f"https://api.notion.com/v1/databases/{db_id}/query",
            headers=_headers, json=_body, timeout=30)
        data = resp.json()
        for p in data.get("results", []):
            pr = p["properties"]
            def txt(k):
                prop = pr.get(k, {})
                items = prop.get("title") or prop.get("rich_text") or []
                return items[0]["plain_text"] if items else ""
            def num(k): return pr.get(k, {}).get("number")
            def sel(k):
                s = pr.get(k, {}).get("select"); return s["name"] if s else ""
            def chk(k): return pr.get(k, {}).get("checkbox", False)
            def dt(k):
                d = pr.get(k, {}).get("date"); return d["start"] if d else ""
            def msel(k): return [o["name"] for o in pr.get(k, {}).get("multi_select", [])]
            rows.append({
                "매물명": txt("매물명"), "주소": txt("주소"),
                "거래방식": sel("거래방식"), "호가": num("호가(만원)"), "월세": num("월세"),
                "매물유형": sel("매물유형"), "준공년도": num("준공년도"),
                "최고층수": num("최고층수"), "평당가(원)": num("최근 거래 평당가(원)"),
                "비교기준": txt("비교 기준"), "전용면적(평)": num("전용면적(평)"),
                "상태": sel("상태"), "관심": chk("관심"), "평점": num("평점"),
                "메모": txt("메모"), "방문일": dt("방문일"), "임장체크": msel("임장체크"),
                "위도": num("위도"), "경도": num("경도"),
                "방향": txt("방향"), "관리비(만원)": num("관리비(만원)"),
                "입주가능일": dt("입주가능일"), "총주차대수": num("총주차대수"),
                "세대당주차대수": num("세대당주차대수"),
                "방수욕실수": txt("방수욕실수"), "특징메모": txt("특징메모"),
                "page_id": p["id"],
            })
        if not data.get("has_more"):
            break
        _body["start_cursor"] = data["next_cursor"]
    return rows


# ── 계산 / 포맷 헬퍼 ──────────────────────────────────────

def compute_gap(r):
    """시세 대비 호가 갭(%). 양수=저평가. 전용면적(평) 필요."""
    area, hoga, sise = r.get("전용면적(평)"), r.get("호가"), r.get("평당가(원)")
    if not (area and hoga and sise) or r.get("거래방식") == "월세":
        return None
    hoga_py = hoga * 10000 / area
    return (sise - hoga_py) / sise * 100


def fmt_eok(manwon):
    if manwon is None: return "—"
    e = manwon / 10000
    if e >= 10: return f"{e:.0f}억" if e == int(e) else f"{e:.1f}억"
    if e >= 1: return f"{e:.1f}억"
    return f"{int(manwon):,}만"


def fmt_eok_won(won):
    if won is None: return "—"
    return f"{won / 1e8:.2f}억"


def extract_dong(addr):
    if not addr: return None
    m = re.findall(r"([가-힣]+\d*동)", addr)
    return m[-1] if m else None


def badge(text, color):
    return (f"<span style='font-size:11px;font-weight:700;color:{color};"
            f"background:{color}1a;padding:3px 9px;border-radius:6px;'>{text}</span>")


def calc_affordability(annual_income, existing_monthly_payment, cash,
                        dsr_limit_pct, ltv_limit_pct, rate_pct, stress_pct, years):
    """대출한도·매수가능액 계산 (모든 금액 단위: 만원).
    반환: dict(max_loan, max_price, monthly_payment_real, extra_cost, bottleneck, dsr_loan_limit)"""
    dsr_annual_limit = max(annual_income * (dsr_limit_pct / 100) - existing_monthly_payment * 12, 0)
    dsr_monthly_limit = dsr_annual_limit / 12
    n = max(years, 1) * 12
    stress_r = max(rate_pct + stress_pct, 0) / 100 / 12

    if stress_r > 0:
        dsr_loan_limit = dsr_monthly_limit * (1 - (1 + stress_r) ** -n) / stress_r
    else:
        dsr_loan_limit = dsr_monthly_limit * n

    ltv_ratio = max(ltv_limit_pct, 0) / 100
    price_if_dsr = cash + dsr_loan_limit
    ltv_used_at_dsr_price = (dsr_loan_limit / price_if_dsr) if price_if_dsr > 0 else 0

    if ltv_ratio <= 0 or ltv_used_at_dsr_price <= ltv_ratio:
        bottleneck = "DSR"
        max_loan = dsr_loan_limit
        max_price = price_if_dsr
    else:
        bottleneck = "LTV"
        max_price = cash / (1 - ltv_ratio) if ltv_ratio < 1 else float("inf")
        max_loan = max_price * ltv_ratio

    real_r = rate_pct / 100 / 12
    if real_r > 0 and n > 0:
        monthly_payment_real = max_loan * real_r / (1 - (1 + real_r) ** -n)
    else:
        monthly_payment_real = max_loan / n if n else 0

    if max_price <= 60000:
        acq_tax_rate = 0.011
    elif max_price <= 90000:
        acq_tax_rate = 0.02
    else:
        acq_tax_rate = 0.03
    extra_cost = max_price * acq_tax_rate + max_price * 0.005  # 취득세(간이) + 중개보수(중간값 0.5%)

    return {
        "max_loan": max_loan, "max_price": max_price,
        "monthly_payment_real": monthly_payment_real,
        "extra_cost": extra_cost, "bottleneck": bottleneck,
        "dsr_loan_limit": dsr_loan_limit,
    }


def load_guide_sections(path):
    """마크다운 가이드 파일을 '## ' 헤더 기준으로 섹션 분리.
    반환: [(제목, 본문), ...] 첫 요소는 최상단 인트로(# 제목 + 요약 블록)."""
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    parts = re.split(r"\n(?=## )", text)
    sections = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        lines = p.split("\n", 1)
        title = lines[0].lstrip("#").strip()
        body = lines[1] if len(lines) > 1 else ""
        sections.append((title, body))
    return sections


_GUIDE_CHECKLIST_RE = re.compile(r"^- \[([ xX])\] (.*)$")


def guide_item_key(text):
    """체크리스트 항목 텍스트로부터 안정적인 짧은 키 생성 (문서 내용이 안 바뀌는 한 유지됨)."""
    return hashlib.md5(text.encode("utf-8")).hexdigest()[:12]


FINANCE_PROFILE_DEFAULT = {
    "annual_income": 0, "existing_monthly_payment": 0, "cash_actual": 0,
    "parent_support": 0, "parent_support_type": "미정",
    "dsr_limit_pct": 40, "ltv_limit_pct": 40, "rate_pct": 4.5, "stress_pct": 1.5, "years": 30,
    "target_price": 0, "monthly_saving": 0, "sub_target_date": "2027-07-01",
    "funding_items": {"예적금": 0, "청약저축": 0, "주식매각": 0, "비상금적립분": 0, "부모님": 0, "주택담보대출": 0},
}


def load_finance_profile(path):
    profile = json.loads(json.dumps(FINANCE_PROFILE_DEFAULT))  # deep copy
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            profile.update({k: v for k, v in loaded.items() if k != "funding_items"})
            if "funding_items" in loaded:
                profile["funding_items"].update(loaded["funding_items"])
        except Exception:
            pass
    return profile


def save_finance_profile(path, profile):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(profile, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def render_affordability_editor(profile_path):
    """'내 구매력 현황' 편집 위젯. 저장된 재정 프로필을 읽고 수정 가능하게 하며,
    calc_affordability()로 대출한도·매수가능가를 실시간 계산해 보여준다."""
    profile = load_finance_profile(profile_path)
    with st.form("afford_profile_form"):
        f1, f2 = st.columns(2)
        with f1:
            cash = st.number_input("자기자금 (실질, 만원)", min_value=0,
                                   value=int(profile["cash_actual"]), step=100)
            parent = st.number_input("부모님 지원금 (만원)", min_value=0,
                                     value=int(profile["parent_support"]), step=100)
            parent_type = st.selectbox("성격", ["미정", "차용", "증여"],
                index=["미정", "차용", "증여"].index(profile.get("parent_support_type", "미정")))
        with f2:
            income = st.number_input("연소득 (만원)", min_value=0,
                                     value=int(profile["annual_income"]), step=10)
            existing = st.number_input("기존 대출 월 상환액 (만원)", min_value=0,
                                       value=int(profile["existing_monthly_payment"]), step=1)
            target = st.number_input("비교할 목표 매매가 (만원)", min_value=0,
                                     value=int(profile.get("target_price", 0)), step=1000,
                                     help="예: 문정동 궁리치웰 25평 실거래가 등")
        f3, f4 = st.columns(2)
        with f3:
            dsr = st.slider("DSR 한도 (%)", 10, 70, int(profile["dsr_limit_pct"]), key="afford_dsr")
            rate = st.number_input("대출금리 (%)", min_value=0.0,
                                   value=float(profile["rate_pct"]), step=0.1)
        with f4:
            ltv = st.slider("LTV 한도 (%)", 10, 90, int(profile["ltv_limit_pct"]), key="afford_ltv",
                           help="서울 등 투기과열지구 일반 무주택자는 40%, 생애최초 구매자는 "
                                "최대 80%까지 완화돼요 (2026년 기준). 본인 조건에 맞게 조정하세요.")
            stress = st.number_input("스트레스금리 가산 (%p)", min_value=0.0,
                                     value=float(profile["stress_pct"]), step=0.1)
        if st.form_submit_button("💾 저장", type="primary"):
            profile.update({
                "cash_actual": cash, "parent_support": parent, "parent_support_type": parent_type,
                "annual_income": income, "existing_monthly_payment": existing, "target_price": target,
                "dsr_limit_pct": dsr, "ltv_limit_pct": ltv, "rate_pct": rate, "stress_pct": stress,
            })
            save_finance_profile(profile_path, profile)
            st.success("✅ 저장 완료! 자금 계산기·현실적 로드맵에도 바로 반영돼요.")
            st.rerun()

    result_incl = calc_affordability(profile["annual_income"], profile["existing_monthly_payment"],
                                     profile["cash_actual"] + profile["parent_support"],
                                     profile["dsr_limit_pct"], profile["ltv_limit_pct"],
                                     profile["rate_pct"], profile["stress_pct"], profile["years"])
    result_excl = calc_affordability(profile["annual_income"], profile["existing_monthly_payment"],
                                     profile["cash_actual"],
                                     profile["dsr_limit_pct"], profile["ltv_limit_pct"],
                                     profile["rate_pct"], profile["stress_pct"], profile["years"])
    st.markdown("<div style='height:6px;'></div>", unsafe_allow_html=True)
    g1, g2, g3 = st.columns(3)
    g1.metric("대출 가능 한도", fmt_eok(result_incl["max_loan"]))
    g2.metric("이론상 최대 매수 가능가", fmt_eok(result_incl["max_price"]))
    g3.metric("부모님 자금 제외 시", fmt_eok(result_excl["max_price"]))
    if profile.get("target_price"):
        gap = profile["target_price"] - result_incl["max_price"]
        if gap > 0:
            st.warning(f"⚠️ 목표 매매가 {fmt_eok(profile['target_price'])} 기준, 자기자금 약 "
                      f"{fmt_eok(gap)} 더 필요해요.")
        else:
            st.success(f"✅ 목표 매매가 {fmt_eok(profile['target_price'])} 이내로 구매 가능한 수준이에요.")
    return profile


def render_funding_plan_editor(profile_path):
    """'자금조달계획서 — 내 경우' 편집 위젯. 항목별 금액을 입력받아 합계·목표가 대비 갭을 보여준다."""
    profile = load_finance_profile(profile_path)
    labels = ["예적금", "청약저축", "주식매각", "비상금적립분", "부모님", "주택담보대출"]
    items = profile.get("funding_items", {})
    with st.form("funding_plan_form"):
        vals = {}
        cols = st.columns(3)
        for i, lb in enumerate(labels):
            with cols[i % 3]:
                vals[lb] = st.number_input(f"{lb} (만원)", min_value=0,
                                           value=int(items.get(lb, 0)), step=100, key=f"fund_{lb}")
        if st.form_submit_button("💾 저장", type="primary"):
            profile["funding_items"] = vals
            save_finance_profile(profile_path, profile)
            st.success("✅ 저장 완료!")
            st.rerun()

    total = sum(items.get(lb, 0) for lb in labels)
    st.metric("자금조달 합계", fmt_eok(total))
    if profile.get("target_price"):
        gap = profile["target_price"] - total
        st.caption(f"목표 매매가 대비 {'약 ' + fmt_eok(gap) + ' 부족' if gap > 0 else fmt_eok(-gap) + ' 여유'}"
                  f" (목표가는 '내 구매력 현황'에서 수정)")


def load_guide_progress(path):
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_guide_progress(path, progress):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(progress, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def render_guide_body(body, progress, progress_path):
    """마크다운 본문에서 '- [ ] ' 체크리스트 줄만 실제 st.checkbox로, 나머지는 일반 마크다운으로 렌더링.
    체크 상태는 progress 딕셔너리(항목 텍스트 해시 → bool)에 저장되고, 바뀔 때마다 파일에 즉시 기록됨."""
    buf = []

    def flush():
        if buf:
            st.markdown("\n".join(buf))
            buf.clear()

    for line in body.split("\n"):
        m = _GUIDE_CHECKLIST_RE.match(line.strip())
        if m:
            flush()
            item_text = m.group(2)
            item_key = guide_item_key(item_text)
            widget_key = f"guide_chk_{item_key}"
            saved_checked = progress.get(item_key, m.group(1).lower() == "x")
            # 이미 이번 세션에서 클릭된 적 있으면 session_state의 최신값을 라벨에 반영해
            # 클릭 즉시(리렌더 지연 없이) 취소선이 붙도록 함
            current_checked = st.session_state.get(widget_key, saved_checked)
            label = f"~~{item_text}~~" if current_checked else item_text
            new_val = st.checkbox(label, value=saved_checked, key=widget_key)
            if new_val != saved_checked:
                progress[item_key] = new_val
                save_guide_progress(progress_path, progress)
        else:
            buf.append(line)
    flush()


# ── Streamlit UI ─────────────────────────────────────────

st.set_page_config(page_title="PropertyBot", page_icon="🏠", layout="wide", initial_sidebar_state="collapsed")

# APP_PASSWORD 환경변수(.env 또는 Streamlit Secrets)를 설정해두면
# '매수 가이드' 탭을 열 때만 비밀번호를 물어봄 (앱 전체는 그대로 공개 접근).
_APP_PASSWORD = os.getenv("APP_PASSWORD", "")

# Pretendard 웹폰트: Streamlit이 <style> 안의 @import를 차단하므로 <link>로 직접 주입
st.markdown(
    '<link rel="stylesheet" as="style" crossorigin '
    'href="https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/static/pretendard.css">',
    unsafe_allow_html=True)

_CSS = """
html, body, [class*="css"], .stApp, button, input, textarea, select {
    font-family:'Pretendard','Malgun Gothic',sans-serif !important;
}
.stApp { background:#f5f5f7; }
.block-container { padding-top:1.6rem; max-width:1280px; }
h1 { font-weight:800; letter-spacing:-.6px; }
h3, .stSubheader { font-weight:800; letter-spacing:-.3px; }
/* 탭처럼 보이게 만든 라디오 메뉴 (st.tabs는 rerun 시 선택 상태가 초기화되는 문제가 있어 대체) */
div[data-testid="stRadio"] > div[role="radiogroup"] {
    flex-direction:row; gap:2px; border-bottom:1px solid #e8e8ec; flex-wrap:wrap;
}
div[data-testid="stRadio"] label {
    padding:9px 16px; border-radius:8px 8px 0 0; font-weight:600; font-size:14px;
    background:transparent; cursor:pointer; border:none; margin-bottom:-1px;
}
div[data-testid="stRadio"] label > div:first-child { display:none; }
div[data-testid="stRadio"] label:has(input:checked) {
    color:__ACCENT__; border-bottom:2px solid __ACCENT__;
}
/* metric을 카드처럼 */
[data-testid="stMetric"] {
    background:#fff; border:1px solid #ececef; border-radius:13px;
    padding:15px 18px; box-shadow:0 1px 2px rgba(20,20,30,.04);
}
[data-testid="stMetricLabel"] p { color:#9a9aa3; font-weight:700; font-size:12px; }
[data-testid="stMetricValue"] { font-weight:800; letter-spacing:-.5px; }
/* 버튼 */
.stButton button, .stDownloadButton button, .stFormSubmitButton button { border-radius:9px; font-weight:600; }
.stButton button[kind="primary"], .stFormSubmitButton button[kind="primary"] {
    background:__ACCENT__; border-color:__ACCENT__;
}
/* 사이드바 */
[data-testid="stSidebar"] { background:#fff; border-right:1px solid #ececef; display:none; }
/* 입력 위젯 */
[data-baseweb="input"], [data-baseweb="select"] > div, .stTextArea textarea { border-radius:9px !important; }
/* 카드(테두리 컨테이너) */
[data-testid="stVerticalBlockBorderWrapper"] { border-radius:13px; }
"""
st.markdown(f"<style>{_CSS.replace('__ACCENT__', ACCENT)}</style>", unsafe_allow_html=True)

st.title("🏠 PropertyBot")
st.caption("부동산 임장 관리 도구 · 수집 → 비교 → 임장 → 기록")

# 사이드바: API 키
with st.sidebar:
    st.header("⚙️ 설정")
    _env_keys = {
        "NOTION_TOKEN": os.getenv("NOTION_TOKEN", ""),
        "NOTION_DATABASE_ID": os.getenv("NOTION_DATABASE_ID", ""),
        "JUSO_API_KEY": os.getenv("JUSO_API_KEY", ""),
        "BLDG_REG_API_KEY": os.getenv("BLDG_REG_API_KEY", ""),
        "KAKAO_API_KEY": os.getenv("KAKAO_API_KEY", ""),
        "KAKAO_JS_KEY": os.getenv("KAKAO_JS_KEY", ""),
    }
    _all_loaded = all([_env_keys["NOTION_TOKEN"], _env_keys["NOTION_DATABASE_ID"],
                       _env_keys["JUSO_API_KEY"], _env_keys["BLDG_REG_API_KEY"]])

    if _all_loaded:
        st.success("✅ API 키 자동 로드 완료")
        _exp = st.expander("🔑 API 키 확인/수정", expanded=False)
    else:
        st.warning(".env 파일이 없거나 키가 누락되었습니다.")
        _exp = st.container()

    with _exp:
        notion_token = st.text_input("Notion Token", value=_env_keys["NOTION_TOKEN"], type="password")
        db_id = st.text_input("Notion Database ID", value=_env_keys["NOTION_DATABASE_ID"])
        juso_key = st.text_input("도로명주소 API 키", value=_env_keys["JUSO_API_KEY"], type="password")
        bldg_key = st.text_input("건축물대장/실거래가 API 키", value=_env_keys["BLDG_REG_API_KEY"], type="password")
        kakao_key = st.text_input("카카오 REST API 키", value=_env_keys["KAKAO_API_KEY"], type="password")
        kakao_js_key = st.text_input("카카오 JavaScript 키", value=_env_keys["KAKAO_JS_KEY"], type="password")
        st.caption("💡 [공공데이터포털](https://www.data.go.kr) | [노션 API](https://www.notion.so/my-integrations)")
        st.caption("💡 카카오 JS키: [개발자콘솔](https://developers.kakao.com/console/app) → 플랫폼 키")

if not all([notion_token, db_id, juso_key, bldg_key]):
    st.warning("사이드바에서 API 키를 모두 입력해주세요.")
    st.stop()

try:
    from notion_client import Client
    notion = Client(auth=notion_token)
except ImportError:
    st.error("`pip install notion-client` 실행 후 재시작해주세요.")
    st.stop()

schema = get_db_schema(notion_token, db_id)

# 노션 DB에 권장되는 컬럼 안내 (없는 컬럼만 노란불로 표시) ──
_REC_COLS = [
    ("전용면적(평)", "숫자(Number)", "시세갭·평당가 계산에 필수"),
    ("상태", "선택(Select)", "방문예정·관심·검토중·방문완료"),
    ("관심", "체크박스(Checkbox)", "⭐ 즐겨찾기"),
    ("평점", "숫자(Number)", "0~5점 임장 평가"),
    ("방문일", "날짜(Date)", "임장 방문 날짜"),
    ("메모", "텍스트(Text)", "자유 메모"),
    ("임장체크", "다중 선택(Multi-select)", "체크리스트 항목"),
    ("위도", "숫자(Number)", "지도 좌표 캐시"),
    ("경도", "숫자(Number)", "지도 좌표 캐시"),
    ("방향", "텍스트(Text)", "예: 북서향"),
    ("관리비(만원)", "숫자(Number)", "월 관리비"),
    ("입주가능일", "날짜(Date)", "예: 2026-10-25"),
    ("총주차대수", "숫자(Number)", "예: 16"),
    ("세대당주차대수", "숫자(Number)", "예: 1"),
    ("방수욕실수", "텍스트(Text)", "예: 3/2"),
    ("특징메모", "텍스트(Text)", "매물설명·특징 등 자유 기록"),
]
if schema:
    _missing = [c for c in _REC_COLS if c[0] not in schema]
    if _missing:
        with st.expander(f"⚠️ 노션 DB에 추가하면 좋은 컬럼 {len(_missing)}개 — 펼쳐서 확인", expanded=False):
            st.caption("아래 컬럼이 없으면 시세갭·평당가·평점 등이 비어 있게 나옵니다. "
                       "노션 DB 우측 ‘+’로 같은 이름·타입의 속성을 추가하면 자동으로 채워집니다. "
                       "(DB에 없는 컬럼은 저장 시 자동으로 건너뜁니다.)")
            for nm, typ, desc in _missing:
                st.markdown(f"- **{nm}** · `{typ}` — {desc}")

TAB_LABELS = ["📊 대시보드", "➕ 새 매물 입력", "📋 매물 목록", "🗺️ 임장 지도", "📖 매수 가이드", "💰 자금 계산기"]
if "active_tab" not in st.session_state:
    st.session_state["active_tab"] = TAB_LABELS[0]
active_tab = st.radio("메뉴", TAB_LABELS, horizontal=True,
                      label_visibility="collapsed", key="active_tab")

# ════════════════ 탭 0: 대시보드 ════════════════
if active_tab == TAB_LABELS[0]:
    st.subheader("대시보드")

    _fin_path_dash = os.path.join(os.path.dirname(os.path.abspath(__file__)), "financial_profile.json")
    _fp_dash = load_finance_profile(_fin_path_dash)
    try:
        _target_d = datetime.fromisoformat(_fp_dash["sub_target_date"]).date()
    except Exception:
        _target_d = date(2027, 7, 1)
    _dday = (_target_d - date.today()).days
    _dday_txt = f"D-{_dday}" if _dday > 0 else ("D-Day" if _dday == 0 else f"D+{-_dday}")
    _dc1, _dc2 = st.columns([5, 1])
    with _dc1:
        st.markdown(
            f"<div style='background:linear-gradient(135deg,{ACCENT},#5b7ce0);color:#fff;"
            f"border-radius:13px;padding:16px 22px;margin-bottom:12px;'>"
            f"<div style='font-size:12px;font-weight:700;opacity:.85;'>🎯 청약 1순위 목표일 "
            f"({_target_d.isoformat()})</div>"
            f"<div style='font-size:22px;font-weight:800;margin-top:4px;'>{_dday_txt}</div></div>",
            unsafe_allow_html=True)
    with _dc2:
        with st.popover("✏️ 날짜 수정", use_container_width=True):
            _new_d = st.date_input("목표일", value=_target_d, format="YYYY-MM-DD", key="dday_edit")
            if st.button("저장", key="dday_save"):
                _fp_dash["sub_target_date"] = _new_d.isoformat()
                save_finance_profile(_fin_path_dash, _fp_dash)
                st.rerun()

    try:
        rows = load_notion_list(notion_token, db_id)
    except Exception as e:
        rows = []
        st.error(f"데이터 로드 실패: {friendly_error(e)}")

    if not rows:
        st.info("저장된 매물이 없어요. '새 매물 입력'에서 먼저 추가해보세요.")
    else:
        for r in rows:
            r["_gap"] = compute_gap(r)
        total = len(rows)
        under = [r for r in rows if r["_gap"] is not None and r["_gap"] > 0]
        py_vals = [r["호가"] * 10000 / r["전용면적(평)"] for r in rows
                   if r.get("거래방식") == "매매" and r.get("호가") and r.get("전용면적(평)")]
        avg_hoga_py = sum(py_vals) / len(py_vals) if py_vals else None
        done = [r for r in rows if r.get("상태") == "방문완료"]
        ratings = [r["평점"] for r in rows if r.get("평점") is not None]
        avg_star = sum(ratings) / len(ratings) if ratings else None

        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("총 매물", f"{total}건")
        c2.metric("저평가 매물", f"{len(under)}건", help="호가 평당 < 실거래 시세 평당")
        c3.metric("평균 평당 호가", fmt_eok_won(avg_hoga_py))
        c4.metric("임장 진행", f"{len(done)}/{total}")
        c5.metric("평균 평점", f"{avg_star:.1f}" if avg_star is not None else "—")

        st.write("")
        colL, colR = st.columns([1.1, 1])
        with colL:
            st.markdown("##### 시세 대비 저평가 Top 5")
            top = sorted(under, key=lambda r: r["_gap"], reverse=True)[:5]
            if top:
                html = "<div>"
                for i, r in enumerate(top):
                    dc = DEAL_COLORS.get(r.get("거래방식"), "#999")
                    html += (
                        "<div style='display:flex;align-items:center;gap:12px;padding:10px 2px;"
                        "border-bottom:1px solid #f3f3f5;'>"
                        f"<span style='font-weight:800;color:#cfcfd6;width:22px;'>{i+1:02d}</span>"
                        "<div style='flex:1;min-width:0;'>"
                        f"<div style='font-weight:700;font-size:14px;'>{r.get('매물명','')}</div>"
                        f"<div style='font-size:12px;color:#a4a4ac;'>{r.get('주소','')}</div></div>"
                        f"{badge(r.get('거래방식','-'), dc)}"
                        f"<span style='font-weight:800;color:{GAP_GOOD};min-width:56px;"
                        f"text-align:right;'>+{r['_gap']:.1f}%</span></div>")
                html += "</div>"
                st.markdown(html, unsafe_allow_html=True)
            else:
                st.caption("전용면적(평)·호가·평당가가 모두 있는 매물이 있어야 시세갭을 계산할 수 있어요.")
        with colR:
            st.markdown("##### 법정동별 평균 평당 호가 (억)")
            dong_vals = {}
            for r in rows:
                if r.get("호가") and r.get("전용면적(평)"):
                    d = extract_dong(r.get("주소", "")) or "기타"
                    dong_vals.setdefault(d, []).append(r["호가"] * 10000 / r["전용면적(평)"] / 1e8)
            if dong_vals:
                dfd = pd.DataFrame({"평당호가(억)": {k: sum(v) / len(v) for k, v in dong_vals.items()}})
                st.bar_chart(dfd, color=ACCENT, height=240)
            else:
                st.caption("전용면적(평)이 입력된 매물이 필요해요.")

        colA, colB = st.columns(2)
        with colA:
            st.markdown("##### 준공년도 분포")
            years = [int(r["준공년도"]) for r in rows if r.get("준공년도")]
            if years:
                s = pd.Series(years).value_counts().sort_index()
                s.index = s.index.astype(str)
                st.bar_chart(s.rename("매물 수"), color="#a9b6e8", height=220)
            else:
                st.caption("준공년도 데이터가 없어요.")
        with colB:
            st.markdown("##### 거래방식 구성")
            deals = [r.get("거래방식") for r in rows if r.get("거래방식")]
            if deals:
                st.bar_chart(pd.Series(deals).value_counts().rename("매물 수"), color=ACCENT, height=220)
            else:
                st.caption("거래방식 데이터가 없어요.")

# ════════════════ 탭 1: 새 매물 입력 ════════════════
elif active_tab == TAB_LABELS[1]:
    st.subheader("새 매물 입력")
    st.caption("주소만 입력하면 도로명주소·건축물대장·실거래가 API가 자동으로 정보를 채웁니다.")

    col1, col2 = st.columns(2)
    with col1:
        name = st.text_input("매물명 *", placeholder="예) 래미안원베일리 84A")
        address = st.text_input("주소 * (도로명 or 지번)", placeholder="예) 서울 서초구 반포동 19")
    with col2:
        deal_type_sel = st.selectbox("거래방식", ["(선택 안 함)", "매매", "전세", "월세"])
        cc1, cc2 = st.columns(2)
        with cc1:
            price_label = "보증금 (만원)" if deal_type_sel in ("전세", "월세") else "호가 (만원)"
            price_in = st.number_input(price_label, min_value=0, value=0, step=100)
        with cc2:
            area_in = st.number_input("전용면적 (평)", min_value=0.0, value=0.0, step=0.1,
                                      help="입력 시 시세갭(호가 평당 vs 실거래 시세)을 계산합니다.")
        monthly_rent_in = 0
        if deal_type_sel == "전세":
            st.caption("💡 전세는 '보증금' 칸에 전세금 전액을 입력하세요. (호가 개념 없음)")
        if deal_type_sel == "월세":
            st.caption("💡 위 칸엔 보증금을, 아래 칸엔 매월 월세를 각각 입력하세요.")
            monthly_rent_in = st.number_input("월세 (만원)", min_value=0, value=0, step=5,
                                              help="매월 납부하는 월세 금액")

    deal_type = None if deal_type_sel == "(선택 안 함)" else deal_type_sel
    price = price_in if price_in > 0 else None
    area = area_in if area_in > 0 else None
    monthly_rent = monthly_rent_in if (deal_type == "월세" and monthly_rent_in > 0) else None

    with st.expander("🏷️ 상세 정보 (선택 — 네이버부동산 등에서 확인한 값 그대로 적어두면 됨)", expanded=False):
        dc1, dc2, dc3 = st.columns(3)
        with dc1:
            in_direction = st.text_input("방향", placeholder="예) 북서향")
        with dc2:
            in_mgmt_fee = st.number_input("관리비 (만원)", min_value=0, value=0, step=1)
        with dc3:
            in_rooms_baths = st.text_input("방수/욕실수", placeholder="예) 3/2")
        dc4, dc5, dc6 = st.columns(3)
        with dc4:
            in_move_in = st.date_input("입주가능일", value=None, format="YYYY-MM-DD")
        with dc5:
            in_parking_total = st.number_input("총주차대수", min_value=0, value=0, step=1)
        with dc6:
            in_parking_per_unit = st.number_input("세대당주차대수", min_value=0, value=0, step=1)
        in_feature_memo = st.text_area("특징메모", placeholder="매물특징·매물설명 등 자유롭게 붙여넣기 (협의사항 등도 여기에)",
                                       height=80)

    extra_details = {
        "방향": in_direction.strip() if in_direction.strip() else None,
        "관리비(만원)": in_mgmt_fee if in_mgmt_fee > 0 else None,
        "입주가능일": in_move_in if in_move_in else None,
        "총주차대수": in_parking_total if in_parking_total > 0 else None,
        "세대당주차대수": in_parking_per_unit if in_parking_per_unit > 0 else None,
        "방수욕실수": in_rooms_baths.strip() if in_rooms_baths.strip() else None,
        "특징메모": in_feature_memo.strip() if in_feature_memo.strip() else None,
    }

    if st.button("🔍 조회", type="primary", disabled=not (name and address), use_container_width=True):
        with st.spinner("도로명주소 조회 중..."):
            juso, err = search_address(address, juso_key)
        res = {"name": name, "address": address, "deal_type": deal_type,
               "price": price, "area": area, "monthly_rent": monthly_rent,
               "extra_details": extra_details,
               "juso": juso, "bldg": None, "market": None, "juso_err": err}
        if err:
            st.warning(f"⚠️ 도로명주소 조회 실패: {err}\n\n"
                      f"건축물대장·실거래가·정확한 도로명주소는 못 받아왔지만, "
                      f"입력하신 매물명·주소·가격·상세정보는 그대로 저장할 수 있어요. "
                      f"나중에 매물 목록에서 '재조회'를 누르면 이 정보가 채워집니다.")
            st.session_state["lookup"] = res
        else:
            with st.spinner("건축물대장 조회 중..."):
                bldg, berr, bnote = get_building_info(juso, bldg_key)
            res["bldg"] = bldg
            res["bldg_err"] = berr
            res["bldg_note"] = bnote
            if bldg and deal_type:
                mtype = map_type(bldg.get("mainPurpsCdNm", ""),
                                 int(bldg.get("grndFlrCnt") or 0) or None)
                if mtype and (mtype, deal_type) in RTMS_ENDPOINTS:
                    with st.spinner(f"실거래가 조회 중 ({mtype} · {deal_type})..."):
                        res["market"] = get_market_price(juso, mtype, deal_type, bldg_key)
            st.session_state["lookup"] = res

    res = st.session_state.get("lookup")
    if res and res.get("name") == name and res.get("address") == address:
        juso, bldg, market = res["juso"], res["bldg"], res["market"]
        st.markdown("#### 조회 결과")

        if res.get("juso_err"):
            st.warning("⚠️ 도로명주소 조회가 실패한 상태예요 — 아래 저장 버튼으로 기본 정보만 먼저 "
                      "저장하고, 나중에 매물 목록에서 '재조회'로 나머지를 채우시면 됩니다.")

        # 중복 경고
        try:
            existing = load_notion_list(notion_token, db_id)
            dup = [r for r in existing if (r.get("주소", "").strip() == address.strip()
                                           or r.get("매물명", "").strip() == name.strip())]
        except Exception:
            dup = []
        if dup:
            st.warning(f"⚠️ 동일 주소/매물명이 이미 {len(dup)}건 저장돼 있어요. 중복 저장에 주의하세요.")

        use_apr = str(bldg.get("useAprDay", "")) if bldg else ""
        yr = use_apr[:4] if len(use_apr) >= 4 else "?"
        purpose = bldg.get("mainPurpsCdNm", "?") if bldg else "?"
        floors = bldg.get("grndFlrCnt", "?") if bldg else "?"
        mtype = map_type(purpose, int(floors) if str(floors).isdigit() else None) if bldg else None

        m1, m2, m3 = st.columns(3)
        m1.metric("도로명주소", juso["roadAddr"] if juso else "—")
        m2.metric("매물유형", f"{mtype or '?'}")
        m3.metric("준공 / 최고층수", f"{yr}년 · {floors}층" if bldg else "—")
        m4, m5, m6 = st.columns(3)
        if bldg:
            m4.metric("건폐율 / 용적률", f"{bldg.get('bcRat','?')}% / {bldg.get('vlRat','?')}%")
            vio = parse_violation(bldg)
            vio_label = {"Y": "있음 ⚠️", "N": "없음", None: "확인불가"}[vio]
            m5.metric("위반건축물", vio_label)
        else:
            m4.metric("건폐율 / 용적률", "—")
            m5.metric("위반건축물", "확인불가")
        m6.metric("PNU", juso["pnu"] if juso else "—")
        if res.get("bldg_err"):
            st.caption(f"건축물대장: {res['bldg_err']}")
        if res.get("bldg_note"):
            st.caption(res["bldg_note"])
        if res.get("deal_type") == "월세":
            dep_txt = f"{res['price']:,}만원" if res.get("price") else "미입력"
            rent_txt = f"{res['monthly_rent']:,}만원" if res.get("monthly_rent") else "미입력"
            st.caption(f"🏠 보증금 {dep_txt} · 월세 {rent_txt}")
        elif res.get("deal_type") == "전세":
            dep_txt = f"{res['price']:,}만원" if res.get("price") else "미입력"
            st.caption(f"🏠 보증금(전세금) {dep_txt}")

        # 시세 비교
        if market:
            hoga_py = None
            if price and bldg and bldg.get("totArea"):
                hoga_py = int(price * 10000 / (float(bldg["totArea"]) / 3.3058))
            elif price and area:
                hoga_py = int(price * 10000 / area)
            gap = ((market["avg"] - hoga_py) / market["avg"] * 100) if hoga_py else None
            gcol = GAP_GOOD if (gap or 0) > 0 else GAP_BAD
            glabel = (f"{gap:+.1f}% {'저평가' if gap > 0 else '고평가'}") if gap is not None else "면적 입력 시 계산"
            st.markdown(
                "<div style='background:#fff;border:1px solid #ececef;border-radius:12px;"
                "padding:16px 20px;margin-top:8px;'>"
                "<div style='display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;'>"
                "<b style='font-size:14px;'>💰 호가 vs 실거래 시세 (평당)</b>"
                f"<span style='font-weight:800;color:{gcol};background:{gcol}1a;"
                f"padding:4px 12px;border-radius:99px;'>{glabel}</span></div>"
                "<div style='display:flex;gap:32px;'>"
                f"<div><div style='font-size:11px;color:#9a9aa3;font-weight:600;'>호가 평당</div>"
                f"<div style='font-size:20px;font-weight:800;'>{fmt_eok_won(hoga_py)}</div></div>"
                f"<div><div style='font-size:11px;color:#9a9aa3;font-weight:600;'>실거래 시세 평당</div>"
                f"<div style='font-size:20px;font-weight:800;'>{fmt_eok_won(market['avg'])}</div>"
                f"<div style='font-size:10.5px;color:#a4a4ac;margin-top:3px;'>{market['basis']}</div></div>"
                "</div></div>", unsafe_allow_html=True)
        elif deal_type:
            st.caption("비교 가능한 실거래가 없어요.")

        st.write("")
        sc1, sc2 = st.columns([1, 3])
        with sc1:
            if st.button("💾 노션에 저장", type="primary", use_container_width=True):
                with st.spinner("노션에 저장 중..."):
                    try:
                        page = save_to_notion(notion, db_id, schema, name, address,
                                              deal_type, price, area, juso, bldg, market,
                                              monthly_rent, extra_details)
                        st.success(f"✅ 저장 완료! [페이지 열기]({page.get('url','')})")
                        st.session_state.pop("lookup", None)
                    except Exception as e:
                        st.error(f"노션 저장 실패: {friendly_error(e)}")
        with sc2:
            st.caption("결과를 확인한 뒤 저장됩니다. (조회 ↔ 저장 분리)")

# ════════════════ 탭 2: 매물 목록 (마스터–디테일) ════════════════
elif active_tab == TAB_LABELS[2]:
    st.subheader("매물 목록")

    tb1, _tbrest = st.columns([1, 5])
    with tb1:
        if st.button("🔄 새로고침", use_container_width=True):
            st.cache_data.clear(); st.rerun()

    try:
        rows = load_notion_list(notion_token, db_id)
    except Exception as e:
        rows = []
        st.error(f"목록 조회 실패: {friendly_error(e)}")

    if not rows:
        st.info("저장된 매물이 없어요.")
    else:
        _plan = [r for r in rows if r.get("상태") == "방문예정"]
        if _plan:
            _names = "  →  ".join(r["매물명"] for r in _plan)
            st.markdown(
                f"<div style='background:{ACCENT};color:#fff;border-radius:13px;padding:14px 20px;margin-bottom:12px;'>"
                f"<div style='font-size:12px;font-weight:700;opacity:.85;'>오늘의 임장 루트 · 방문예정 {len(_plan)}건</div>"
                f"<div style='font-size:15px;font-weight:800;margin-top:4px;'>{_names}</div></div>",
                unsafe_allow_html=True)

        for r in rows:
            r["_gap"] = compute_gap(r)

        left, right = st.columns([1, 1.9], gap="medium")

        # ── 왼쪽(마스터): 필터 + 매물 리스트 ──
        with left:
            fcol1, fcol2 = st.columns(2)
            with fcol1:
                deal_types = sorted(set(r.get("거래방식", "") for r in rows if r.get("거래방식")))
                deal_filter = st.selectbox("거래방식", ["전체"] + deal_types, label_visibility="collapsed")
            with fcol2:
                sort_option = st.selectbox(
                    "정렬", ["시세갭 높은순", "최신순", "호가 낮은순", "호가 높은순", "평당가 낮은순"],
                    label_visibility="collapsed")

            filtered = rows if deal_filter == "전체" else [r for r in rows if r.get("거래방식") == deal_filter]
            if sort_option == "시세갭 높은순":
                filtered = sorted(filtered, key=lambda r: r["_gap"] if r["_gap"] is not None else -1e9, reverse=True)
            elif sort_option == "호가 낮은순":
                filtered = sorted(filtered, key=lambda r: r.get("호가") or float("inf"))
            elif sort_option == "호가 높은순":
                filtered = sorted(filtered, key=lambda r: r.get("호가") or 0, reverse=True)
            elif sort_option == "평당가 낮은순":
                filtered = sorted(filtered, key=lambda r: r.get("평당가(원)") or float("inf"))

            under_n = sum(1 for r in rows if r["_gap"] is not None and r["_gap"] > 0)
            st.caption(f"총 {len(rows)}개 중 {len(filtered)}개 · 저평가 {under_n}건")

            ids = [r["page_id"] for r in filtered]
            if not ids:
                st.info(f"'{deal_filter}' 매물이 없어요.")
                st.stop()
            if st.session_state.get("list_sel") not in ids:
                st.session_state["list_sel"] = ids[0]

            for r in filtered:
                is_sel = r["page_id"] == st.session_state["list_sel"]
                dc = DEAL_COLORS.get(r.get("거래방식"), "#999")
                gap = r["_gap"]
                gcol = GAP_GOOD if (gap or 0) > 0 else GAP_BAD
                gtxt = f"{gap:+.1f}%" if gap is not None else "—"
                price_txt = fmt_eok(r.get("호가")) if r.get("호가") else "—"
                star = "★" if r.get("관심") else ""
                if is_sel:
                    st.markdown(
                        f"<div style='border:1.5px solid {ACCENT};background:#f4f6fe;border-radius:12px;"
                        f"padding:11px 13px;margin-bottom:8px;'>"
                        f"<div style='display:flex;justify-content:space-between;align-items:center;'>"
                        f"<span style='font-weight:800;font-size:14px;'>{r.get('매물명','')}</span>"
                        f"<span style='color:#c98a00;'>{star}</span></div>"
                        f"<div style='font-size:12px;color:#9a9aa3;margin-top:2px;'>{r.get('주소','')}</div>"
                        f"<div style='display:flex;justify-content:space-between;align-items:center;margin-top:9px;'>"
                        f"<span>{badge(r.get('거래방식','-'), dc)} <b style='font-size:13px;margin-left:4px;'>{price_txt}</b></span>"
                        f"<b style='color:{gcol};'>{gtxt}</b></div></div>",
                        unsafe_allow_html=True)
                else:
                    label = f"{(star + ' ') if star else ''}{r.get('매물명','')}  ·  {price_txt}  ·  {gtxt}"
                    if st.button(label, key="pick_" + r["page_id"], use_container_width=True):
                        st.session_state["list_sel"] = r["page_id"]
                        st.rerun()

        # ── 오른쪽(디테일): 선택 매물 상세 ──
        with right:
            sel = next((r for r in filtered if r["page_id"] == st.session_state["list_sel"]), None)
            if sel:
                sc = STATUS_COLORS.get(sel.get("상태"), "#8a8a93")
                nurl = "https://www.notion.so/" + str(sel["page_id"]).replace("-", "")
                st.markdown(
                    f"<div style='display:flex;justify-content:space-between;align-items:flex-start;gap:14px;'>"
                    f"<div><div style='font-size:24px;font-weight:800;letter-spacing:-.6px;'>{sel.get('매물명','')} "
                    f"<span style='font-size:12px;font-weight:700;color:{sc};background:{sc}1a;padding:3px 10px;"
                    f"border-radius:99px;vertical-align:middle;'>{sel.get('상태') or '검토중'}</span></div>"
                    f"<div style='font-size:13px;color:#8a8a93;margin-top:4px;'>{sel.get('주소','')} · "
                    f"{sel.get('매물유형') or '유형 미상'}</div></div>"
                    f"<a href='{nurl}' target='_blank' style='font-size:12.5px;font-weight:700;color:{ACCENT};"
                    f"text-decoration:none;white-space:nowrap;'>노션에서 열기 ↗</a></div>",
                    unsafe_allow_html=True)

                # 비교 히어로
                hoga_py = int(sel["호가"] * 10000 / sel["전용면적(평)"]) if (sel.get("호가") and sel.get("전용면적(평)")) else None
                sise = sel.get("평당가(원)")
                gap = sel["_gap"]
                if hoga_py and sise:
                    gcol = GAP_GOOD if (gap or 0) > 0 else GAP_BAD
                    glabel = f"{gap:+.1f}% {'저평가' if gap > 0 else '고평가'}"
                    mx = max(hoga_py, sise)
                    hw = hoga_py / mx * 100
                    sw = sise / mx * 100
                    st.markdown(
                        f"<div style='background:#fff;border:1px solid #ececef;border-radius:14px;padding:18px 20px;margin-top:14px;'>"
                        f"<div style='display:flex;justify-content:space-between;align-items:center;margin-bottom:14px;'>"
                        f"<b style='font-size:15px;'>💰 호가 vs 실거래 시세 <span style='color:#9a9aa3;font-weight:600;'>(평당)</span></b>"
                        f"<span style='font-weight:800;color:{gcol};background:{gcol}1a;padding:5px 14px;border-radius:99px;'>{glabel}</span></div>"
                        f"<div style='display:flex;gap:36px;margin-bottom:14px;'>"
                        f"<div><div style='font-size:11px;color:#9a9aa3;font-weight:700;'>호가 평당</div>"
                        f"<div style='font-size:26px;font-weight:800;letter-spacing:-.8px;'>{fmt_eok_won(hoga_py)}</div></div>"
                        f"<div><div style='font-size:11px;color:{ACCENT};font-weight:700;'>실거래 시세 평당</div>"
                        f"<div style='font-size:26px;font-weight:800;letter-spacing:-.8px;color:{ACCENT};'>{fmt_eok_won(sise)}</div>"
                        f"<div style='font-size:11px;color:#a4a4ac;margin-top:2px;'>{sel.get('비교기준') or ''}</div></div></div>"
                        f"<div style='display:flex;align-items:center;gap:10px;margin-bottom:6px;'>"
                        f"<span style='width:52px;font-size:11px;color:#9a9aa3;font-weight:700;text-align:right;'>호가</span>"
                        f"<div style='flex:1;height:13px;background:#f0f0f3;border-radius:99px;overflow:hidden;'>"
                        f"<div style='height:100%;width:{hw:.0f}%;background:#c3ccef;border-radius:99px;'></div></div></div>"
                        f"<div style='display:flex;align-items:center;gap:10px;'>"
                        f"<span style='width:52px;font-size:11px;color:{ACCENT};font-weight:700;text-align:right;'>실거래</span>"
                        f"<div style='flex:1;height:13px;background:#f0f0f3;border-radius:99px;overflow:hidden;'>"
                        f"<div style='height:100%;width:{sw:.0f}%;background:{ACCENT};border-radius:99px;'></div></div></div>"
                        f"</div>", unsafe_allow_html=True)
                else:
                    st.info("전용면적(평)·호가·평당가가 모두 있어야 시세 비교가 나와요. 아래 '재조회'로 평당가를 받아오세요.")

                # 건축물대장 타일
                area_txt = f"{sel['전용면적(평)']:.1f}평" if sel.get("전용면적(평)") else "—"
                tiles = [
                    ("준공년도", f"{int(sel['준공년도'])}년" if sel.get("준공년도") else "—"),
                    ("최고층수", f"{int(sel['최고층수'])}층" if sel.get("최고층수") else "—"),
                    ("전용면적", area_txt),
                    ("실거래 평당", fmt_eok_won(sise) if sise else "—"),
                    ("거래방식", sel.get("거래방식") or "—"),
                    ("시세갭", f"{gap:+.1f}%" if gap is not None else "—"),
                ]
                tile_html = "<div style='display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:12px;'>"
                for _lb, _vl in tiles:
                    tile_html += (
                        f"<div style='background:#fff;border:1px solid #ececef;border-radius:12px;padding:12px 14px;'>"
                        f"<div style='font-size:11px;color:#9a9aa3;font-weight:700;'>{_lb}</div>"
                        f"<div style='font-size:16px;font-weight:800;margin-top:3px;'>{_vl}</div></div>")
                tile_html += "</div>"
                st.markdown(tile_html, unsafe_allow_html=True)

                # 매물 상세 정보 (방향·관리비·입주가능일·주차대수·방수욕실수 중 하나라도 있으면 표시)
                _park_total, _park_unit = sel.get("총주차대수"), sel.get("세대당주차대수")
                if _park_total and _park_unit:
                    _park_txt = f"{int(_park_total)}대(세대당 {int(_park_unit)}대)"
                elif _park_total:
                    _park_txt = f"{int(_park_total)}대"
                else:
                    _park_txt = None
                _detail_items = [
                    ("방향", sel.get("방향")),
                    ("관리비", f"{int(sel['관리비(만원)']):,}만원" if sel.get("관리비(만원)") else None),
                    ("입주가능일", sel.get("입주가능일") or None),
                    ("총주차대수", _park_txt),
                    ("방수/욕실수", sel.get("방수욕실수")),
                ]
                _detail_items = [(k, v) for k, v in _detail_items if v]
                if _detail_items:
                    _dh = "<div style='display:flex;flex-wrap:wrap;gap:8px;margin-top:10px;'>"
                    for _k, _v in _detail_items:
                        _dh += (f"<span style='font-size:12px;background:#f5f5f7;border-radius:8px;"
                               f"padding:5px 10px;color:#57575f;'><b style='color:#33333c;'>{_k}</b> {_v}</span>")
                    _dh += "</div>"
                    st.markdown(_dh, unsafe_allow_html=True)
                if sel.get("특징메모"):
                    st.markdown(f"<div style='margin-top:8px;padding:10px 14px;background:#fafafa;"
                               f"border-left:3px solid #ececef;border-radius:4px;font-size:13px;"
                               f"color:#57575f;white-space:pre-wrap;'>{sel['특징메모']}</div>",
                               unsafe_allow_html=True)

                st.markdown("<div style='height:8px;'></div>", unsafe_allow_html=True)

                # 임장 기록 편집
                with st.form(key="detail_" + sel["page_id"]):
                    ec1, ec2, ec3 = st.columns(3)
                    with ec1:
                        cur = sel.get("상태") or "검토중"
                        new_status = st.selectbox("상태", STATUS_OPTIONS,
                                                  index=STATUS_OPTIONS.index(cur) if cur in STATUS_OPTIONS else 0)
                    with ec2:
                        new_rating = st.slider("평점", 0.0, 5.0, float(sel.get("평점") or 0.0), 0.5)
                    with ec3:
                        new_fav = st.checkbox("⭐ 관심", value=bool(sel.get("관심")))
                    try:
                        _cur_visit = datetime.fromisoformat(sel["방문일"]).date() if sel.get("방문일") else None
                    except Exception:
                        _cur_visit = None
                    new_visit_date = st.date_input("방문일", value=_cur_visit, format="YYYY-MM-DD")
                    new_checks = st.multiselect("현장 체크", CHECK_ITEMS, default=sel.get("임장체크") or [])
                    new_memo = st.text_area("메모", value=sel.get("메모") or "",
                                            placeholder="채광·소음·주차·관리상태·주변 환경 등 현장 인상")
                    if st.form_submit_button("💾 변경사항 저장", type="primary"):
                        props = {
                            "상태": {"select": {"name": new_status}},
                            "평점": {"number": float(new_rating)},
                            "관심": {"checkbox": bool(new_fav)},
                            "방문일": {"date": {"start": new_visit_date.isoformat()} if new_visit_date else None},
                            "임장체크": {"multi_select": [{"name": c} for c in new_checks]},
                            "메모": {"rich_text": [{"text": {"content": new_memo}}]},
                        }
                        props = filter_props(props, schema)
                        if props:
                            try:
                                update_notion_page(notion_token, sel["page_id"], props)
                                st.success("✅ 저장 완료!")
                                st.cache_data.clear(); st.rerun()
                            except Exception as e:
                                st.error(f"저장 실패: {friendly_error(e)}")
                        else:
                            st.info("저장 가능한 속성이 없어요.")

                # 기본 정보 수정 (매물명·주소·호가·거래·면적)
                with st.expander("✏️ 기본 정보 수정", expanded=False):
                    with st.form(key="edit_" + sel["page_id"]):
                        e_name = st.text_input("매물명", value=sel.get("매물명", ""))
                        e_addr = st.text_input("주소", value=sel.get("주소", ""))
                        ig1, ig2, ig3 = st.columns(3)
                        with ig1:
                            _deal_opts = ["매매", "전세", "월세"]
                            _cur_deal = sel.get("거래방식") or "매매"
                            e_deal = st.selectbox("거래방식", _deal_opts,
                                                  index=_deal_opts.index(_cur_deal) if _cur_deal in _deal_opts else 0)
                        with ig2:
                            e_hoga = st.number_input("호가(만원)", min_value=0,
                                                     value=int(sel.get("호가") or 0), step=100)
                        with ig3:
                            e_area = st.number_input("전용면적(평)", min_value=0.0,
                                                     value=float(sel.get("전용면적(평)") or 0.0), step=0.1)
                        e_rent = st.number_input("월세(만원, 월세 거래만)", min_value=0,
                                                 value=int(sel.get("월세") or 0), step=5)
                        if st.form_submit_button("💾 기본 정보 저장", type="primary"):
                            props = {}
                            if e_name.strip():
                                props["매물명"] = {"title": [{"text": {"content": e_name.strip()}}]}
                            if e_addr.strip():
                                props["주소"] = {"rich_text": [{"text": {"content": e_addr.strip()}}]}
                            props["거래방식"] = {"select": {"name": e_deal}}
                            props["호가(만원)"] = {"number": int(e_hoga) if e_hoga > 0 else None}
                            props["전용면적(평)"] = {"number": float(e_area) if e_area > 0 else None}
                            if e_deal == "월세":
                                props["월세"] = {"number": int(e_rent) if e_rent > 0 else None}
                            props = filter_props(props, schema)
                            if props:
                                try:
                                    update_notion_page(notion_token, sel["page_id"], props)
                                    st.success("✅ 기본 정보 저장 완료!")
                                    st.cache_data.clear(); st.rerun()
                                except Exception as e:
                                    st.error(f"저장 실패: {friendly_error(e)}")

                # 매물 상세 정보 수정 (방향·관리비·입주가능일·주차대수·방수욕실수·특징메모)
                with st.expander("🏷️ 매물 상세 정보 수정", expanded=False):
                    _cur_move_in = None
                    if sel.get("입주가능일"):
                        try:
                            _cur_move_in = datetime.fromisoformat(sel["입주가능일"]).date()
                        except Exception:
                            _cur_move_in = None
                    with st.form(key="details_" + sel["page_id"]):
                        de1, de2, de3 = st.columns(3)
                        with de1:
                            d_dir = st.text_input("방향", value=sel.get("방향") or "", placeholder="예) 북서향")
                        with de2:
                            d_mgmt = st.number_input("관리비 (만원)", min_value=0,
                                                     value=int(sel.get("관리비(만원)") or 0), step=1)
                        with de3:
                            d_rb = st.text_input("방수/욕실수", value=sel.get("방수욕실수") or "", placeholder="예) 3/2")
                        de4, de5, de6 = st.columns(3)
                        with de4:
                            d_move = st.date_input("입주가능일", value=_cur_move_in, format="YYYY-MM-DD")
                        with de5:
                            d_park_total = st.number_input("총주차대수", min_value=0,
                                                           value=int(sel.get("총주차대수") or 0), step=1)
                        with de6:
                            d_park_unit = st.number_input("세대당주차대수", min_value=0,
                                                          value=int(sel.get("세대당주차대수") or 0), step=1)
                        d_memo = st.text_area("특징메모", value=sel.get("특징메모") or "",
                                              placeholder="매물특징·매물설명 등 자유롭게", height=80)
                        if st.form_submit_button("💾 상세 정보 저장", type="primary"):
                            dprops = {
                                "방향": {"rich_text": [{"text": {"content": d_dir.strip()}}]},
                                "관리비(만원)": {"number": int(d_mgmt) if d_mgmt > 0 else None},
                                "입주가능일": {"date": {"start": d_move.isoformat()} if d_move else None},
                                "총주차대수": {"number": int(d_park_total) if d_park_total > 0 else None},
                                "세대당주차대수": {"number": int(d_park_unit) if d_park_unit > 0 else None},
                                "방수욕실수": {"rich_text": [{"text": {"content": d_rb.strip()}}]},
                                "특징메모": {"rich_text": [{"text": {"content": d_memo.strip()}}]},
                            }
                            dprops = filter_props(dprops, schema)
                            if dprops:
                                try:
                                    update_notion_page(notion_token, sel["page_id"], dprops)
                                    st.success("✅ 상세 정보 저장 완료!")
                                    st.cache_data.clear(); st.rerun()
                                except Exception as e:
                                    st.error(f"저장 실패: {friendly_error(e)}")
                            else:
                                st.info("저장 가능한 속성이 없어요. 노션 DB에 방향·관리비(만원)·입주가능일·"
                                       "총주차대수·세대당주차대수·방수욕실수·특징메모 컬럼을 먼저 추가해주세요.")

                # 액션: 재조회 · 삭제
                st.markdown("<div style='height:4px;'></div>", unsafe_allow_html=True)
                act1, act2, _act3 = st.columns([1, 1, 2])
                with act1:
                    if st.button("🔄 재조회", use_container_width=True, key="relk_" + sel["page_id"]):
                        with st.spinner("재조회 중..."):
                            try:
                                n, msg = relookup_and_update(notion_token, db_id, schema, sel, juso_key, bldg_key)
                            except Exception as e:
                                n, msg = 0, f"재조회 실패: {friendly_error(e)}"
                        if n:
                            st.success(f"✅ 갱신 — {msg}")
                            st.cache_data.clear(); st.rerun()
                        else:
                            st.warning(f"⚠️ {msg}")
                with act2:
                    if st.button("🗑️ 삭제", use_container_width=True, key="delbtn_" + sel["page_id"]):
                        st.session_state["confirm_del"] = sel["page_id"]
                if st.session_state.get("confirm_del") == sel["page_id"]:
                    st.warning("정말 삭제할까요? 되돌릴 수 없습니다.")
                    dcol1, dcol2, _dcol3 = st.columns([1, 1, 2])
                    with dcol1:
                        if st.button("삭제 확인", type="primary", key="delok"):
                            try:
                                SESSION.patch(f"https://api.notion.com/v1/pages/{sel['page_id']}",
                                    headers={"Authorization": f"Bearer {notion_token}",
                                             "Notion-Version": "2022-06-28", "Content-Type": "application/json"},
                                    json={"archived": True}, timeout=25)
                                st.session_state.pop("confirm_del", None)
                                st.session_state.pop("list_sel", None)
                                st.success("✅ 삭제 완료!")
                                st.cache_data.clear(); st.rerun()
                            except Exception as e:
                                st.error(f"삭제 실패: {friendly_error(e)}")
                    with dcol2:
                        if st.button("취소", key="delcancel"):
                            st.session_state.pop("confirm_del", None); st.rerun()

        # ── 하단: 일괄 도구 (CSV · 전체 재조회) ──
        st.divider()
        with st.expander("🛠️ 일괄 도구 — CSV 내보내기 · 전체 재조회", expanded=False):
            exp_df = pd.DataFrame([{
                "매물명": r.get("매물명", ""), "주소": r.get("주소", ""),
                "거래방식": r.get("거래방식", ""), "호가": r.get("호가"), "월세": r.get("월세"),
                "시세갭": (f"{r['_gap']:+.1f}%" if r["_gap"] is not None else ""),
                "평당가(원)": r.get("평당가(원)"), "준공": r.get("준공년도"),
                "상태": r.get("상태"), "평점": r.get("평점"),
            } for r in rows])
            st.download_button("⬇️ CSV 내보내기",
                exp_df.to_csv(index=False).encode("utf-8-sig"),
                file_name="propertybot.csv", mime="text/csv")
            st.caption("건당 API 호출이 많아 시간이 걸려요. 사람이 입력한 값(호가·전용면적)은 유지되고, "
                       "건축물대장·실거래 시세만 새로 받아옵니다.")
            if st.button(f"🔄🔄 전체 재조회 ({len(rows)}건)"):
                prog = st.progress(0.0, text="재조회 시작...")
                ok, fail, fail_msgs = 0, 0, []
                for _idx, target in enumerate(rows):
                    _label = target.get("매물명") or "(이름없음)"
                    prog.progress(_idx / len(rows), text=f"{_label} 재조회 중... ({_idx + 1}/{len(rows)})")
                    try:
                        n, msg = relookup_and_update(notion_token, db_id, schema, target, juso_key, bldg_key)
                        if n:
                            ok += 1
                        else:
                            fail += 1; fail_msgs.append(f"{_label}: {msg}")
                    except Exception as e:
                        fail += 1; fail_msgs.append(f"{_label}: {friendly_error(e)}")
                prog.progress(1.0, text="완료")
                st.success(f"✅ 전체 재조회 완료 — 성공 {ok}건 / 실패·정보없음 {fail}건")
                if fail_msgs:
                    for m in fail_msgs:
                        st.write(m)
                st.cache_data.clear(); st.rerun()


# ════════════════ 탭 3: 임장 지도 ════════════════
elif active_tab == TAB_LABELS[3]:
    st.subheader("임장 지도")
    st.caption("좌표는 노션에 캐싱돼 다음부터 즉시 로딩됩니다. 🖱️ 클릭→선 그리기(거리 측정), 우클릭→종료")

    cm1, cm2 = st.columns([1, 4])
    with cm1:
        if st.button("🔄 지도 새로고침", use_container_width=True):
            st.cache_data.clear()
            st.rerun()

    @st.cache_data(ttl=300)
    def get_map_data(_juso_key, _notion_token, _db_id, _kakao_key, _schema):
        rows = load_notion_list(_notion_token, _db_id)
        result, failed = [], []
        for row in rows:
            if not row.get("주소"):
                continue
            lat, lng = row.get("위도"), row.get("경도")
            if lat and lng:
                result.append({**row, "lat": float(lat), "lng": float(lng)})
                continue
            juso, err = search_address(row["주소"], _juso_key)
            road_addr = juso.get("roadAddr") if juso else row["주소"]
            glat, glng, gerr = geocode_address(road_addr or row["주소"], _kakao_key)
            if glat and glng:
                result.append({**row, "lat": glat, "lng": glng})
                if _schema and "위도" in _schema and "경도" in _schema and row.get("page_id"):
                    try:
                        update_notion_page(_notion_token, row["page_id"],
                            {"위도": {"number": glat}, "경도": {"number": glng}})
                    except Exception:
                        pass
            else:
                failed.append(f"⚠️ {row['매물명']}: 좌표 없음 ({gerr})")
        return result, failed

    with st.spinner("매물 위치 조회 중..."):
        try:
            map_rows, map_failed = get_map_data(juso_key, notion_token, db_id, kakao_key, schema)
        except Exception as e:
            st.error(f"지도 데이터 로드 실패: {friendly_error(e)}")
            map_rows, map_failed = [], []

    if map_failed:
        with st.expander(f"⚠️ 좌표 조회 실패 {len(map_failed)}건 (클릭해서 확인)"):
            for msg in map_failed:
                st.write(msg)

    if not map_rows:
        st.info("지도에 표시할 매물이 없어요. 주소가 입력된 매물을 먼저 저장해주세요.")
    else:
        import json as _json

        markers_json = _json.dumps([
            {
                "name": r["매물명"], "addr": r.get("주소", ""),
                "deal": r.get("거래방식", ""), "mtype": r.get("매물유형", ""),
                "price": r.get("호가"), "avgPrice": r.get("평당가(원)"),
                "year": r.get("준공년도"), "floors": r.get("최고층수"),
                "gap": compute_gap(r),
                "lat": r["lat"], "lng": r["lng"],
            }
            for r in map_rows
        ], ensure_ascii=False)

        center_lat = map_rows[0]["lat"]
        center_lng = map_rows[0]["lng"]

        _TILE = "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
        _MAP_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<link rel="stylesheet" href="https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.css"/>
<link rel="stylesheet" href="https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.Default.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="https://unpkg.com/leaflet.markercluster@1.5.3/dist/leaflet.markercluster.js"></script>
<style>
html,body{margin:0;padding:0;width:100%;height:100%;font-family:'Pretendard','Malgun Gothic',sans-serif}
#ct{display:flex;width:100%;height:100%}
@media(max-width:768px){#ct{flex-direction:column;height:auto}#lp{width:100%!important;min-width:100%!important;height:180px;min-height:180px;border-right:none!important;border-bottom:1px solid #ececef}#mw{height:450px;min-height:450px}#map{height:450px!important;min-height:450px}}
#lp{width:262px;min-width:262px;height:100%;overflow-y:auto;background:#fafafa;border-right:1px solid #ececef;display:flex;flex-direction:column}
#lh{padding:11px 14px;font-size:14px;font-weight:800;border-bottom:1px solid #ececef;background:#fff;display:flex;align-items:center;justify-content:space-between}
#lf{padding:8px 14px;border-bottom:1px solid #efeff1;background:#fff}
#lf select{width:100%;padding:6px 8px;font-size:12px;border:1px solid #e2e2e7;border-radius:8px}
#li{flex:1;overflow-y:auto}
.it{padding:11px 14px;border-bottom:1px solid #efeff1;cursor:pointer;transition:background .15s}
.it:hover{background:#eef1fd}
.it.ac{background:#e3e9fb;border-left:3px solid #3b5bdb}
.it .nm{font-size:13px;font-weight:700;margin-bottom:3px}
.it .ad{font-size:11px;color:#a4a4ac;margin:0 0 3px 14px}
.it .inf{font-size:11px;color:#8a8a93;margin-left:14px;display:flex;gap:8px;align-items:center}
.dt{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:6px;vertical-align:middle}
#mw{flex:1;position:relative}
#map{width:100%;height:100%}
.cp{position:absolute;top:12px;right:12px;z-index:1000;background:#fff;border-radius:9px;box-shadow:0 2px 8px rgba(0,0,0,.18);padding:9px;display:flex;flex-direction:column;gap:5px;font-size:12px}
.cb{cursor:pointer;border:1px solid #e2e2e7;background:#fff;border-radius:7px;padding:5px 9px;font-size:12px;text-align:center}
.cb:hover{background:#f3f3f5}
.cb.av{background:#3b5bdb;color:#fff;border-color:#3b5bdb}
.lg{position:absolute;bottom:12px;left:12px;z-index:1000;background:rgba(255,255,255,.94);border-radius:8px;padding:8px 13px;box-shadow:0 1px 4px rgba(0,0,0,.14);font-size:12px;display:flex;gap:14px}
.li{display:flex;align-items:center;gap:5px}
.ld{width:10px;height:10px;border-radius:50%}
.dl{background:#fff;border:1px solid #db4040;color:#db4040;font-size:12px;font-weight:700;padding:3px 8px;border-radius:6px;box-shadow:0 1px 3px rgba(0,0,0,.2);white-space:nowrap}
</style></head><body>
<div id="ct"><div id="lp"><div id="lh"><span>📋 매물 목록</span><span id="lc" style="font-size:11px;color:#a4a4ac;font-weight:600"></span></div><div id="lf"><select id="df" onchange="fL()"><option value="전체">전체</option><option value="매매">매매</option><option value="전세">전세</option><option value="월세">월세</option></select></div><div id="li"></div></div>
<div id="mw"><div class="cp"><button class="cb" id="bD" onclick="tD()">📏 거리 측정</button><button class="cb" id="bC" onclick="tC()">📍 클러스터</button><button class="cb" id="bG" onclick="tG()">🏷️ 시세갭 라벨</button></div>
<div class="lg"><div class="li"><div class="ld" style="background:#e5484d"></div>매매</div><div class="li"><div class="ld" style="background:#2f6feb"></div>전세</div><div class="li"><div class="ld" style="background:#2f9e63"></div>월세</div></div>
<div id="map"></div></div></div>
<script>
var D=__DATA__,C={'매매':'#e5484d','전세':'#2f6feb','월세':'#2f9e63'};
var map=L.map('map',{zoomControl:false}).setView([__LAT__,__LNG__],15);
L.control.zoom({position:'bottomright'}).addTo(map);
L.tileLayer('__TILE__',{attribution:'© OpenStreetMap',maxZoom:19}).addTo(map);
function mI(c){var s='<svg xmlns="http://www.w3.org/2000/svg" width="28" height="40" viewBox="0 0 28 40"><path d="M14 0C6.3 0 0 6.3 0 14c0 10.5 14 26 14 26s14-15.5 14-26C28 6.3 21.7 0 14 0z" fill="'+c+'"/><circle cx="14" cy="14" r="6" fill="white"/></svg>';return L.divIcon({html:s,className:'',iconSize:[28,40],iconAnchor:[14,40],popupAnchor:[0,-40]});}
function pH(d){var h=d.price?d.price.toLocaleString()+'만원':'-';var a=d.avgPrice?d.avgPrice.toLocaleString()+'원/평':'-';var b=(d.year&&d.floors)?'🏗️ '+Math.floor(d.year)+'년/'+Math.floor(d.floors)+'층<br>':'';var g=(d.gap!=null)?'<span style="color:'+(d.gap>0?'#1f9d57':'#e5484d')+';font-weight:bold">📊 '+(d.gap>0?'+':'')+d.gap.toFixed(1)+'%</span>':'';return '<div style="font-family:Pretendard,sans-serif;font-size:13px;min-width:180px;line-height:1.6"><b style="font-size:15px">'+d.name+'</b><hr style="margin:4px 0;border:none;border-top:1px solid #eee">📍 '+d.addr+'<br>🏷️ '+(d.deal||'-')+' · '+(d.mtype||'-')+'<br>💰 '+h+'<br>📊 '+a+'<br>'+b+g+'</div>';}
var ms=[],cg=L.markerClusterGroup({maxClusterRadius:50});
D.forEach(function(d,i){var m=L.marker([d.lat,d.lng],{icon:mI(C[d.deal]||'#999')});m.bindPopup(pH(d),{maxWidth:280});m.on('click',function(){hL(i)});m.addTo(map);cg.addLayer(m);ms.push(m)});
function rL(f){var c=document.getElementById('li');c.innerHTML='';var n=0;D.forEach(function(d,i){if(f&&f!=='전체'&&d.deal!==f)return;n++;var v=document.createElement('div');v.className='it';v.setAttribute('data-idx',i);v.innerHTML='<div class="nm"><span class="dt" style="background:'+(C[d.deal]||'#999')+'"></span>'+d.name+'</div><div class="ad">'+d.addr+'</div><div class="inf"><span>'+(d.deal||'-')+'</span><span>💰 '+(d.price?d.price.toLocaleString()+'만':'-')+'</span>'+(d.gap!=null?'<span style="color:'+(d.gap>0?'#1f9d57':'#e5484d')+';font-weight:bold">'+(d.gap>0?'+':'')+d.gap.toFixed(1)+'%</span>':'')+'</div>';v.onclick=function(){fM(i)};c.appendChild(v)});document.getElementById('lc').textContent=n+'개'}
function fM(i){map.setView([D[i].lat,D[i].lng],17);ms[i].openPopup();hL(i)}
function hL(i){document.querySelectorAll('.it').forEach(function(e){e.classList.remove('ac')});var t=document.querySelector('.it[data-idx="'+i+'"]');if(t){t.classList.add('ac');t.scrollIntoView({behavior:'smooth',block:'nearest'})}}
window.fL=function(){rL(document.getElementById('df').value)};rL('전체');
var co=false;function tC(){co=!co;var b=document.getElementById('bC');if(co){ms.forEach(function(m){map.removeLayer(m)});map.addLayer(cg);b.classList.add('av')}else{map.removeLayer(cg);ms.forEach(function(m){m.addTo(map)});b.classList.remove('av')}}
var go=false,gl=[];function tG(){go=!go;var b=document.getElementById('bG');if(go){D.forEach(function(d,i){if(d.gap==null)return;var c=d.gap>0?'#1f9d57':'#e5484d';gl.push(L.marker([d.lat,d.lng],{icon:L.divIcon({html:'<div style="background:#fff;border:1px solid '+c+';color:'+c+';font-size:11px;font-weight:700;padding:2px 7px;border-radius:99px;box-shadow:0 1px 3px rgba(0,0,0,.25);white-space:nowrap">'+(d.gap>0?'+':'')+d.gap.toFixed(1)+'%</div>',className:'',iconAnchor:[20,48]}),interactive:false}).addTo(map))});b.classList.add('av')}else{gl.forEach(function(l){map.removeLayer(l)});gl=[];b.classList.remove('av')}}
var dm=false,dp=[],dn=null,dd=[],db=[];function tD(){dm=!dm;var b=document.getElementById('bD');if(dm){b.classList.add('av');document.getElementById('map').style.cursor='crosshair'}else{b.classList.remove('av');document.getElementById('map').style.cursor='';if(dn){map.removeLayer(dn);dn=null}dd.forEach(function(m){map.removeLayer(m)});db.forEach(function(l){map.removeLayer(l)});dp=[];dd=[];db=[]}}
map.on('click',function(e){if(!dm)return;dp.push(e.latlng);dd.push(L.circleMarker(e.latlng,{radius:5,color:'#db4040',fillColor:'#db4040',fillOpacity:1}).addTo(map));if(dp.length>1){if(dn)map.removeLayer(dn);dn=L.polyline(dp,{color:'#db4040',weight:3}).addTo(map);var t=0;for(var i=1;i<dp.length;i++)t+=dp[i-1].distanceTo(dp[i]);var d=Math.round(t),w=Math.floor(d/67),k=Math.floor(d/227);db.forEach(function(l){map.removeLayer(l)});db=[];db.push(L.marker(e.latlng,{icon:L.divIcon({html:'<div class="dl">📏 '+d+'m · 🚶 '+(w>60?Math.floor(w/60)+'시간 ':'')+(w%60)+'분 · 🚲 '+(k>60?Math.floor(k/60)+'시간 ':'')+(k%60)+'분</div>',className:'',iconAnchor:[0,-10]}),interactive:false}).addTo(map))}});
map.on('contextmenu',function(){if(dm)tD()});
</script></body></html>"""

        import streamlit.components.v1 as components
        html_str = _MAP_HTML.replace("__DATA__", markers_json).replace("__LAT__", str(center_lat)).replace("__LNG__", str(center_lng)).replace("__TILE__", _TILE)
        components.html(html_str, height=620)
        st.caption(f"총 {len(map_rows)}개 매물 표시됨")


elif active_tab == TAB_LABELS[4]:
    st.subheader("📖 매수 가이드")
    st.caption("나만 보는 개인 재정·매매 절차 메모입니다. (민감한 금액 정보가 포함되어 있어요)")

    if _APP_PASSWORD and not st.session_state.get("_guide_authed"):
        st.markdown("<div style='text-align:center;padding:30px 0 10px;'>"
                    "<div style='font-size:36px;'>🔒</div>"
                    "<div style='font-weight:700;margin-top:6px;'>비밀번호를 입력하면 가이드가 열립니다</div>"
                    "</div>", unsafe_allow_html=True)
        _gc1, _gc2, _gc3 = st.columns([1, 1.2, 1])
        with _gc2:
            _gpw = st.text_input("비밀번호", type="password", label_visibility="collapsed",
                                 placeholder="비밀번호를 입력하세요", key="_guide_pw_input")
            if st.button("열기", use_container_width=True, type="primary"):
                if _gpw == _APP_PASSWORD:
                    st.session_state["_guide_authed"] = True
                    st.rerun()
                else:
                    st.error("비밀번호가 틀렸어요.")
        st.stop()

    _guide_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "buying_guide.md")
    _sections = load_guide_sections(_guide_path)

    if not _APP_PASSWORD:
        st.warning("⚠️ 이 탭엔 소득·증여액 등 민감한 정보가 들어있는데, 지금 앱은 비밀번호 없이 "
                   "누구나 URL로 접속 가능한 상태예요. 사이드바 안내대로 `APP_PASSWORD`를 "
                   "설정해 접근을 제한하는 걸 권장합니다.")

    if not _sections:
        st.error(f"가이드 파일을 찾을 수 없어요. `{os.path.basename(_guide_path)}` 파일을 "
                 f"`app.py`와 같은 폴더에 두세요. (Streamlit Cloud라면 GitHub 레포에도 같이 커밋 필요)")
    else:
        _progress_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "guide_progress.json")
        if "_guide_progress" not in st.session_state:
            st.session_state["_guide_progress"] = load_guide_progress(_progress_path)
        _progress = st.session_state["_guide_progress"]

        _total_items = sum(
            1 for _, b in _sections for line in b.split("\n")
            if _GUIDE_CHECKLIST_RE.match(line.strip()))
        _done_items = sum(1 for k, v in _progress.items() if v)
        if _total_items:
            st.caption(f"✅ 체크리스트 진행률: {min(_done_items, _total_items)}/{_total_items}")

        _finance_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "financial_profile.json")

        _intro_title, _intro_body = _sections[0]
        st.markdown(f"## {_intro_title}")
        render_guide_body(_intro_body, _progress, _progress_path)
        for _title, _body in _sections[1:]:
            if _title == "내 구매력 현황":
                with st.expander(f"📊 {_title} (실시간 — 여기서 수정하면 아래 로드맵·자금 계산기에도 반영)",
                                 expanded=True):
                    render_affordability_editor(_finance_path)
            elif _title == "자금조달계획서 — 내 경우":
                with st.expander(_title, expanded=False):
                    render_funding_plan_editor(_finance_path)
                    with st.expander("원본 안내 텍스트 (증여/차용 신고 방법 등)", expanded=False):
                        render_guide_body(_body, _progress, _progress_path)
            elif _title == "현실적 로드맵":
                _fp = load_finance_profile(_finance_path)
                _res = calc_affordability(_fp["annual_income"], _fp["existing_monthly_payment"],
                                          _fp["cash_actual"] + _fp["parent_support"],
                                          _fp["dsr_limit_pct"], _fp["ltv_limit_pct"],
                                          _fp["rate_pct"], _fp["stress_pct"], _fp["years"])
                with st.expander(_title, expanded=False):
                    if _fp.get("target_price"):
                        _gap = _fp["target_price"] - _res["max_price"]
                        if _gap > 0:
                            st.info(f"📌 현재 재정 기준, 목표 매매가까지 자기자금 약 {fmt_eok(_gap)} "
                                   f"더 필요한 상태예요. ('내 구매력 현황'에서 최신 수정)")
                        else:
                            st.success("📌 현재 재정 기준, 목표 매매가 이내로 구매 가능한 상태예요.")
                    else:
                        st.caption("💡 '내 구매력 현황'에서 목표 매매가를 입력하면 여기 갭이 표시돼요.")
                    render_guide_body(_body, _progress, _progress_path)
            else:
                with st.expander(_title, expanded=False):
                    render_guide_body(_body, _progress, _progress_path)

        st.caption("💾 체크 상태는 이 서버의 `guide_progress.json` 파일에 저장돼요. "
                  "로컬(Windows)에서는 계속 유지되지만, Streamlit Cloud 무료 플랜은 앱이 "
                  "재시작(reboot)되면 초기화될 수 있어요.")

# ════════════════ 탭 6: 자금 계산기 ════════════════
elif active_tab == TAB_LABELS[5]:
    st.subheader("💰 자금 계산기")
    st.caption("DSR·LTV 기준 대출한도·매수가능액을 간이 계산합니다. "
              "실제 은행 심사 결과와 다를 수 있으니 참고용으로만 써주세요 — 저는 금융 전문가가 아니에요.")

    with st.expander("ℹ️ 계산 방식 및 가정 (2026년 기준)", expanded=False):
        st.markdown(
            "- **DSR 한도** = 연소득 × DSR% − 기존 대출 연간 상환액 (1금융권 상한 **40%**가 기본값)\n"
            "- 그 한도를 **스트레스금리(입력금리+가산금리) 기준 원리금균등상환**으로 역산해 DSR상 대출한도를 구합니다\n"
            "- 기본 스트레스 가산금리 **1.5%p**는 2025.7 시행된 스트레스 DSR 3단계, 서울·경기·인천(수도권) "
            "변동금리 주담대 기준 (금융위원회 발표)\n"
            "- **LTV 한도** = 매수가 × LTV%. 기본값 **40%**는 서울 등 투기과열지구 **일반 무주택자** 기준이고, "
            "**생애최초 구매자는 최대 80%**까지 완화돼요 — 본인이 생애최초에 해당하면 슬라이더를 올려서 재계산하세요\n"
            "- DSR 한도와 LTV 한도 중 **더 낮은 쪽이 실제 한도**가 됩니다 (병목 구간 표시)\n"
            "- 월 상환액은 스트레스금리가 아닌 **실제 입력 금리** 기준으로 계산\n"
            "- 부대비용은 취득세(간이 구간별) + 중개보수(0.5% 가정)만 반영한 대략치입니다\n"
            "- ⚠️ 규제지역 지정 여부·본인의 생애최초/신혼/다자녀 특례 해당 여부는 수시로 바뀌고 개별 심사에 따라 "
            "달라져요 — 이 계산기는 참고용이고, 정확한 값은 반드시 **은행 사전심사**로 확인하세요"
        )

    _fin_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "financial_profile.json")
    _fp = load_finance_profile(_fin_path)
    st.caption("💡 연소득·DSR·LTV·금리 등은 '매수 가이드 → 내 구매력 현황'과 같은 값을 공유해요. "
              "(자기자금/부모님 지원금 분리 입력은 그쪽에서 별도로 관리돼요)")

    c1, c2 = st.columns(2)
    with c1:
        in_income = st.number_input("연소득 (만원)", min_value=0, value=int(_fp["annual_income"]), step=10)
        in_existing_pay = st.number_input("기존 대출 월 상환액 (만원)", min_value=0,
                                          value=int(_fp["existing_monthly_payment"]), step=1,
                                          help="청년도약대출 등 기존 대출이 있으면 월 상환액을 입력")
        in_cash = st.number_input("자기자금 (만원, 현금성 자산)", min_value=0,
                                  value=int(_fp["cash_actual"] + _fp["parent_support"]), step=100,
                                  help="부모님 지원금 포함 여부는 '내 구매력 현황'에서 세부 조정 가능")
        in_years = st.number_input("대출기간 (년)", min_value=1, max_value=40, value=int(_fp["years"]), step=1)
    with c2:
        in_dsr = st.slider("DSR 한도 (%)", min_value=10, max_value=70, value=int(_fp["dsr_limit_pct"]), step=1)
        in_ltv = st.slider("LTV 한도 (%)", min_value=10, max_value=90, value=int(_fp["ltv_limit_pct"]), step=1,
                           help="서울 등 투기과열지구 일반 무주택자는 40%, 생애최초 구매자는 "
                                "최대 80%까지 완화돼요 (2026년 기준). 본인 조건에 맞게 조정하세요.")
        in_rate = st.number_input("대출금리 (%, 실제)", min_value=0.0, value=float(_fp["rate_pct"]), step=0.1)
        in_stress = st.number_input("스트레스금리 가산 (%p)", min_value=0.0, value=float(_fp["stress_pct"]), step=0.1,
                                    help="스트레스 DSR 단계별로 다름 (2026년 기준 3단계 적용 중)")

    if st.button("🧮 계산하기", type="primary", use_container_width=True):
        result = calc_affordability(in_income, in_existing_pay, in_cash,
                                    in_dsr, in_ltv, in_rate, in_stress, in_years)
        st.session_state["affordability_result"] = result
        # 자기자금/부모님지원금은 '내 구매력 현황'에서 별도로 나눠 관리하므로 여기서는 덮어쓰지 않음
        # (그 외 공통 가정값만 프로필에 반영)
        _fp.update({
            "annual_income": in_income, "existing_monthly_payment": in_existing_pay,
            "dsr_limit_pct": in_dsr, "ltv_limit_pct": in_ltv,
            "rate_pct": in_rate, "stress_pct": in_stress, "years": in_years,
        })
        save_finance_profile(_fin_path, _fp)

    result = st.session_state.get("affordability_result")
    if result:
        st.markdown("<div style='height:6px;'></div>", unsafe_allow_html=True)
        rc1, rc2 = st.columns(2)
        rc1.metric("최대 대출 가능액", fmt_eok(result["max_loan"]))
        rc2.metric("살 수 있는 집 (현금 포함)", fmt_eok(result["max_price"]))
        rc3, rc4 = st.columns(2)
        rc3.metric("월 상환액 (실제금리 기준)", f"{result['monthly_payment_real']:,.0f}만원")
        rc4.metric("취득세·중개보수 등 부대비용", f"{result['extra_cost']:,.0f}만원")
        st.caption(f"**{result['bottleneck']}**이 한도를 결정했어요 "
                  f"({'DSR(소득) 기준' if result['bottleneck']=='DSR' else 'LTV(담보비율) 기준'}이 "
                  f"더 낮아서 그쪽으로 제한됨)")

        st.divider()
        st.markdown("##### 📈 자금 축적 시뮬레이터")
        st.caption("지금 속도로 저축하면 목표 매매가에 언제쯤 도달하는지 계산해요.")
        _fp2 = load_finance_profile(_fin_path)
        _sim_saving = st.number_input("월 저축액 (만원)", min_value=0,
                                      value=int(_fp2.get("monthly_saving", 0)), step=5,
                                      key="sim_saving_input")
        if _sim_saving != _fp2.get("monthly_saving", 0):
            _fp2["monthly_saving"] = _sim_saving
            save_finance_profile(_fin_path, _fp2)

        _target_for_sim = _fp2.get("target_price", 0)
        if not _target_for_sim:
            st.caption("💡 '매수 가이드 → 내 구매력 현황'에서 목표 매매가를 입력하면 도달 시점을 계산해줘요.")
        elif _sim_saving <= 0:
            st.caption("월 저축액을 입력하면 도달 시점을 계산해줘요.")
        else:
            _gap_now = _target_for_sim - result["max_price"]
            if _gap_now <= 0:
                st.success(f"✅ 이미 목표 매매가 {fmt_eok(_target_for_sim)}를 감당할 수 있는 수준이에요.")
            else:
                # 자기자금이 월 _sim_saving만큼 늘면 max_price도 대략 그만큼(레버리지 없이) 늘어난다고 가정한 단순 근사
                _months_needed = -(-int(_gap_now) // int(_sim_saving))  # 올림 나눗셈
                _reach_date = date.today() + pd.Timedelta(days=_months_needed * 30)
                st.info(f"현재 갭 {fmt_eok(_gap_now)} ÷ 월 {_sim_saving}만원 저축 "
                       f"≈ **약 {_months_needed}개월 후** (약 {_reach_date.strftime('%Y년 %m월')}경) 도달 예상")
                st.caption("⚠️ 단순 근사치예요 — 대출한도·집값 변동, 이자 수익 등은 반영 안 했습니다.")

        st.divider()
        st.markdown("##### 🏠 이 예산으로 살 수 있는 우리 매물")
        st.caption("매물 목록의 '매매' 거래 매물 중, 호가가 위에서 계산한 예산 이내인 것만 보여줘요.")
        try:
            _rows = load_notion_list(notion_token, db_id)
            _affordable = [r for r in _rows if r.get("거래방식") == "매매" and r.get("호가")
                          and r["호가"] <= result["max_price"]]
            if not _affordable:
                st.info("이 예산 안에 들어오는 매매 매물이 아직 없어요.")
            else:
                _affordable = sorted(_affordable, key=lambda r: r["호가"], reverse=True)
                for r in _affordable:
                    st.markdown(
                        f"<div style='background:#fff;border:1px solid #ececef;border-radius:10px;"
                        f"padding:10px 14px;margin-bottom:6px;display:flex;justify-content:space-between;'>"
                        f"<div><b>{r.get('매물명','')}</b><br>"
                        f"<span style='font-size:12px;color:#9a9aa3;'>{r.get('주소','')}</span></div>"
                        f"<div style='text-align:right;font-weight:800;'>{fmt_eok(r['호가'])}"
                        f"<div style='font-size:11px;color:#9a9aa3;font-weight:400;'>"
                        f"여유 {fmt_eok(result['max_price'] - r['호가'])}</div></div></div>",
                        unsafe_allow_html=True)
        except Exception as e:
            st.error(f"매물 목록 조회 실패: {friendly_error(e)}")
