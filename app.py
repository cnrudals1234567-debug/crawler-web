# app.py
# -------------------------------------------------------------
# Google Places 단독 크롤러 (상세 확장 + 대용량/운영기능)
# - TextSearch / NearbySearch Grid
# - 업종(types) 멀티셀렉트 + 제외 types
# - 최소 평점/리뷰수 + 영업상태/영업중만 필터
# - 반경/페이지 수/그리드/호출 지연/최대 결과 수
# - Place Details 확장: 영업시간/가격대/전화/웹사이트/요약/UTC 오프셋
# - 운영 기능: 증분 수집(중복 캐시), 리뷰 스냅샷 CSV 저장(옵션)
# - 결과: CSV / GeoJSON (+ 리뷰 CSV 옵션)
# -------------------------------------------------------------

import streamlit as st
import os, sys, pathlib, subprocess
from subprocess import TimeoutExpired

st.set_page_config(page_title="Google Places 크롤러 (대용량·상세)", layout="centered")

st.title("📍 Google Places 크롤러 (대용량·상세)")
st.caption("검색어/지역 → Places 수집 → 상세(영업시간·가격·전화·웹사이트 등) → CSV/GeoJSON")

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

with st.form(key="form"):
    st.subheader("🔎 기본 검색 조건")
    c1, c2 = st.columns(2)
    with c1:
        country = st.text_input("Country (국가, 한국어)", value="일본", help="예: 일본, 필리핀, 태국, 미국, 프랑스")
        area = st.text_input("Region/City (지역/도시, 선택)", value="도쿄 시부야", help="예: 도쿄 시부야 / 방콕 / 세부 막탄 (비워도 가능)")
        query = st.text_input("Query (검색어)", value="라멘 맛집", help="예: 라멘 맛집 / 분위기 좋은 바 / 감성 카페 등")
        out_name = st.text_input("Output base name", value="result", help="결과 파일 기본 이름 (CSV/GeoJSON)")
    with c2:
        radius_m = st.select_slider("반경 (미터)", options=[2000, 5000, 10000, 15000, 20000, 30000], value=10000)
        google_pages = st.slider("TextSearch 페이지 수", 1, 6, 4, help="페이지당 ~20~60개, next_page_token 사용")
        grid_steps = st.slider("Grid 단계(가로/세로)", 1, 5, 3, help="Nearby Grid 모드에서 3→ 7x7(중심 포함)")
        sleep_ms = st.select_slider("호출 지연 (ms)", options=[100, 200, 300, 500, 800, 1000], value=300, help="API 호출 간 지연")
        max_results = st.number_input("최대 결과 수(상한)", min_value=50, max_value=5000, value=600, step=50)

    st.markdown("---")
    st.subheader("🏷️ 업종(Types) 필터")
    selected_types = st.multiselect(
        "원하는 업종을 선택하세요 (Places types 필터)",
        TYPE_LABELS,
        default=["식당"],
        help="선택한 업종만 결과에 포함됩니다. 예: 식당+카페만 보기"
    )
    include_types = ",".join(labels_to_types(selected_types)) if selected_types else ""
    exclude_types = st.text_input("제외할 types (쉼표 구분, 옵션)", value="", help="예: tourist_attraction,night_club")

    st.markdown("---")
    st.subheader("⭐ 품질/영업 상태 필터")
    c3, c4, c5 = st.columns(3)
    with c3:
        min_rating  = st.number_input("최소 평점", min_value=0.0, max_value=5.0, value=4.2, step=0.1)
    with c4:
        min_reviews = st.number_input("최소 리뷰 수", min_value=0, max_value=200000, value=100, step=10)
    with c5:
        open_now_only = st.checkbox("지금 영업중만", value=False)

    business_status_filter = st.selectbox(
        "영업상태 필터",
        ["무관", "OPERATIONAL(영업중)", "CLOSED_TEMPORARILY(일시휴업)", "CLOSED_PERMANENTLY(폐업)"],
        index=1  # 기본: 영업중
    )

    st.markdown("---")
    st.subheader("🧭 수집 모드")
    mode = st.radio("모드 선택", ["TextSearch (간단)", "NearbySearch Grid (많이)"], index=1,
                    help="Grid 모드는 지도에 그리드를 만들어 각 점에서 NearbySearch를 수행하여 더 많은 결과 수집")
    details_on = st.checkbox("Place Details 수집(영업시간/가격/전화/웹사이트/요약/UTC)", value=True)

    st.markdown("---")
    st.subheader("⚙️ 운영 기능")
    use_cache = st.checkbox("증분 수집(기존 seen 캐시 건너뛰기)", value=True)
    reset_cache = st.checkbox("캐시 초기화(이번 실행에서만 무시)", value=False)
    save_reviews = st.checkbox("상위 리뷰 스냅샷 저장(최대 3개/장소, CSV)", value=False)

    st.caption("🔒 Google API 키는 환경변수 또는 App secrets에 설정 (GOOGLE_PLACES_API_KEY 또는 GOOGLE_MAPS_API_KEY)")
    submitted = st.form_submit_button("▶ 실행")

if submitted:
    gkey = (
        os.environ.get("GOOGLE_PLACES_API_KEY")
        or os.environ.get("GOOGLE_MAPS_API_KEY")
        or st.secrets.get("GOOGLE_PLACES_API_KEY", None)
        or st.secrets.get("GOOGLE_MAPS_API_KEY", None)
    )

    st.markdown("### 🔐 키 로딩 상태")
    st.write("🔑 GOOGLE_PLACES_API_KEY/GOOGLE_MAPS_API_KEY:", "✅ Loaded" if gkey else "❌ Not found")

    REPO_ROOT = pathlib.Path(__file__).resolve().parent
    candidates = list(REPO_ROOT.rglob("naver_blog_to_places.py"))
    if not candidates:
        st.error("naver_blog_to_places.py 파일을 찾지 못했습니다. 레포지토리 내 위치를 확인하세요.")
        st.stop()
    SCRIPT = candidates[0].as_posix()

    OUT_DIR = "/tmp"
    CSV_PATH = f"{OUT_DIR}/{out_name}.csv"
    GEO_PATH = f"{OUT_DIR}/{out_name}.geojson"
    REV_CSV_PATH = f"{OUT_DIR}/{out_name}_reviews.csv"

    mode_value = "text" if mode.startswith("TextSearch") else "nearby_grid"

    command = [
        sys.executable, SCRIPT,
        "--mode", mode_value,
        "--country", country,
        "--area", area,
        "--query", query,
        "--language", "ko",
        "--radius_m", str(radius_m),
        "--google_result_pages", str(google_pages),
        "--grid_steps", str(grid_steps),
        "--sleep_ms", str(sleep_ms),
        "--out_dir", OUT_DIR,
        "--out_name", out_name,
        "--include_types", include_types,
        "--exclude_types", exclude_types,
        "--min_rating", str(min_rating),
        "--min_reviews", str(min_reviews),
        "--max_results", str(max_results),
        "--business_status_filter", business_status_filter,
    ]
    if details_on:
        command += ["--details"]
    if open_now_only:
        command += ["--open_now_only"]
    if use_cache:
        command += ["--skip_seen"]
    if reset_cache:
        command += ["--reset_seen"]
    if save_reviews:
        command += ["--save_reviews"]

    st.markdown("### ▶ 실행 커맨드")
    st.code(" ".join(command))

    def run_cmd(args, extra_env=None):
        env = os.environ.copy()
        if gkey:
            env["GOOGLE_PLACES_API_KEY"] = gkey
        if extra_env:
            env.update(extra_env)
        # 대용량 대비 타임아웃 20분
        return subprocess.run(args, capture_output=True, text=True, env=env, timeout=1200)

    st.info("⏳ 실행 중… (Grid 모드는 결과가 많아 시간이 더 걸릴 수 있어요)")
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
        if os.path.exists(REV_CSV_PATH):
            any_file = True
            with open(REV_CSV_PATH, "rb") as f:
                st.download_button("⬇️ 리뷰 스냅샷 CSV 다운로드", f, file_name=os.path.basename(REV_CSV_PATH), mime="text/csv")

        if not any_file:
            st.warning("생성된 파일이 없습니다. 위의 로그를 확인하세요.")

    except TimeoutExpired:
        st.error("⏱️ 시간 초과: 페이지 수/그리드/반경/지연(ms)을 조정해서 다시 시도해보세요.")
    except Exception as e:
        st.exception(e)
