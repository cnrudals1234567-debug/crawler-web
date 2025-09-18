import streamlit as st
import os, sys, subprocess, pathlib, textwrap

st.set_page_config(page_title="네이버 블로그 ➜ Google Places 크롤러")

# ───────────── UI ─────────────
st.title("📍 네이버 블로그 ➜ Google Places 크롤러")
st.markdown("Tip: 결과는 Top10 / 나머지 CSV로 자동 분리됩니다. My Maps에는 CSV를 레이어별로 올리세요.")

with st.form(key="form"):
    country   = st.text_input("Country", value="Japan")
    city      = st.text_input("City", value="Tokyo")
    query     = st.text_input("Query (예: 도쿄 라멘 맛집)", value="도쿄 라멘 맛집")
    max_posts = st.slider("Max posts (Naver)", 5, 50, 15)
    out_name  = st.text_input("Output base name", value="result")

    lang      = st.text_input("Places language (예: ko, ja, en)", value="ko")
    radius_km = st.slider("Search radius (km)", 1, 50, 30)

    submitted = st.form_submit_button("▶ 실행")

# ───────────── 실행 ─────────────
if submitted:
    # 진단 정보
    st.markdown("### 🔍 진단 정보")
    st.code(textwrap.dedent(f"""
    CWD: {os.getcwd()}
    Python: {sys.version.split()[0]}
    Writable /tmp: {os.path.isdir('/tmp')}
    """).strip(), language="bash")

    # Secrets/ENV
    cid  = os.environ.get("NAVER_CLIENT_ID")       or st.secrets.get("NAVER_CLIENT_ID", None)
    csec = os.environ.get("NAVER_CLIENT_SECRET")   or st.secrets.get("NAVER_CLIENT_SECRET", None)
    gkey = os.environ.get("GOOGLE_PLACES_API_KEY") or st.secrets.get("GOOGLE_PLACES_API_KEY", None)

    st.write("🔑 NAVER_CLIENT_ID:",       "✅ Loaded" if cid  else "❌ Not found")
    st.write("🔑 NAVER_CLIENT_SECRET:",   "✅ Loaded" if csec else "❌ Not found")
    st.write("🔑 GOOGLE_PLACES_API_KEY:", "✅ Loaded" if gkey else "❌ Not found")

    # 스크립트 자동 탐색
    REPO_ROOT = pathlib.Path(__file__).resolve().parent
    candidates = list(REPO_ROOT.rglob("naver_blog_to_places.py"))
    if not candidates:
        txt_candidates = list(REPO_ROOT.rglob("naver_blog_to_places.py.txt"))
        st.error("naver_blog_to_places.py 파일을 찾지 못했습니다.")
        tree_sample = "\n".join([str(p.relative_to(REPO_ROOT)) for p in REPO_ROOT.rglob('*')][:100])
        st.code("Repo 내 파일 트리 상위 100개:\n" + tree_sample)
        if txt_candidates:
            st.warning("비슷한 파일을 찾았습니다: " + str(txt_candidates[0].relative_to(REPO_ROOT)))
            st.write("→ 파일명을 'naver_blog_to_places.py' 로 변경 후 커밋/푸시 해주세요.")
        st.stop()

    SCRIPT = candidates[0].as_posix()
    st.write("🗂️ Using script:", SCRIPT)

    OUT_DIR  = "/tmp"
    CSV_PATH = f"{OUT_DIR}/{out_name}.csv"
    GEO_PATH = f"{OUT_DIR}/{out_name}.geojson"

    # 실행 커맨드
    command = [
        sys.executable, SCRIPT,
        "--query", query,
        "--city", city,
        "--country", country,
        "--max_posts", str(max_posts),
        "--out_dir", OUT_DIR,
        "--out_name", out_name,
        "--language", lang,
        "--radius_m", str(int(radius_km * 1000)),
    ]
    st.markdown("#### ▶ 실행 커맨드")
    st.code(" ".join(command), language="bash")

    # 실행 함수
    def run_cmd(args, extra_env=None):
        env = os.environ.copy()
        if cid:  env["NAVER_CLIENT_ID"] = cid
        if csec: env["NAVER_CLIENT_SECRET"] = csec
        if gkey: env["GOOGLE_PLACES_API_KEY"] = gkey
        if extra_env: env.update(extra_env)
        return subprocess.run(args, capture_output=True, text=True, env=env)

    # 사전 청소
    for p in (CSV_PATH, GEO_PATH):
        try:
            if os.path.exists(p): os.remove(p)
        except Exception as e:
            st.warning(f"사전 삭제 실패: {p} - {e}")

    # 실행
    st.write("⏳ 실행 중…")
    res = run_cmd(command, extra_env={"PYTHONUNBUFFERED": "1"})

    st.markdown("#### Exit code:")
    st.code(str(res.returncode), language="bash")

    st.markdown("#### STDOUT")
    st.code(res.stdout or "(no stdout)", language="bash")

    st.markdown("#### STDERR")
    st.code(res.stderr or "(no stderr)", language="bash")

    # 결과 다운로드
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

    # /tmp 목록 (디버깅)
    try:
        tmp_list = os.listdir(OUT_DIR)
        st.markdown("#### /tmp 목록 (상위 50)")
        st.code("\n".join(tmp_list[:50]) or "(empty)", language="bash")
    except Exception as e:
        st.error(f"/tmp 조회 실패: {e}")
