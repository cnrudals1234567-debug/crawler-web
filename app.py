# app.py 전체 교체

import streamlit as st
import os, sys, subprocess, pathlib

st.set_page_config(page_title="네이버 블로그 ➜ Google Places 크롤러")

# ───────────── UI 영역 ─────────────

st.title("📍 네이버 블로그 ➜ Google Places 크롤러")
st.markdown("Tip: 결과는 Top10 / 나머지 CSV로 자동 분리됩니다. My Maps에는 CSV를 레이어별로 올리세요.")

with st.form(key="form"):
    country = st.text_input("Country", value="Japan")
    city = st.text_input("City", value="Tokyo")
    query = st.text_input("Query (예: 도쿄 라멘 맛집)", value="도쿄 라멘 맛집")
    max_posts = st.slider("Max posts (Naver)", 5, 50, 15)
    out_name = st.text_input("Output base name", value="result")
    submitted = st.form_submit_button("▶ 실행")

# ───────────── 실행 영역 ─────────────

if submitted:

    # ── 디버깅 정보 출력 ──
    st.markdown("### 🔍 진단 정보")
    st.code(f"""
CWD: {os.getcwd()}
Python: {sys.version.split()[0]}
Writable /tmp: {os.path.isdir('/tmp')}
""", language="bash")

    # ── Secrets 확인 ──
    cid = os.environ.get("NAVER_CLIENT_ID") or st.secrets.get("NAVER_CLIENT_ID")
    csec = os.environ.get("NAVER_CLIENT_SECRET") or st.secrets.get("NAVER_CLIENT_SECRET")
    gkey = os.environ.get("GOOGLE_PLACES_API_KEY") or st.secrets.get("GOOGLE_PLACES_API_KEY")

    st.write("🔑 NAVER_CLIENT_ID:", "✅ Loaded" if cid else "❌ Not found")
    st.write("🔑 NAVER_CLIENT_SECRET:", "✅ Loaded" if csec else "❌ Not found")
    st.write("🔑 GOOGLE_PLACES_API_KEY:", "✅ Loaded" if gkey else "❌ Not found")

    # ── 경로 설정 ──
    REPO_ROOT = pathlib.Path(__file__).resolve().parent
    SCRIPT = (REPO_ROOT / "naver_blog_to_places.py").as_posix()
    OUT_DIR = "/tmp"
    CSV_PATH = f"{OUT_DIR}/{out_name}.csv"
    GEO_PATH = f"{OUT_DIR}/{out_name}.geojson"

    command = [
        sys.executable,
        SCRIPT,
        "--query", query,
        "--city", city,
        "--country", country,
        "--max_posts", str(max_posts),
        "--out_dir", OUT_DIR,
        "--out_name", out_name,
    ]

    st.markdown("#### ▶ 실행 커맨드")
    st.code(" ".join(command))

    # ── 실행 ──
    def run_cmd(args, extra_env=None):
        env = os.environ.copy()
        if cid: env["NAVER_CLIENT_ID"] = cid
        if csec: env["NAVER_CLIENT_SECRET"] = csec
        if gkey: env["GOOGLE_PLACES_API_KEY"] = gkey
        if extra_env:
            env.update(extra_env)
        return subprocess.run(args, capture_output=True, text=True, env=env)

    st.write("⏳ 실행 중…")
    res = run_cmd(command)

    st.markdown("#### Exit code:")
    st.code(str(res.returncode))

    st.markdown("#### STDOUT")
    st.code(res.stdout or "(no stdout)")

    st.markdown("#### STDERR")
    st.code(res.stderr or "(no stderr)")

    # ── 결과 파일 다운로드 ──
    st.markdown("### 📁 결과 파일")
    files = []
    if os.path.exists(CSV_PATH): files.append(("CSV", CSV_PATH))
    if os.path.exists(GEO_PATH): files.append(("GeoJSON", GEO_PATH))

    if not files:
        st.warning("생성된 파일이 없습니다. 위의 STDOUT/STDERR를 확인하세요.")
    else:
        for label, path in files:
            with open(path, "rb") as f:
                st.download_button(
                    label=f"⬇️ Download {label}",
                    data=f.read(),
                    file_name=os.path.basename(path),
                    mime="text/csv" if label == "CSV" else "application/geo+json"
                )
