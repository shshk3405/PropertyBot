"""
PropertyBot v4.0 - 대시보드 · 입력 · 목록 · 지도 · 임장 체크리스트
"""
import os, re, requests, xml.etree.ElementTree as ET
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

# ── 디자인 상수 ───────────────────────────────────────────
ACCENT = "#3b5bdb"
DEAL_COLORS = {"매매": "#e5484d", "전세": "#2f6feb", "월세": "#2f9e63"}
GAP_GOOD, GAP_BAD = "#1f9d57", "#e5484d"
STATUS_COLORS = {"검토중": "#8a8a93", "방문예정": ACCENT, "방문완료": "#1f9d57", "보류": "#c98a00", "제외": "#b0b0b8"}
STATUS_OPTIONS = ["검토중", "방문예정", "방문완료", "보류", "제외"]
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
    r = requests.get("https://business.juso.go.kr/addrlink/addrLinkApi.do",
        params={"confmKey": juso_key, "currentPage": 1, "countPerPage": 10,
                "keyword": address, "resultType": "json", "addInfoYn": "Y"}, timeout=25)
    data = r.json()
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


def get_building_info(j, bldg_key):
    r = requests.get("https://apis.data.go.kr/1613000/BldRgstHubService/getBrTitleInfo",
        params={"serviceKey": bldg_key, "sigunguCd": j["sigunguCd"],
                "bjdongCd": j["bjdongCd"], "platGbCd": j["platGbCd"],
                "bun": j["bun"], "ji": j["ji"],
                "_type": "json", "numOfRows": "10", "pageNo": "1"}, timeout=30)
    items = r.json().get("response", {}).get("body", {}).get("items", {})
    if not items:
        return None, "건축물대장 조회 결과 없음"
    item = items.get("item")
    if isinstance(item, list):
        item = item[0] if item else None
    return item, None


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
        r = requests.get(
            "https://dapi.kakao.com/v2/local/search/address.json",
            params={"query": road_addr},
            headers={"Authorization": f"KakaoAK {kakao_key}"},
            timeout=25
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
        return None, None, str(e)


def get_market_price(j, mtype, deal_type, bldg_key, months=6):
    endpoint = RTMS_ENDPOINTS.get((mtype, deal_type))
    if not endpoint or len(j["sigunguCd"]) != 5: return None
    url = f"https://apis.data.go.kr/1613000/{endpoint}"
    all_deals = []
    today = datetime.now()
    for i in range(months):
        yr, mo = today.year, today.month - i
        while mo <= 0: mo += 12; yr -= 1
        try:
            r = requests.get(url, params={"serviceKey": bldg_key, "LAWD_CD": j["sigunguCd"],
                "DEAL_YMD": f"{yr}{mo:02d}", "pageNo": "1", "numOfRows": "1000"}, timeout=30)
            root = ET.fromstring(r.content)
            rc = root.find(".//resultCode")
            if rc is None or rc.text not in ("00", "000"): continue
            for item in root.findall(".//item"):
                all_deals.append({c.tag: (c.text or "").strip() for c in item})
        except Exception: continue
    if not all_deals: return None

    same_road, same_dong = [], []
    for d in all_deals:
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

    pool = same_road or same_dong
    if not pool: return None
    basis = "도로명" if same_road else "법정동"
    avg = int(sum(pool) / len(pool))
    return {"avg": avg, "count": len(pool),
            "basis": f"{deal_type} · 같은 {basis} {len(pool)}건 (최근 {months}개월)"}


# ── 노션 스키마 인식 / 저장 / 수정 ────────────────────────

@st.cache_data(ttl=600)
def get_db_schema(notion_token, db_id):
    """노션 DB에 존재하는 속성명→타입 맵 (없는 컬럼 저장 시도 방지용)"""
    try:
        r = requests.get(f"https://api.notion.com/v1/databases/{db_id}",
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
    requests.patch(
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

    bldg, berr = get_building_info(juso, bldg_key)
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
        props["위반건축물"] = {"checkbox": bldg.get("vltnBldYn", "N") == "Y"}

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
    return len(props), " · ".join(parts) or "갱신 완료"


def save_to_notion(notion, db_id, schema, name, address, deal_type, price, area, juso, bldg, market):
    """노션 DB에 새 페이지 생성"""
    props = {
        "매물명": {"title": [{"text": {"content": name}}]},
        "주소": {"rich_text": [{"text": {"content": address}}]},
    }
    if deal_type:
        props["거래방식"] = {"select": {"name": deal_type}}
    if price:
        props["호가"] = {"number": price}
    if area:
        props["전용면적(평)"] = {"number": float(area)}
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
        props["위반건축물"] = {"checkbox": bldg.get("vltnBldYn", "N") == "Y"}
    if market:
        props["최근 거래 평당가(원)"] = {"number": market["avg"]}
        props["비교 거래 건수"] = {"number": market["count"]}
        props["비교 기준"] = {"rich_text": [{"text": {"content": market["basis"]}}]}
    # 신규 기본값 (스키마에 있을 때만)
    props["상태"] = {"select": {"name": "검토중"}}
    return notion.pages.create(parent={"database_id": db_id},
                               properties=filter_props(props, schema))


def load_notion_list(notion_token, db_id):
    """노션 DB 매물 목록 조회 (직접 HTTP)"""
    resp = requests.post(
        f"https://api.notion.com/v1/databases/{db_id}/query",
        headers={"Authorization": f"Bearer {notion_token}",
                 "Notion-Version": "2022-06-28", "Content-Type": "application/json"},
        json={"sorts": [{"timestamp": "created_time", "direction": "descending"}]},
        timeout=30)
    rows = []
    for p in resp.json().get("results", []):
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
            "거래방식": sel("거래방식"), "호가": num("호가"),
            "매물유형": sel("매물유형"), "준공년도": num("준공년도"),
            "최고층수": num("최고층수"), "평당가(원)": num("최근 거래 평당가(원)"),
            "비교기준": txt("비교 기준"), "전용면적(평)": num("전용면적(평)"),
            "상태": sel("상태"), "관심": chk("관심"), "평점": num("평점"),
            "메모": txt("메모"), "방문일": dt("방문일"), "임장체크": msel("임장체크"),
            "위도": num("위도"), "경도": num("경도"),
            "page_id": p["id"],
        })
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


# ── Streamlit UI ─────────────────────────────────────────

st.set_page_config(page_title="PropertyBot", page_icon="🏠", layout="wide", initial_sidebar_state="collapsed")

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
/* 탭 */
.stTabs [data-baseweb="tab-list"] { gap:2px; border-bottom:1px solid #e8e8ec; }
.stTabs [data-baseweb="tab"] { font-weight:600; font-size:14px; padding:9px 16px; }
.stTabs [aria-selected="true"] { color:__ACCENT__; }
.stTabs [data-baseweb="tab-highlight"] { background:__ACCENT__; }
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
[data-testid="stSidebar"] { background:#fff; border-right:1px solid #ececef; }
[data-testid="stSidebar"] { display:none; }
[data-testid="stSidebar"][aria-expanded="true"] { min-width:0 !important; }
section[data-testid="stSidebar"] > div {{ padding-top:1rem; }}
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
    ("상태", "선택(Select)", "검토중·관심·방문예정·보류·제외"),
    ("관심", "체크박스(Checkbox)", "⭐ 즐겨찾기"),
    ("평점", "숫자(Number)", "0~5점 임장 평가"),
    ("방문일", "날짜(Date)", "임장 방문 날짜"),
    ("메모", "텍스트(Text)", "자유 메모"),
    ("임장체크", "다중 선택(Multi-select)", "체크리스트 항목"),
    ("위도", "숫자(Number)", "지도 좌표 캐시"),
    ("경도", "숫자(Number)", "지도 좌표 캐시"),
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
tab_dash, tab_input, tab_list, tab_map, tab_check = st.tabs(
    ["📊 대시보드", "➕ 새 매물 입력", "📋 매물 목록", "🗺️ 임장 지도", "🗓️ 임장 체크리스트"])

# ════════════════ 탭 0: 대시보드 ════════════════
with tab_dash:
    st.subheader("대시보드")
    try:
        rows = load_notion_list(notion_token, db_id)
    except Exception as e:
        rows = []
        st.error(f"데이터 로드 실패: {e}")

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
with tab_input:
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
            price_in = st.number_input("호가 (만원)", min_value=0, value=0, step=100)
        with cc2:
            area_in = st.number_input("전용면적 (평)", min_value=0.0, value=0.0, step=0.1,
                                      help="입력 시 시세갭(호가 평당 vs 실거래 시세)을 계산합니다.")

    deal_type = None if deal_type_sel == "(선택 안 함)" else deal_type_sel
    price = price_in if price_in > 0 else None
    area = area_in if area_in > 0 else None

    if st.button("🔍 조회", type="primary", disabled=not (name and address), use_container_width=True):
        with st.spinner("도로명주소 조회 중..."):
            juso, err = search_address(address, juso_key)
        if err:
            st.error(f"도로명주소: {err}")
            st.session_state.pop("lookup", None)
        else:
            res = {"name": name, "address": address, "deal_type": deal_type,
                   "price": price, "area": area, "juso": juso, "bldg": None, "market": None}
            with st.spinner("건축물대장 조회 중..."):
                bldg, berr = get_building_info(juso, bldg_key)
            res["bldg"] = bldg
            res["bldg_err"] = berr
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
            m5.metric("위반건축물", "있음 ⚠️" if bldg.get("vltnBldYn") == "Y" else "없음")
        else:
            m4.metric("건폐율 / 용적률", "—")
            m5.metric("위반건축물", "—")
        m6.metric("PNU", juso["pnu"] if juso else "—")
        if res.get("bldg_err"):
            st.caption(f"건축물대장: {res['bldg_err']}")

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
                                              deal_type, price, area, juso, bldg, market)
                        st.success(f"✅ 저장 완료! [페이지 열기]({page.get('url','')})")
                        st.session_state.pop("lookup", None)
                    except Exception as e:
                        st.error(f"노션 저장 실패: {e}")
        with sc2:
            st.caption("결과를 확인한 뒤 저장됩니다. (조회 ↔ 저장 분리)")

# ════════════════ 탭 2: 매물 목록 ════════════════
with tab_list:
    st.subheader("매물 목록")
    cr1, cr2 = st.columns([1, 5])
    with cr1:
        if st.button("🔄 새로고침", use_container_width=True):
            st.cache_data.clear()
            st.rerun()

    try:
        rows = load_notion_list(notion_token, db_id)
    except Exception as e:
        rows = []
        st.error(f"목록 조회 실패: {e}")

    if not rows:
        st.info("저장된 매물이 없어요.")
    else:
        for r in rows:
            r["_gap"] = compute_gap(r)

        cf1, cf2, _ = st.columns([1, 1, 2])
        with cf1:
            deal_types = sorted(set(r.get("거래방식", "") for r in rows if r.get("거래방식")))
            deal_filter = st.selectbox("거래방식", ["전체"] + deal_types)
        with cf2:
            sort_option = st.selectbox("정렬", ["최신순", "시세갭 높은순", "호가 낮은순", "호가 높은순", "평당가 낮은순"])

        filtered = rows if deal_filter == "전체" else [r for r in rows if r.get("거래방식") == deal_filter]
        if sort_option == "시세갭 높은순":
            filtered = sorted(filtered, key=lambda r: r["_gap"] if r["_gap"] is not None else -1e9, reverse=True)
        elif sort_option == "호가 낮은순":
            filtered = sorted(filtered, key=lambda r: r.get("호가") or float("inf"))
        elif sort_option == "호가 높은순":
            filtered = sorted(filtered, key=lambda r: r.get("호가") or 0, reverse=True)
        elif sort_option == "평당가 낮은순":
            filtered = sorted(filtered, key=lambda r: r.get("평당가(원)") or float("inf"))

        if not filtered:
            st.info(f"'{deal_filter}' 매물이 없어요.")
        else:
            page_ids = [r["page_id"] for r in filtered]
            orig = [{"관심": bool(r.get("관심")), "상태": r.get("상태") or "검토중",
                     "평점": r.get("평점")} for r in filtered]
            recs = []
            for r in filtered:
                recs.append({
                    "삭제": False,
                    "관심": bool(r.get("관심")),
                    "상태": r.get("상태") or "검토중",
                    "매물명": r.get("매물명", ""),
                    "거래": r.get("거래방식", ""),
                    "주소": r.get("주소", ""),
                    "호가": r.get("호가"),
                    "시세갭": (f"{r['_gap']:+.1f}%" if r["_gap"] is not None else "—"),
                    "평당가(원)": r.get("평당가(원)"),
                    "준공": r.get("준공년도"),
                    "평점": r.get("평점"),
                })
            df = pd.DataFrame(recs)

            edited = st.data_editor(
                df, use_container_width=True, hide_index=True,
                column_config={
                    "삭제": st.column_config.CheckboxColumn("🗑️", width="small"),
                    "관심": st.column_config.CheckboxColumn("⭐", width="small"),
                    "상태": st.column_config.SelectboxColumn("상태", options=STATUS_OPTIONS, width="small"),
                    "거래": st.column_config.TextColumn("거래", width="small", disabled=True),
                    "매물명": st.column_config.TextColumn("매물명", disabled=True),
                    "주소": st.column_config.TextColumn("주소", disabled=True),
                    "호가": st.column_config.NumberColumn("호가", format="%d만", disabled=True),
                    "시세갭": st.column_config.TextColumn("시세갭", help="양수=저평가", disabled=True),
                    "평당가(원)": st.column_config.NumberColumn("평당가", format="%d원", disabled=True),
                    "준공": st.column_config.NumberColumn("준공", format="%d년", disabled=True),
                    "평점": st.column_config.NumberColumn("평점", min_value=0.0, max_value=5.0, step=0.5),
                })
            st.caption(f"총 {len(rows)}개 중 {len(filtered)}개 표시됨 · 저평가 "
                       f"{sum(1 for r in rows if r['_gap'] is not None and r['_gap'] > 0)}건")

            ca1, ca2, ca3 = st.columns([1.2, 1.2, 3])
            with ca1:
                if st.button("💾 변경사항 저장", use_container_width=True):
                    changed = 0
                    for i, pid in enumerate(page_ids):
                        props = {}
                        if edited["관심"][i] != orig[i]["관심"]:
                            props["관심"] = {"checkbox": bool(edited["관심"][i])}
                        if edited["상태"][i] != orig[i]["상태"]:
                            props["상태"] = {"select": {"name": edited["상태"][i]}}
                        ev = edited["평점"][i]
                        ev = float(ev) if pd.notna(ev) else None
                        if ev != orig[i]["평점"]:
                            props["평점"] = {"number": ev}
                        props = filter_props(props, schema)
                        if props:
                            try:
                                update_notion_page(notion_token, pid, props)
                                changed += 1
                            except Exception:
                                pass
                    if changed: st.success(f"✅ {changed}건 업데이트")
                    else: st.info("변경사항이 없어요.")
                    if changed:
                        st.cache_data.clear(); st.rerun()
            with ca2:
                st.download_button("⬇️ CSV 내보내기",
                    df.drop(columns=["삭제"]).to_csv(index=False).encode("utf-8-sig"),
                    file_name="propertybot.csv", mime="text/csv", use_container_width=True)

            # ── 재조회: 기존 매물의 실거래 시세·건축물대장 다시 받아 노션 갱신 ──
            st.divider()
            st.markdown("##### 🔄 매물 재조회")
            st.caption("기존 매물의 실거래 시세·건축물대장을 다시 불러와 노션을 갱신합니다. "
                       "평당가·준공·매물유형이 채워집니다. (시세갭은 전용면적(평)까지 입력돼야 계산돼요.)")
            rc1, rc2 = st.columns([3, 1])
            with rc1:
                relookup_idx = st.selectbox(
                    "재조회할 매물", range(len(filtered)),
                    format_func=lambda i: (f"{filtered[i].get('매물명') or '(이름없음)'} · "
                                           f"{filtered[i].get('주소') or '주소없음'}"),
                    label_visibility="collapsed")
            with rc2:
                do_relookup = st.button("🔄 재조회", use_container_width=True, type="primary")
            if do_relookup:
                target = filtered[relookup_idx]
                with st.spinner(f"'{target.get('매물명') or ''}' 재조회 중..."):
                    try:
                        n, msg = relookup_and_update(notion_token, db_id, schema,
                                                     target, juso_key, bldg_key)
                    except Exception as e:
                        n, msg = 0, f"재조회 실패: {e}"
                if n:
                    st.success(f"✅ {target.get('매물명') or '매물'} 갱신 완료 — {msg} ({n}개 필드)")
                    st.cache_data.clear(); st.rerun()
                else:
                    st.warning(f"⚠️ {msg}")

            selected_ids = [page_ids[i] for i, v in enumerate(edited["삭제"]) if v]
            if selected_ids:
                st.warning(f"⚠️ {len(selected_ids)}개 매물을 삭제하시겠습니까? 되돌릴 수 없습니다.")
                cd1, cd2, _ = st.columns([1, 1, 3])
                with cd1:
                    confirm_delete = st.button(f"🗑️ {len(selected_ids)}개 삭제 확인", type="primary")
                with cd2:
                    st.button("취소")
                if confirm_delete:
                    deleted, failed = 0, 0
                    for pid in selected_ids:
                        try:
                            requests.patch(f"https://api.notion.com/v1/pages/{pid}",
                                headers={"Authorization": f"Bearer {notion_token}",
                                         "Notion-Version": "2022-06-28", "Content-Type": "application/json"},
                                json={"archived": True}, timeout=25)
                            deleted += 1
                        except Exception:
                            failed += 1
                    if deleted: st.success(f"✅ {deleted}개 삭제 완료!")
                    if failed: st.error(f"❌ {failed}개 삭제 실패")
                    st.cache_data.clear(); st.rerun()

# ════════════════ 탭 3: 임장 지도 ════════════════
with tab_map:
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
            st.error(f"지도 데이터 로드 실패: {e}")
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

        leaflet_map_html = f"""
<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<link rel="stylesheet" href="https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.css"/>
<link rel="stylesheet" href="https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.Default.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="https://unpkg.com/leaflet.markercluster@1.5.3/dist/leaflet.markercluster.js"></script>
<style>
html, body {{ margin:0; padding:0; width:100%; height:100%; font-family:'Pretendard','Malgun Gothic',sans-serif; }}
#container {{ display:flex; width:100%; height:100%; }}
@media (max-width: 768px) {{
    #container {{ flex-direction:column; height:auto; }}
    #list-panel {{ width:100%; min-width:100%; height:180px; min-height:180px; border-right:none; border-bottom:1px solid #ececef; }}
    #map-wrap {{ height:450px; min-height:450px; }}
    #map {{ height:450px; min-height:450px; }}
}}
#list-panel {{
    width:262px; min-width:262px; height:100%; overflow-y:auto;
    background:#fafafa; border-right:1px solid #ececef;
    display:flex; flex-direction:column;
}}
#list-header {{
    padding:11px 14px; font-size:14px; font-weight:800;
    border-bottom:1px solid #ececef; background:#fff;
    display:flex; align-items:center; justify-content:space-between;
}}
#list-filter {{ padding:8px 14px; border-bottom:1px solid #efeff1; background:#fff; }}
#list-filter select {{ width:100%; padding:6px 8px; font-size:12px; border:1px solid #e2e2e7; border-radius:8px; }}
#list-items {{ flex:1; overflow-y:auto; }}
.list-item {{ padding:11px 14px; border-bottom:1px solid #efeff1; cursor:pointer; transition:background .15s; }}
.list-item:hover {{ background:#eef1fd; }}
.list-item.active {{ background:#e3e9fb; border-left:3px solid {ACCENT}; }}
.list-item .li-name {{ font-size:13px; font-weight:700; margin-bottom:3px; }}
.list-item .li-addr {{ font-size:11px; color:#a4a4ac; margin:0 0 3px 14px; }}
.list-item .li-info {{ font-size:11px; color:#8a8a93; margin-left:14px; display:flex; gap:8px; align-items:center; }}
.li-dot {{ width:8px; height:8px; border-radius:50%; display:inline-block; margin-right:6px; vertical-align:middle; }}
#map-wrap {{ flex:1; position:relative; }}
#map {{ width:100%; height:100%; }}
.ctrl-panel {{
    position:absolute; top:12px; right:12px; z-index:1000;
    background:#fff; border-radius:9px; box-shadow:0 2px 8px rgba(0,0,0,.18);
    padding:9px; display:flex; flex-direction:column; gap:5px; font-size:12px;
}}
.ctrl-btn {{ cursor:pointer; border:1px solid #e2e2e7; background:#fff; border-radius:7px; padding:5px 9px; font-size:12px; text-align:center; }}
.ctrl-btn:hover {{ background:#f3f3f5; }}
.ctrl-btn.active {{ background:{ACCENT}; color:#fff; border-color:{ACCENT}; }}
.legend {{
    position:absolute; bottom:12px; left:12px; z-index:1000;
    background:rgba(255,255,255,0.94); border-radius:8px; padding:8px 13px;
    box-shadow:0 1px 4px rgba(0,0,0,.14); font-size:12px; display:flex; gap:14px;
}}
.legend-item {{ display:flex; align-items:center; gap:5px; }}
.legend-dot {{ width:10px; height:10px; border-radius:50%; }}
.dist-label {{
    background:#fff; border:1px solid #db4040; color:#db4040;
    font-size:12px; font-weight:700; padding:3px 8px; border-radius:6px;
    box-shadow:0 1px 3px rgba(0,0,0,.2); white-space:nowrap;
}}
</style>
</head><body>

<div id="container">
<div id="list-panel">
    <div id="list-header">
        <span>📋 매물 목록</span>
        <span id="list-count" style="font-size:11px;color:#a4a4ac;font-weight:600;"></span>
    </div>
    <div id="list-filter">
        <select id="listDealFilter" onchange="filterList()">
            <option value="전체">전체</option>
            <option value="매매">매매</option>
            <option value="전세">전세</option>
            <option value="월세">월세</option>
        </select>
    </div>
    <div id="list-items"></div>
</div>

<div id="map-wrap">
<div class="ctrl-panel">
    <button class="ctrl-btn" id="btnDistance" onclick="toggleDistanceMode()">📏 거리 측정</button>
    <button class="ctrl-btn" id="btnCluster" onclick="toggleCluster()">📍 클러스터</button>
    <button class="ctrl-btn" id="btnGapLabel" onclick="toggleGapLabels()">🏷️ 시세갭 라벨</button>
</div>
<div class="legend">
    <div class="legend-item"><div class="legend-dot" style="background:#e5484d;"></div>매매</div>
    <div class="legend-item"><div class="legend-dot" style="background:#2f6feb;"></div>전세</div>
    <div class="legend-item"><div class="legend-dot" style="background:#2f9e63;"></div>월세</div>
</div>
<div id="map"></div>
</div>
</div>

<script>
var MARKERS_DATA = {markers_json};
var COLOR_MAP = {{ '매매':'#e5484d', '전세':'#2f6feb', '월세':'#2f9e63' }};

var map = L.map('map', {{ zoomControl: false }}).setView([{center_lat}, {center_lng}], 15);
L.control.zoom({{ position: 'bottomright' }}).addTo(map);

// 타일 레이어 (OpenStreetMap)
L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
    attribution: '© OpenStreetMap', maxZoom: 19
}}).addTo(map);

// SVG 아이콘 생성
function makeIcon(color) {{
    var svg = '<svg xmlns="http://www.w3.org/2000/svg" width="28" height="40" viewBox="0 0 28 40">'
        + '<path d="M14 0C6.3 0 0 6.3 0 14c0 10.5 14 26 14 26s14-15.5 14-26C28 6.3 21.7 0 14 0z" fill="' + color + '"/>'
        + '<circle cx="14" cy="14" r="6" fill="white"/></svg>';
    return L.divIcon({{
        html: svg, className: '', iconSize: [28, 40], iconAnchor: [14, 40], popupAnchor: [0, -40]
    }});
}}

function popupHTML(d) {{
    var hoqa = d.price ? d.price.toLocaleString() + '만원' : '-';
    var avg = d.avgPrice ? d.avgPrice.toLocaleString() + '원/평' : '-';
    var bldg = (d.year && d.floors) ? '🏗️ 준공: ' + Math.floor(d.year) + '년 / ' + Math.floor(d.floors) + '층<br>' : '';
    var gapTxt = (d.gap !== null && d.gap !== undefined)
        ? '<span style="color:' + (d.gap>0?'#1f9d57':'#e5484d') + ';font-weight:bold;">📊 시세갭: ' + (d.gap>0?'+':'') + d.gap.toFixed(1) + '%</span>' : '';
    return '<div style="font-family:Pretendard,sans-serif;font-size:13px;min-width:180px;line-height:1.6;">'
        + '<div style="font-size:15px;font-weight:800;margin-bottom:4px;">' + d.name + '</div><hr style="margin:4px 0;border:none;border-top:1px solid #eee;">'
        + '📍 ' + d.addr + '<br>🏷️ ' + (d.deal||'-') + ' · ' + (d.mtype||'-') + '<br>'
        + '💰 호가: ' + hoqa + '<br>📊 평당가: ' + avg + '<br>' + bldg + gapTxt + '</div>';
}}

var markers = [];
var clusterGroup = L.markerClusterGroup({{ maxClusterRadius: 50 }});

MARKERS_DATA.forEach(function(d, idx) {{
    var color = COLOR_MAP[d.deal] || '#999';
    var marker = L.marker([d.lat, d.lng], {{ icon: makeIcon(color) }});
    marker.bindPopup(popupHTML(d), {{ maxWidth: 280 }});
    marker.on('click', function() {{ _highlightListItem(idx); }});
    marker.addTo(map);
    clusterGroup.addLayer(marker);
    markers.push(marker);
}});

// ── 리스트 패널 ──
function renderList(filter) {{
    var container = document.getElementById('list-items');
    container.innerHTML = '';
    var count = 0;
    MARKERS_DATA.forEach(function(d, i) {{
        if (filter && filter !== '전체' && d.deal !== filter) return;
        count++;
        var color = COLOR_MAP[d.deal] || '#999';
        var hoqa = d.price ? d.price.toLocaleString() + '만원' : '-';
        var gp = (d.gap !== null && d.gap !== undefined)
            ? '<span style="color:' + (d.gap>0?'#1f9d57':'#e5484d') + ';font-weight:bold;">' + (d.gap>0?'+':'') + d.gap.toFixed(1) + '%</span>' : '';
        var div = document.createElement('div');
        div.className = 'list-item';
        div.setAttribute('data-idx', i);
        div.innerHTML = '<div class="li-name"><span class="li-dot" style="background:' + color + '"></span>' + d.name + '</div>'
            + '<div class="li-addr">' + d.addr + '</div>'
            + '<div class="li-info"><span>' + (d.deal||'-') + '</span><span>💰 ' + hoqa + '</span>' + gp + '</div>';
        div.onclick = function() {{ focusMarker(i); }};
        container.appendChild(div);
    }});
    document.getElementById('list-count').textContent = count + '개';
}}

function focusMarker(idx) {{
    var d = MARKERS_DATA[idx], marker = markers[idx];
    map.setView([d.lat, d.lng], 17);
    marker.openPopup();
    _highlightListItem(idx);
}}

function _highlightListItem(idx) {{
    document.querySelectorAll('.list-item').forEach(function(el) {{ el.classList.remove('active'); }});
    var target = document.querySelector('.list-item[data-idx="' + idx + '"]');
    if (target) {{ target.classList.add('active'); target.scrollIntoView({{ behavior:'smooth', block:'nearest' }}); }}
}}

window.filterList = function() {{ renderList(document.getElementById('listDealFilter').value); }};
renderList('전체');

// ── 클러스터 토글 ──
var clusterOn = false;
function toggleCluster() {{
    clusterOn = !clusterOn;
    var btn = document.getElementById('btnCluster');
    if (clusterOn) {{
        markers.forEach(function(m) {{ map.removeLayer(m); }});
        map.addLayer(clusterGroup);
        btn.classList.add('active');
    }} else {{
        map.removeLayer(clusterGroup);
        markers.forEach(function(m) {{ m.addTo(map); }});
        btn.classList.remove('active');
    }}
}}

// ── 시세갭 라벨 ──
var gapLabelsOn = false;
var gapLabelLayers = [];
function toggleGapLabels() {{
    gapLabelsOn = !gapLabelsOn;
    var btn = document.getElementById('btnGapLabel');
    if (gapLabelsOn) {{
        MARKERS_DATA.forEach(function(d, i) {{
            if (d.gap === null || d.gap === undefined) return;
            var col = d.gap > 0 ? '#1f9d57' : '#e5484d';
            var txt = (d.gap > 0 ? '+' : '') + d.gap.toFixed(1) + '%';
            var icon = L.divIcon({{
                html: '<div style="background:#fff;border:1px solid ' + col + ';color:' + col + ';font-size:11px;font-weight:700;padding:2px 7px;border-radius:99px;box-shadow:0 1px 3px rgba(0,0,0,.25);white-space:nowrap;">' + txt + '</div>',
                className: '', iconAnchor: [20, 48]
            }});
            var lbl = L.marker([d.lat, d.lng], {{ icon: icon, interactive: false }}).addTo(map);
            gapLabelLayers.push(lbl);
        }});
        btn.classList.add('active');
    }} else {{
        gapLabelLayers.forEach(function(l) {{ map.removeLayer(l); }});
        gapLabelLayers = [];
        btn.classList.remove('active');
    }}
}}

// ── 거리 측정 ──
var distMode = false, distPoints = [], distLine = null, distMarkers = [], distLabels = [];
function toggleDistanceMode() {{
    distMode = !distMode;
    var btn = document.getElementById('btnDistance');
    if (distMode) {{
        btn.classList.add('active');
        document.getElementById('map').style.cursor = 'crosshair';
    }} else {{
        btn.classList.remove('active');
        document.getElementById('map').style.cursor = '';
        clearDistance();
    }}
}}

function clearDistance() {{
    if (distLine) {{ map.removeLayer(distLine); distLine = null; }}
    distMarkers.forEach(function(m) {{ map.removeLayer(m); }});
    distLabels.forEach(function(l) {{ map.removeLayer(l); }});
    distPoints = []; distMarkers = []; distLabels = [];
}}

map.on('click', function(e) {{
    if (!distMode) return;
    distPoints.push(e.latlng);
    var dot = L.circleMarker(e.latlng, {{ radius:5, color:'#db4040', fillColor:'#db4040', fillOpacity:1 }}).addTo(map);
    distMarkers.push(dot);
    if (distPoints.length > 1) {{
        if (distLine) map.removeLayer(distLine);
        distLine = L.polyline(distPoints, {{ color:'#db4040', weight:3 }}).addTo(map);
        var total = 0;
        for (var i = 1; i < distPoints.length; i++) {{
            total += distPoints[i-1].distanceTo(distPoints[i]);
        }}
        var dist = Math.round(total);
        var wt = Math.floor(dist / 67), bt = Math.floor(dist / 227);
        var wh = wt > 60 ? Math.floor(wt/60) + '시간 ' : '', wm = (wt%60) + '분';
        var bh = bt > 60 ? Math.floor(bt/60) + '시간 ' : '', bm = (bt%60) + '분';
        distLabels.forEach(function(l) {{ map.removeLayer(l); }});
        distLabels = [];
        var icon = L.divIcon({{
            html: '<div class="dist-label">📏 ' + dist + 'm · 🚶 ' + wh + wm + ' · 🚲 ' + bh + bm + '</div>',
            className: '', iconAnchor: [0, -10]
        }});
        var lbl = L.marker(e.latlng, {{ icon: icon, interactive: false }}).addTo(map);
        distLabels.push(lbl);
    }}
}});

map.on('contextmenu', function(e) {{
    if (distMode) {{
        toggleDistanceMode();
    }}
}});
</script>
</body></html>
"""
        import streamlit.components.v1 as components
        components.html(leaflet_map_html, height=620)
        st.caption(f"총 {len(map_rows)}개 매물 표시됨")

# ════════════════ 탭 4: 임장 체크리스트 ════════════════
with tab_check:
    st.subheader("임장 체크리스트")
    st.caption("방문 상태·평점·현장 체크·메모를 매물별로 기록하세요.")

    missing = [c for c in ["상태", "평점", "방문일", "메모", "임장체크"] if c not in schema]
    if missing:
        st.info("노션 DB에 다음 속성을 추가하면 모두 저장됩니다 → "
                + " · ".join(f"`{m}`" for m in missing)
                + "\n\n(상태=선택, 평점=숫자, 방문일=날짜, 메모=텍스트, 임장체크=다중선택)")

    try:
        rows = load_notion_list(notion_token, db_id)
    except Exception as e:
        rows = []
        st.error(f"데이터 로드 실패: {e}")

    if not rows:
        st.info("저장된 매물이 없어요.")
    else:
        plan = [r for r in rows if r.get("상태") == "방문예정"]
        if plan:
            names = "  →  ".join(r["매물명"] for r in plan)
            st.markdown(
                f"<div style='background:{ACCENT};color:#fff;border-radius:13px;padding:16px 22px;margin-bottom:14px;'>"
                f"<div style='font-size:12px;font-weight:700;opacity:.85;'>오늘의 임장 루트 · 방문예정 {len(plan)}건</div>"
                f"<div style='font-size:16px;font-weight:800;margin-top:5px;'>{names}</div></div>",
                unsafe_allow_html=True)

        for r in rows:
            sc = STATUS_COLORS.get(r.get("상태"), "#8a8a93")
            head = f"{r['매물명']}  ·  {r.get('주소','')}  ·  {r.get('상태') or '검토중'}"
            with st.expander(head, expanded=False):
                with st.form(key="chk_" + r["page_id"]):
                    fc1, fc2, fc3 = st.columns([1, 1, 1])
                    with fc1:
                        cur = r.get("상태") or "검토중"
                        status = st.selectbox("상태", STATUS_OPTIONS,
                                              index=STATUS_OPTIONS.index(cur) if cur in STATUS_OPTIONS else 0)
                    with fc2:
                        rating = st.slider("평점", 0.0, 5.0, float(r.get("평점") or 0.0), 0.5)
                    with fc3:
                        try:
                            dval = datetime.fromisoformat(r["방문일"]).date() if r.get("방문일") else date.today()
                        except Exception:
                            dval = date.today()
                        visit_date = st.date_input("방문일", value=dval)
                    checks = st.multiselect("현장 체크", CHECK_ITEMS, default=r.get("임장체크") or [])
                    memo = st.text_area("메모", value=r.get("메모") or "",
                                        placeholder="채광·소음·주차·관리상태·주변 환경 등 현장 인상을 남겨두세요.")
                    if st.form_submit_button("💾 저장", type="primary"):
                        props = {
                            "상태": {"select": {"name": status}},
                            "평점": {"number": float(rating)},
                            "방문일": {"date": {"start": visit_date.isoformat()}},
                            "메모": {"rich_text": [{"text": {"content": memo}}]},
                            "임장체크": {"multi_select": [{"name": c} for c in checks]},
                        }
                        props = filter_props(props, schema)
                        if not props:
                            st.warning("저장 가능한 속성이 없어요. 위 안내의 노션 컬럼을 먼저 추가해주세요.")
                        else:
                            try:
                                update_notion_page(notion_token, r["page_id"], props)
                                st.success("✅ 저장 완료!")
                                st.cache_data.clear()
                            except Exception as e:
                                st.error(f"저장 실패: {e}")
