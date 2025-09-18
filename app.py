import streamlit as st
import os, sys, pathlib, textwrap, subprocess
from subprocess import TimeoutExpired

st.set_page_config(page_title="네이버 블로그 ➜ Google Places 크롤러")

# ───────────── UI ─────────────
st.title("📍 네이버 블로그 ➜ Google Places 크롤러")

st.markdown("""
- 나라는 한국어로 입력 (예: 도쿄 / 일본).
- 지역/도시는 **선택** 입력 (예: 시부야 / 막탄). 비워도 동작합니다.
- 검색어는 원하는 문장 그대로 (예: 현지 분위기 좋은 술집).
- 힌트는 **쉼표(,)** 로 1~3개 정도 권장 (예: 현지, 맛집, 레스토랑).  
  → 힌트는 본문에서 후보 줄을 찾는 기준입니다. 검색어도 자동으로 힌트에 포함됩니다.
""")

with st.form(key="form"):
    country        = st.text_input("Country (국가, 한국어)", value="일본")
    area           = st.text_input("Region/City (지역/도시, 선택)", value="도쿄 시부야")
    query          = st.text_input("Query (검색어)", value="현지 분위기 좋은 술집")
    extra_hints    = st.text_input("Hints (쉼표로 구분, 예: 현지, 맛집, 레스토랑)", value="현지, 맛집, 레스토랑")

    # Naver API는 display 최대 30, start는 1~100
    # 50개까지 원하셔서 2회 호출로 최대 50개를 맞춥니다.
    max_posts      = st.slider("Max posts (Naver 크롤링 수)", 5, 50, 30, step=5)

    out_name       = st.text_input("Output base name (선택)", value="result")

    # 정밀도/속도 관련
    language       = "ko"  # 한국어 고정
    radius_km      = 10    # 위치편향 반경 10km 고정
    max_candidates = st.slider("Max candidates (Places 조회 상한)", 50, 300, 150, step=10)
    sleep_ms       = st.slider("Delay between Places calls (ms)", 100, 180000, 300, step=100)

    submitted      = st.form_submit_button("▶ 실행")

# ───────────── 실행 ─────────────
if submitted:
    st.markdown("### 🔍 진단 정보")
    st.code(textwrap.dedent(f"""
    CWD: {os.getcwd()}
    Python: {sys.version.split()[0]}
    Writable /tmp: {os.path.isdir('/tmp')}
    """).strip(), language="bash")

    # Secrets/ENV 확인 (값은 출력하지 않음)
    cid  = os.environ.get("NAVER_CLIENT_ID")        or st.secrets.get("NAVER_CLIENT_ID", None)
    csec = os.environ.get("NAVER_CLIENT_SECRET")    or st.secrets.get("NAVER_CLIENT_SECRET", None)
    gkey = os.environ.get("GOOGLE_PLACES_API_KEY")  or st.secrets.get("GOOGLE_PLACES_API_KEY", None)

    st.write("🔑 NAVER_CLIENT_ID:",       "✅ Loaded" if cid  else "❌ Not found")
    st.write("🔑 NAVER_CLIENT_SECRET:",   "✅ Loaded" if csec else "❌ Not found")
    st.write("🔑 GOOGLE_PLACES_API_KEY:", "✅ Loaded" if gkey else "❌ Not found")

    # 스크립트 자동 탐색
    REPO_ROOT = pathlib.Path(__file__).resolve().parent
    candidates = list(REPO_ROOT.rglob("naver_blog_to_places.py"))
    if not candidates:
        txt_candidates = list(REPO_ROOT.rglob("naver_blog_to_places.py.txt"))
        st.error("naver_blog_to_places.py 파일을 찾지 못했습니다.")
        tree_sample = "\n".join([str(p.relative_to(REPO_ROOT)) for p in REPO_ROOT.rglob('*')][:120])
        st.code("Repo 내 파일 트리 상위 120개:\n" + tree_sample)
        if txt_candidates:
            st.warning("비슷한 파일을 찾았습니다: " + str(txt_candidates[0].relative_to(REPO_ROOT)))
            st.write("→ 파일명을 'naver_blog_to_places.py' 로 변경 후 커밋/푸시 해주세요.")
        st.stop()

    SCRIPT = candidates[0].as_posix()
    st.write("🗂️ Using script:", SCRIPT)

    OUT_DIR  = "/tmp"
    # out_name이 비어 있으면 스크립트 쪽에서 city/query 기반으로 자동 생성
    CSV_PATH = f"{OUT_DIR}/{out_name}.csv" if out_name else None
    GEO_PATH = f"{OUT_DIR}/{out_name}.geojson" if out_name else None

    # 캐시 키: 같은 조건으로 실행 시 이전에 본 블로그 URL은 스킵
    cache_key = f"{country}|{area}|{query}|{extra_hints}"

    # 실행 커맨드
    command = [
        sys.executable, SCRIPT,
        "--country",      country,
        "--area",         area,
        "--query",        query,
        "--extra_hints",  extra_hints,
        "--include_query_as_hint",
        "--hint_mode",    "query",            # 기본 힌트 없이, (검색어 토큰 + extra_hints)만 사용
        "--language",     language,           # ko
        "--radius_m",     str(int(radius_km * 1000)),  # 10km
        "--max_posts",    str(max_posts),
        "--max_candidates", str(max_candidates),
        "--sleep_ms",     str(sleep_ms),
        "--out_dir",      OUT_DIR,
        "--cache_key",    cache_key,
    ]
    if out_name:
        command += ["--out_name", out_name]

    st.markdown("#### ▶ 실행 커맨드")
    st.code(" ".join(command), language="bash")

    # 실행 함수
    def run_cmd(args, extra_env=None):
        env = os.environ.copy()
        if cid:  env["NAVER_CLIENT_ID"] = cid
        if csec: env["NAVER_CLIENT_SECRET"] = csec
        if gkey: env["GOOGLE_PLACES_API_KEY"] = gkey
        if extra_env: env.update(extra_env)
        # 최대 15분(900초) 타임아웃
        return subprocess.run(args, capture_output=True, text=True, env=env, timeout=900)

    # 실행
    st.write("⏳ 실행 중… (최대 15분)")
    try:
        res = run_cmd(command, extra_env={"PYTHONUNBUFFERED": "1"})
    except TimeoutExpired:
        st.error("⏱️ 시간 초과(15분). 후보가 너무 많거나 외부 API 응답이 지연되었습니다. Max posts 또는 Max candidates를 줄이세요.")
        res = None

    if res is not None:
        st.markdown("#### Exit code:")
        st.code(str(res.returncode), language="bash")

        st.markdown("#### STDOUT")
        st.code(res.stdout or "(no stdout)", language="bash")

        st.markdown("#### STDERR")
        st.code(res.stderr or "(no stderr)", language="bash")

    # 결과 다운로드
    st.markdown("### 📁 결과 파일")
    # out_name 미지정 시, 파일명은 스크립트가 자동 생성하므로 /tmp를 스캔해서 최근 파일 제시
    files = []
    if out_name:
        csvp = CSV_PATH; geop = GEO_PATH
        if csvp and os.path.exists(csvp): files.append(("CSV", csvp))
        if geop and os.path.exists(geop): files.append(("GeoJSON", geop))
    else:
        # /tmp 밑 최신 CSV/GeoJSON 5개 정도 찾아서 표출
        try:
            tmpdir = pathlib.Path(OUT_DIR)
            cand = sorted(tmpdir.glob("*.csv"))[-3:] + sorted(tmpdir.glob("*.geojson"))[-3:]
            for p in cand:
                files.append((p.suffix.replace(".","").upper(), p.as_posix()))
        except Exception:
            pass

    if not files:
        st.warning("생성된 파일이 없습니다. 위의 STDOUT/STDERR를 확인하세요.")
    else:
        for label, path in files:
            try:
                with open(path, "rb") as f:
                    st.download_button(
                        label=f"⬇️ Download {label}: {os.path.basename(path)}",
                        data=f.read(),
                        file_name=os.path.basename(path),
                        mime="text/csv" if label.upper()=="CSV" else "application/geo+json"
                    )
            except Exception as e:
                st.warning(f"다운로드 실패: {path} - {e}")

    # /tmp 목록 (디버깅)
    try:
        tmp_list = os.listdir(OUT_DIR)
        st.markdown("#### /tmp 목록 (상위 60)")
        st.code("\n".join(tmp_list[:60]) or "(empty)", language="bash")
    except Exception as e:
        st.error(f"/tmp 조회 실패: {e}")
