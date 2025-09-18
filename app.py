# app.py
# -------------------------------------------------------------
# 네이버 블로그 ➜ Google Places 크롤러 (업종 필터 UI 포함)
# - 국가/지역/검색어/힌트/최대 포스트/지연/후보수
# - 업종(Types) 멀티셀렉트 UI -> Places types 필터링 전달
# - 실행 결과: CSV/GeoJSON + 블로그 URL 로그 다운로드
# -------------------------------------------------------------

import streamlit as st
import os, sys, pathlib, subprocess
from subprocess import TimeoutExpired

st.set_page_config(page_title="네이버 블로그 ➜ Google Places 크롤러", layout="centered")

st.title("📍 네이버 블로그 ➜ Google Places 크롤러")
st.caption("네이버 블로그 → 장소 추출 → Google Places 매핑 → CSV/GeoJSON 생성")

# ─────────────────────────────────────────────────────────────
# 업종(한글 라벨) -> Google Places types 매핑
#   * 멀티셀렉트에서 선택한 항목을 Places types로 변환해
#   * 백엔드(naver_blog_to_places.py)의 --include_types 로 전달
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
    # 중복 제거
    return sorted(set(types))

# ─────────────────────────────────────────────────────────────
# 폼 UI
# ─────────────────────────────────────────────────────────────
with st.form(key="form"):
    st.subheader("🔎 검색 조건")
    col1, col2 = st.columns(2)
    with col1:
        country = st.text_input("Country (국가, 한국어)", value="일본", help="예: 일본, 필리핀, 태국")
        area = st.text_input("Region/City (지역/도시, 선택)", value="도쿄 시부야", help="예: 도쿄 시부야 / 방콕 / 세부 막탄 (비워도 가능)")
        query = st.text_input("Query (검색어)", value="현지 분위기 좋은 술집", help="네이버 블로그 검색어")
        extra_hints = st.text_input("Hints (쉼표로 구분)", value="현지,맛집,레스토랑", help="추출 정밀도 높이는 보조 힌트")
    with col2:
        max_posts = st.slider("Max posts (Naver)", 5, 50, 30, step=5, help="네이버 블로그 검색 결과 크롤링 개수")
        max_candidates = st.slider("Max candidates (per run)", 30, 200, 150, step=10, help="본문에서 추출되는 후보 최대 개수")
        radius_m = st.select_slider("위치 반경 (미터)", options=[2000, 5000, 10000, 15000, 20000], value=10000, help="지역 중심좌표로부터 검색 반경")
        sleep_ms = st.select_slider("호출 지연 (ms)", options=[100, 200, 300, 500, 800, 1000], value=300, help="Google API 호출 간 지연")
    out_name = st.text_input("Output base name", value="result", help="결과 파일 기본 이름 (CSV/GeoJSON)")

    st.markdown("---")
    st.subheader("🏷️ 업종(Types) 필터")
    selected_types = st.multiselect(
        "원하는 업종을 선택하세요 (Places types 필터)",
        TYPE_LABELS,
        default=["식당"],  # 기본은 식당만
        help="선택한 업종만 결과에 포함됩니다. 예: 식당+카페만 보기"
    )
    include_types = ",".join(labels_to_types(selected_types)) if selected_types else ""

    st.caption("🔒 Secrets는 Streamlit Cloud의 App secrets 또는 환경변수로 설정되어야 합니다.")
    submitted = st.form_submit_button("▶ 실행")

# ─────────────────────────────────────────────────────────────
# 실행
# ─────────────────────────────────────────────────────────────
if submitted:
    # Secrets 로드
    cid  = os.environ.get("NAVER_CLIENT_ID") or st.secrets.get("NAVER_CLIENT_ID", None)
    csec = os.environ.get("NAVER_CLIENT_SECRET") or st.secrets.get("NAVER_CLIENT_SECRET", None)
    gkey = os.environ.get("GOOGLE_PLACES_API_KEY") or os.environ.get("GOOGLE_MAPS_API_KEY") or st.secrets.get("GOOGLE_PLACES_API_KEY", None)

    st.markdown("### 🔐 키 로딩 상태")
    st.write("🔑 NAVER_CLIENT_ID:", "✅ Loaded" if cid else "❌ Not found")
    st.write("🔑 NAVER_CLIENT_SECRET:", "✅ Loaded" if csec else "❌ Not found")
    st.write("🔑 GOOGLE_PLACES_API_KEY:", "✅ Loaded" if gkey else "❌ Not found")

    # 경로 설정
    REPO_ROOT = pathlib.Path(__file__).resolve().parent
    # 스크립트 자동 탐색 (혹시 하위 폴더로 이동해도 첫번째 일치 사용)
    candidates = list(REPO_ROOT.rglob("naver_blog_to_places.py"))
    if not candidates:
        st.error("naver_blog_to_places.py 파일을 찾지 못했습니다. 레포지토리 내 위치를 확인하세요.")
        st.stop()
    SCRIPT = candidates[0].as_posix()

    OUT_DIR = "/tmp"
    CSV_PATH = f"{OUT_DIR}/{out_name}.csv"
    GEO_PATH = f"{OUT_DIR}/{out_name}.geojson"
    LOG_PATH = f"{OUT_DIR}/crawled_urls.csv"

    # 실행 커맨드 구성
    command = [
        sys.executable, SCRIPT,
        "--country", country,
        "--area", area,
        "--query", query,
        "--extra_hints", extra_hints,
        "--include_query_as_hint",
        "--hint_mode", "query",
        "--language", "ko",
        "--radius_m", str(radius_m),
        "--max_posts", str(max_posts),
        "--max_candidates", str(max_candidates),
        "--sleep_ms", str(sleep_ms),
        "--out_dir", OUT_DIR,
        "--out_name", out_name,
        "--no_cache",
        "--log_urls",
        "--include_types", include_types,   # ← 업종 필터 전달 (쉼표구분)
    ]

    st.markdown("### ▶ 실행 커맨드")
    st.code(" ".join(command))

    # 실행 함수
    def run_cmd(args, extra_env=None):
        env = os.environ.copy()
        if cid:  env["NAVER_CLIENT_ID"] = cid
        if csec: env["NAVER_CLIENT_SECRET"] = csec
        if gkey: env["GOOGLE_PLACES_API_KEY"] = gkey
        if extra_env: env.update(extra_env)
        # 최대 15분(900초) 타임아웃
        return subprocess.run(args, capture_output=True, text=True, env=env, timeout=900)

    st.info("⏳ 실행 중… 최대 15분 걸릴 수 있습니다. (후보/지연/포스트 수에 비례)")
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
        if os.path.exists(LOG_PATH):
            any_file = True
            with open(LOG_PATH, "rb") as f:
                st.download_button("⬇️ 블로그 URL 로그 다운로드", f, file_name="crawled_urls.csv", mime="text/csv")
        if not any_file:
            st.warning("생성된 파일이 없습니다. 위의 로그를 확인하세요.")

    except TimeoutExpired:
        st.error("⏱️ 시간 초과: 후보 수/지연(ms)/포스트 수를 낮춰서 다시 시도해보세요.")
    except Exception as e:
        st.exception(e)
