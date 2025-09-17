import os, shlex, subprocess, glob, io, zipfile
import pandas as pd
import streamlit as st

st.set_page_config(page_title="Naver Blog → Google Places", layout="wide")
st.title("🧭 네이버 블로그 → Google Places 크롤러")

for k in ["NAVER_CLIENT_ID","NAVER_CLIENT_SECRET","GOOGLE_MAPS_API_KEY"]:
    if k in st.secrets:
        os.environ[k] = st.secrets[k]

with st.sidebar:
    st.header("검색 설정")
    country = st.text_input("Country", "Japan")
    city    = st.text_input("City", "Tokyo")
    query   = st.text_input("Query (예: 도쿄 라멘 맛집)", "도쿄 라멘 맛집")
    max_posts = st.slider("Max posts (Naver)", 5, 50, 15, 5)
    out_base  = st.text_input("Output base name", "result")
    run = st.button("▶ 실행")

st.write("**Tip:** 결과는 Top10 / 나머지 CSV로 자동 분리됩니다. My Maps에는 CSV를 레이어별로 올리세요.")

def to_mymaps(df: pd.DataFrame) -> pd.DataFrame:
    return df.rename(columns={
        "resolved_name":"Name","lat":"Latitude","lng":"Longitude",
        "formatted_address":"Address","rating":"Rating","user_ratings_total":"Reviews",
        "google_maps_url":"URL"
    })[["Name","Latitude","Longitude","Address","Rating","Reviews","URL"]]

def split_top10(csv_path: str):
    df = pd.read_csv(csv_path)
    for c in ["rating","user_ratings_total","lat","lng"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["lat","lng"]).copy()
    df = df.sort_values(by=["rating","user_ratings_total"], ascending=[False, False])
    top10, rest = df.head(10).copy(), df.iloc[10:].copy()
    base = os.path.splitext(csv_path)[0]
    top10_path, rest_path = f"{base}_top10.csv", f"{base}_rest.csv"
    to_mymaps(top10).to_csv(top10_path, index=False, encoding="utf-8-sig")
    to_mymaps(rest ).to_csv(rest_path , index=False, encoding="utf-8-sig")
    return top10_path, rest_path

def list_outputs(base_prefix: str):
    pats = [f"./{base_prefix}*.csv", f"./{base_prefix}*.geojson"]
    files=[]
    for pat in pats: files += glob.glob(pat)
    return sorted(set(files))

if run:
    script = os.path.abspath("./naver_blog_to_places.py")
    cmd = f'python {shlex.quote(script)} --query {shlex.quote(query)} --city {shlex.quote(city)} --country {shlex.quote(country)} --max_posts {int(max_posts)} --out_name {shlex.quote(out_base)}'
    st.code(cmd, language="bash")
    code = subprocess.call(cmd, shell=True)
    st.write("Exit code:", code)

    main_csv = f"./{out_base}.csv"
    if os.path.exists(main_csv):
        t10, rest = split_top10(main_csv)
        st.success("CSV 분리 완료 (Top10/Rest).")

    files = list_outputs(out_base)
    if files:
        st.subheader("📁 생성된 파일")
        for p in files:
            st.write("•", os.path.basename(p))
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for p in files: z.write(p, arcname=os.path.basename(p))
        st.download_button("⬇ 모든 파일 ZIP 다운로드", data=buf.getvalue(),
                           file_name=f"{out_base}_outputs.zip", mime="application/zip")
    else:
        st.warning("생성된 파일이 없습니다. 로그/키/쿼터를 확인하세요.")
