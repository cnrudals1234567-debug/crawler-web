# app.py — Fast 100-cap version
import streamlit as st
import os, sys, pathlib, subprocess
from subprocess import TimeoutExpired

st.set_page_config(page_title="Google Places 빠른 수집 (≤100)", layout="centered")
st.title("⚡ Google Places 빠른 수집 (항상 ≤ 100개)")
st.caption("빠른 응답을 위해 TextSearch 기본, 최대 100개로 하드 제한. Details는 옵션(느려질 수 있음).")

TYPE_MAP = {
    "식당": ["restaurant", "food"],
    "카페": ["cafe"],
    "바/술집": ["bar", "night_club"],
    "관광지": ["tourist_attraction"],
}
TYPE_LABELS = list(TYPE_MAP.keys())

def labels_to_types(labels):
    out = []
    for lb in labels:
        out += TYPE_MAP.get(lb, [])
    return sorted(set(out))

with st.form("form"):
    st.subheader("🔎 조건")
    col1, col2 = st.columns(2)
    with col1:
        country = st.text_input("국가", "일본")
        area    = st.text_input("지역/도시 (선택)", "도쿄 시부야")
        query   = st.text_input("검색어", "라멘")
    with col2:
        mode = st.radio("모드", ["TextSearch(빠름)", "Nearby(작은 반경)"], index=0)
        out_name = st.text_input("출력 이름", "result")

    st.markdown("—")
    st.subheader("🏷️ 필터 (간단)")
    types = st.multiselect("업종(선택 시 해당 types만)", TYPE_LABELS, default=["식당"])
    include_types = ",".join(labels_to_types(types)) if types else ""
    min_rating = st.number_input("최소 평점", 0.0, 5.0, 3.8, 0.1)
    min_reviews = st.number_input("최소 리뷰 수", 0, 200000, 20, 10)

    st.markdown("—")
    st.subheader("⚙️ 성능/운영")
    details = st.checkbox("Details(영업시간/가격/웹 등) 수집", value=False, help="켜면 느려질 수 있음")
    area_filter_none = st.checkbox("지역명 포함 필터 끄기(Nearby 권장)", value=True)
    radius_m = st.select_slider("반경(m, Nearby 전용)", options=[1500, 3000, 5000], value=3000)
    grid_steps = st.slider("Grid 단계(Nearby 전용)", 1, 3, 2, help="2→3x3, 3→5x5")
    sleep_ms = st.select_slider("지연(ms)", options=[100, 200, 300], value=200)
    pages = st.slider("TextSearch 페이지 수", 1, 3, 2)
    submitted = st.form_submit_button("▶ 실행")

if submitted:
    gkey = (
        os.environ.get("GOOGLE_PLACES_API_KEY")
        or os.environ.get("GOOGLE_MAPS_API_KEY")
        or st.secrets.get("GOOGLE_PLACES_API_KEY", None)
        or st.secrets.get("GOOGLE_MAPS_API_KEY", None)
    )
    st.write("🔑 키:", "✅ Loaded" if gkey else "❌ Not found")

    REPO_ROOT = pathlib.Path(__file__).resolve().parent
    script = None
    for p in REPO_ROOT.rglob("naver_blog_to_places.py"):
        script = p.as_posix(); break
    if not script:
        st.error("naver_blog_to_places.py를 찾지 못했습니다."); st.stop()

    OUT_DIR = "/tmp"
    CSV_PATH = f"{OUT_DIR}/{out_name}.csv"
    GEO_PATH = f"{OUT_DIR}/{out_name}.geojson"

    mode_val = "text" if mode.startswith("Text") else "nearby"
    area_filter_val = "none" if area_filter_none else "loose"

    cmd = [
        sys.executable, script,
        "--mode", mode_val,
        "--country", country,
        "--area", area,
        "--query", query,
        "--language", "ko",
        "--include_types", include_types,
        "--min_rating", str(min_rating),
        "--min_reviews", str(min_reviews),
        "--sleep_ms", str(sleep_ms),
        "--google_result_pages", str(pages),
        "--radius_m", str(radius_m),
        "--grid_steps", str(grid_steps),
        "--area_filter", area_filter_val,
        "--out_dir", OUT_DIR,
        "--out_name", out_name,
        "--max_results", "100",       # 하드 제한(CLI에서도 명시)
    ]
    if details: cmd += ["--details"]

    st.code(" ".join(cmd))
    def run(args):
        env = os.environ.copy()
        if gkey: env["GOOGLE_PLACES_API_KEY"] = gkey
        return subprocess.run(args, capture_output=True, text=True, env=env, timeout=900)

    st.info("⏳ 실행 중…")
    try:
        res = run(cmd)
        st.subheader("STDOUT"); st.code(res.stdout or "(no stdout)")
        st.subheader("STDERR"); st.code(res.stderr or "(no stderr)")

        st.subheader("📁 결과 다운로드")
        anyf = False
        if os.path.exists(CSV_PATH):
            anyf = True
            with open(CSV_PATH, "rb") as f:
                st.download_button("CSV 다운로드", f, file_name=os.path.basename(CSV_PATH), mime="text/csv")
        if os.path.exists(GEO_PATH):
            anyf = True
            with open(GEO_PATH, "rb") as f:
                st.download_button("GeoJSON 다운로드", f, file_name=os.path.basename(GEO_PATH), mime="application/geo+json")
        if not anyf:
            st.warning("생성된 파일이 없습니다. 위 로그를 확인하세요.")
    except TimeoutExpired:
        st.error("⏱️ 시간 초과. 조건을 더 가볍게 조절해 주세요.")
    except Exception as e:
        st.exception(e)
