# app.py

import streamlit as st
import os, sys, pathlib, subprocess
from subprocess import TimeoutExpired

st.set_page_config(page_title="네이버 블로그 ➜ Google Places 크롤러")

st.title("📍 네이버 블로그 ➜ Google Places 크롤러")

with st.form(key="form"):
    country = st.text_input("Country (국가, 한국어)", value="일본")
    area = st.text_input("Region/City (지역/도시, 선택)", value="도쿄 시부야")
    query = st.text_input("Query (검색어)", value="현지 분위기 좋은 술집")
    extra_hints = st.text_input("Hints (쉼표로 구분, 예: 현지,맛집,레스토랑)", value="현지,맛집,레스토랑")
    max_posts = st.slider("Max posts (Naver)", 5, 50, 30, step=5)
    out_name = st.text_input("Output base name", value="result")
    submitted = st.form_submit_button("▶ 실행")

if submitted:
    cid  = os.environ.get("NAVER_CLIENT_ID") or st.secrets.get("NAVER_CLIENT_ID", None)
    csec = os.environ.get("NAVER_CLIENT_SECRET") or st.secrets.get("NAVER_CLIENT_SECRET", None)
    gkey = os.environ.get("GOOGLE_PLACES_API_KEY") or st.secrets.get("GOOGLE_PLACES_API_KEY", None)

    st.write("🔑 NAVER_CLIENT_ID:", "✅ Loaded" if cid else "❌ Not found")
    st.write("🔑 NAVER_CLIENT_SECRET:", "✅ Loaded" if csec else "❌ Not found")
    st.write("🔑 GOOGLE_PLACES_API_KEY:", "✅ Loaded" if gkey else "❌ Not found")

    REPO_ROOT = pathlib.Path(__file__).resolve().parent
    SCRIPT = (REPO_ROOT / "naver_blog_to_places.py").as_posix()
    OUT_DIR = "/tmp"
    CSV_PATH = f"{OUT_DIR}/{out_name}.csv"
    GEO_PATH = f"{OUT_DIR}/{out_name}.geojson"
    LOG_PATH = f"{OUT_DIR}/crawled_urls.csv"

    command = [
        sys.executable, SCRIPT,
        "--country", country,
        "--area", area,
        "--query", query,
        "--extra_hints", extra_hints,
        "--include_query_as_hint",
        "--hint_mode", "query",
        "--language", "ko",
        "--radius_m", "10000",
        "--max_posts", str(max_posts),
        "--max_candidates", "150",
        "--sleep_ms", "300",
        "--out_dir", OUT_DIR,
        "--out_name", out_name,
        "--no_cache",
        "--log_urls"
    ]

    st.code(" ".join(command))

    def run_cmd(args, extra_env=None):
        env = os.environ.copy()
        if cid: env["NAVER_CLIENT_ID"] = cid
        if csec: env["NAVER_CLIENT_SECRET"] = csec
        if gkey: env["GOOGLE_PLACES_API_KEY"] = gkey
        if extra_env: env.update(extra_env)
        return subprocess.run(args, capture_output=True, text=True, env=env, timeout=900)

    st.write("⏳ 실행 중… 최대 15분 소요될 수 있음")
    try:
        res = run_cmd(command)
        st.code(res.stdout or "(no stdout)")
        st.code(res.stderr or "(no stderr)")
    except TimeoutExpired:
        st.error("⏱️ 시간 초과")

    if os.path.exists(CSV_PATH):
        with open(CSV_PATH, "rb") as f:
            st.download_button("⬇️ CSV 다운로드", f, file_name=os.path.basename(CSV_PATH))
    if os.path.exists(GEO_PATH):
        with open(GEO_PATH, "rb") as f:
            st.download_button("⬇️ GeoJSON 다운로드", f, file_name=os.path.basename(GEO_PATH))
    if os.path.exists(LOG_PATH):
        with open(LOG_PATH, "rb") as f:
            st.download_button("⬇️ 블로그 URL 목록 다운로드", f, file_name="crawled_urls.csv")