# app.py — Fast 100-cap (DOM-safe)
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

# 결과 전용 안전 컨테이너 (rerun마다 이 안만 갈아끼움)
result_slot = st.empty()

with st.form("run_form", clear_on_submit=False):
    st.subheader("🔎 조건", anchor=False)
    col1, col2 = st.columns(2)
    with col1:
        country = st.text_input("국가", "일본", key="inp_country")
        area    = st.text_input("지역/도시 (선택)", "도쿄 시부야", key="inp_area")
        query   = st.text_input("검색어", "라멘", key="inp_query")
    with col2:
        mode = st.radio("모드", ["TextSearch(빠름)", "Nearby(작은 반경)"], index=0, key="inp_mode")
        out_name = st.text_input("출력 이름", "result", key="inp_outname")

    st.markdown("—")
    st.subheader("🏷️ 필터 (간단)", anchor=False)
    types = st.multiselect("업종(선택 시 해당 types만)", TYPE_LABELS, default=["식당"], key="inp_types")
    include_types = ",".join(labels_to_types(types)) if types else ""
    min_rating = st.number_input("최소 평점", 0.0, 5.0, 3.8, 0.1, key="inp_min_rating")
    min_reviews = st.number_input("최소 리뷰 수", 0, 200000, 20, 10, key="inp_min_reviews")

    st.markdown("—")
    st.subheader("⚙️ 성능/운영", anchor=False)
    details = st.checkbox("Details(영업시간/가격/웹 등) 수집", value=False, key="inp_details",
                          help="켜면 느려질 수 있음")
    area_filter_none = st.checkbox("지역명 포함 필터 끄기(Nearby 권장)", value=True, key="inp_area_filter_none")
    radius_m = st.select_slider("반경(m, Nearby 전용)", options=[1500, 3000, 5000], value=3000, key="inp_radius")
    grid_steps = st.slider("Grid 단계(Nearby 전용)", 1, 3, 2, key="inp_grid",
                           help="2→3x3, 3→5x5")
    sleep_ms = st.select_slider("지연(ms)", options=[100, 200, 300], value=200, key="inp_sleep")
    pages = st.slider("TextSearch 페이지 수", 1, 3, 2, key="inp_pages")

    submitted = st.form_submit_button("▶ 실행", use_container_width=True)

def render_results(stdout_txt: str, stderr_txt: str, csv_path: str, geo_path: str):
    # 이 컨테이너 내부 구성은 rerun 사이에서도 "항상 같은 개수/순서" 유지
    with result_slot.container():
        st.markdown("### 실행 로그", help="stdout / stderr", unsafe_allow_html=False)
        st.code(stdout_txt or "(no stdout)", language="bash", key="out_stdout")
        st.code(stderr_txt or "(no stderr)", language="bash", key="out_stderr")

        st.markdown("### 📁 결과 다운로드")
        csv_exists = os.path.exists(csv_path)
        geo_exists = os.path.exists(geo_path)

        # 두 버튼은 항상 렌더 (없으면 disabled)
        if csv_exists:
            with open(csv_path, "rb") as f:
                st.download_button(
                    "CSV 다운로드",
                    data=f,
                    file_name=os.path.basename(csv_path),
                    mime="text/csv",
                    key="btn_csv",
                    use_container_width=False,
                )
        else:
            st.download_button(
                "CSV 다운로드 (없음)",
                data=b"",
                file_name="no_file.csv",
                mime="text/csv",
                disabled=True,
                key="btn_csv_disabled",
            )

        if geo_exists:
            with open(geo_path, "rb") as f:
                st.download_button(
                    "GeoJSON 다운로드",
                    data=f,
                    file_name=os.path.basename(geo_path),
                    mime="application/geo+json",
                    key="btn_geo",
                    use_container_width=False,
                )
        else:
            st.download_button(
                "GeoJSON 다운로드 (없음)",
                data=b"",
                file_name="no_file.geojson",
                mime="application/geo+json",
                disabled=True,
                key="btn_geo_disabled",
            )

        if not (csv_exists or geo_exists):
            st.info("생성된 파일이 없습니다. 위 로그를 확인하세요.", icon="ℹ️")

if submitted:
    # 결과 컨테이너를 먼저 '자리를 잡아두고' 진행 (시각적 깜빡임 최소/DOM 안정)
    with result_slot.container():
        st.info("⏳ 실행 중…", icon="⏳")

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
        script = p.as_posix()
        break
    if not script:
        # 결과 영역을 통해 에러 표시 (트리 안정성 ↑)
        render_results("", "naver_blog_to_places.py를 찾지 못했습니다.", "", "")
        st.stop()

    OUT_DIR = "/tmp"
    CSV_PATH = f"{OUT_DIR}/{st.session_state.get('inp_outname','result')}.csv"
    GEO_PATH = f"{OUT_DIR}/{st.session_state.get('inp_outname','result')}.geojson"

    mode_val = "text" if st.session_state.get("inp_mode","Text").startswith("Text") else "nearby"
    area_filter_val = "none" if st.session_state.get("inp_area_filter_none", True) else "loose"

    cmd = [
        sys.executable, script,
        "--mode", mode_val,
        "--country", st.session_state.get("inp_country",""),
        "--area", st.session_state.get("inp_area",""),
        "--query", st.session_state.get("inp_query",""),
        "--language", "ko",
        "--include_types", ",".join(labels_to_types(st.session_state.get("inp_types", []))) if st.session_state.get("inp_types") else "",
        "--min_rating", str(st.session_state.get("inp_min_rating", 0.0)),
        "--min_reviews", str(st.session_state.get("inp_min_reviews", 0)),
        "--sleep_ms", str(st.session_state.get("inp_sleep", 200)),
        "--google_result_pages", str(st.session_state.get("inp_pages", 2)),
        "--radius_m", str(st.session_state.get("inp_radius", 3000)),
        "--grid_steps", str(st.session_state.get("inp_grid", 2)),
        "--area_filter", area_filter_val,
        "--out_dir", OUT_DIR,
        "--out_name", st.session_state.get("inp_outname","result"),
        "--max_results", "100",
    ]
    if st.session_state.get("inp_details", False):
        cmd += ["--details"]

    # 고정 위치에 커맨드 미리 출력 (키 지정)
    with result_slot.container():
        st.markdown("### 실행 커맨드")
        st.code(" ".join(cmd), language="bash", key="out_cmd")

    def run(args):
        env = os.environ.copy()
        if gkey:
            env["GOOGLE_PLACES_API_KEY"] = gkey
        return subprocess.run(args, capture_output=True, text=True, env=env, timeout=900)

    try:
        res = run(cmd)
        render_results(res.stdout, res.stderr, CSV_PATH, GEO_PATH)
    except TimeoutExpired:
        render_results("", "⏱️ 시간 초과. 조건을 더 가볍게 조절해 주세요.", CSV_PATH, GEO_PATH)
    except Exception as e:
        render_results("", f"예외: {e}", CSV_PATH, GEO_PATH)
