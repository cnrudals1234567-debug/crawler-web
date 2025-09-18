# app.py
# -------------------------------------------------------------
# Google Places 단독 크롤러 (네이버 사용 안 함)
# - 국가/지역/검색어 + 업종(types) 멀티셀렉트 필터
# - 최소 평점/최소 리뷰 수 필터
# - 반경/페이지 수/호출 지연 설정
# - 결과: CSV / GeoJSON 다운로드
# -------------------------------------------------------------

import streamlit as st
import os, sys, pathlib, subprocess
from subprocess import TimeoutExpired

st.set_page_config(page_title="Google Places 크롤러 (단독 모드)", layout="centered")

st.title("📍 Google Places 크롤러 (단독 모드)")
st.caption("검색어 → Google Places TextSearch → 필터 → CSV/GeoJSON 생성")

# ─────────────────────────────────────────────────────────────
# 업종(한글 라벨) -> Google Places types 매핑
# ─────────────────────────────────────────────────────────────
TYPE_MAP = {
    "식당": ["restaurant", "food"],
    "카페": ["cafe"],
    "바/술집": ["bar", "night_club"],
    "관광지": ["tourist_attraction"],
    "숙소": ["lodging"],
    "미용실": ["hair_care", "beauty_salon"],
    "쇼핑": ["shopping_mall", "clothing_store", "store"],
    "병원/약국": ["hospital", "pharmacy", "doctor"],
    "편의점": ["convenience_store"],
}
TYPE_LABELS = list(TYPE_MAP.keys())

def labels_to_types(labels):
    types = []
    for lb in labels:
        types += TYPE_MAP.get(lb, [])
    return sorted(set(types))

# ─────────────────────────────────────────────────────────────
# 폼 UI
# ─────────────────────────────────────────────────────────────
with st.form(key="form"):
    st.subheader("🔎 검색 조건")
    col1, col2 = st.columns(2)
    with col1:
        country = st.text_input("Country (국가, 한국어)", value="일본", help="예: 일본, 필리핀, 태국, 미국, 프랑스")
        area = st.text_input("Region/City (지역/도시, 선택)", value="도쿄 시부야", help="예: 도쿄 시부야 / 방콕 / 세부 막탄 (비워도 가능)")
        query = st.text_input("Query (검색어)", value="라멘 맛집", help="예: 라멘 맛집 / 분위기 좋은 바 / 감성 카페 등")
        out_name = st.text_input("Output base name", value="result", help="결과 파일 기본 이름 (CSV/GeoJSON)")
    with col2:
        radius_m = st.select_slider("위치 반경 (미터)", options=[2000, 5000, 10000, 15000, 20000], value=10000)
        google_pages = st.slider("Google 결과 페이지 수", 1, 5, 3, help="TextSearch next_page_token 페이징 수(최대 약 60개/페이지)")
        sleep_ms = st.select_slider("호출 지연 (ms)", options=[100, 200, 300, 500, 800, 1000], value=300, help="API 호출 간 지연")
        min_rating = st.number_input("최소 평점", min_value=0.0, max_value=5.0, value=4.2, step=0.1)
        min_reviews = st.number_input("최소 리뷰 수", min_value=0, max_value=100000, value=100, step=10)

    st.markdown("---")
    st.subheader("🏷️ 업종(Types) 필터")
    selected_types = st.multiselect(
        "원하는 업종을 선택하세요 (Places types 필터)",
        TYPE_LABELS,
        default=["식당"],  # 기본은 식당만
        help="선택한 업종만 결과에 포함됩니다. 예: 식당+카페만 보기"
    )
    include_types = ",".join(labels_to_types(selected_types)) if selected_types else ""
    exclude_types = st.text_input("제외할 types (쉼표 구분, 옵션)", value="", help="예: tourist_attraction")

    st.caption("🔒 Google API 키는 환경변수 또는 App secrets에 설정되어야 합니다. (GOOGLE_PLACES_API_KEY 또는 GOOGLE_MAPS_API_KEY)")
    submitted = st.form_submit_button("▶ 실행")

# ─────────────────────────────────────────────────────────────
# 실행
# ─────────────────────────────────────────────────────────────
if submitted:
    # Google 키 확인
    gkey = (
        os.environ.get("GOOGLE_PLACES_API_KEY")
        or os.environ.get("GOOGLE_MAPS_API_KEY")
        or st.secrets.get("GOOGLE_PLACES_API_KEY", None)
        or st.secrets.get("GOOGLE_MAPS_API_KEY", None)
    )

    st.markdown("### 🔐 키 로딩 상태")
    st.write("🔑 GOOGLE_PLACES_API_KEY/GOOGLE_MAPS_API_KEY:", "✅ Loaded" if gkey else "❌ Not found")

    REPO_ROOT = pathlib.Path(__file__).resolve().parent
    # 스크립트 자동 탐색
    candidates = list(REPO_ROOT.rglob("naver_blog_to_places.py"))
    if not candidates:
        st.error("naver_blog_to_places.py 파일을 찾지 못했습니다. 레포지토리 내 위치를 확인하세요.")
        st.stop()
    SCRIPT = candidates[0].as_posix()

    OUT_DIR = "/tmp"
    CSV_PATH = f"{OUT_DIR}/{out_name}.csv"
    GEO_PATH = f"{OUT_DIR}/{out_name}.geojson"

    # 실행 커맨드 구성 (google_only on)
    command = [
        sys.executable, SCRIPT,
        "--google_only",
        "--country", country,
        "--area", area,
        "--query", query,
        "--language", "ko",
        "--radius_m", str(radius_m),
        "--google_result_pages", str(google_pages),
        "--sleep_ms", str(sleep_ms),
        "--out_dir", OUT_DIR,
        "--out_name", out_name,
        "--include_types", include_types,     # 포함 업종
        "--exclude_types", exclude_types,     # 제외 업종(옵션)
        "--min_rating", str(min_rating),      # 최소 평점
        "--min_reviews", str(min_reviews),    # 최소 리뷰 수
    ]

    st.markdown("### ▶ 실행 커맨드")
    st.code(" ".join(command))

    def run_cmd(args, extra_env=None):
        env = os.environ.copy()
        if gkey:
            env["GOOGLE_PLACES_API_KEY"] = gkey  # 백엔드에서 읽음
        if extra_env:
            env.update(extra_env)
        return subprocess.run(args, capture_output=True, text=True, env=env, timeout=900)

    st.info("⏳ 실행 중… (페이지 수 × 페이지당 ~60개 정도)")
    try:
        res = run_cmd(command)
        st.markdown("### 🧾 STDOUT")
        st.code(res.stdout or "(no stdout)")
        st.markdown("### ⚠️ STDERR")
        st.code(res.stderr or "(no stderr)")

        st.markdown("### 📁 결과 다운로드")
        any_file = False
        if os.path.exists(CSV_PATH):
            any_file = True
            with open(CSV_PATH, "rb") as f:
                st.download_button("⬇️ CSV 다운로드", f, file_name=os.path.basename(CSV_PATH), mime="text/csv")
        if os.path.exists(GEO_PATH):
            any_file = True
            with open(GEO_PATH, "rb") as f:
                st.download_button("⬇️ GeoJSON 다운로드", f, file_name=os.path.basename(GEO_PATH), mime="application/geo+json")
        if not any_file:
            st.warning("생성된 파일이 없습니다. 위의 로그를 확인하세요.")

    except TimeoutExpired:
        st.error("⏱️ 시간 초과: 페이지 수/반경/지연(ms)을 조정해서 다시 시도해보세요.")
    except Exception as e:
        st.exception(e)
