import os, re, json, time, csv
import argparse
from urllib.parse import urlencode
import requests
from bs4 import BeautifulSoup
from readability import Document
from slugify import slugify
from tqdm import tqdm

# ── Keys (환경변수/Secrets에서 읽기)
NAVER_CLIENT_ID        = os.getenv("NAVER_CLIENT_ID")
NAVER_CLIENT_SECRET    = os.getenv("NAVER_CLIENT_SECRET")
GOOGLE_PLACES_API_KEY  = os.getenv("GOOGLE_PLACES_API_KEY") or os.getenv("GOOGLE_MAPS_API_KEY")

NAVER_BLOG_SEARCH_ENDPOINT = "https://openapi.naver.com/v1/search/blog.json"
USER_AGENT = "Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.0 Mobile/15E148 Safari/604.1"

# 기본 힌트는 비워둔다(요청에 따라 검색어/사용자 힌트가 메인)
POI_HINTS = []

def normalize_naver_blog_url(url: str) -> str:
    m = re.search(r"https?://blog\.naver\.com/([^/]+)/(\d+)", url)
    if m:
        bid, logno = m.group(1), m.group(2)
        return f"https://m.blog.naver.com/{bid}/{logno}"
    return url

def naver_blog_search(query: str, total: int = 30):
    """Naver Blog API: display<=30, start<=100. total 최대 50까지 지원."""
    if not NAVER_CLIENT_ID or not NAVER_CLIENT_SECRET:
        raise RuntimeError("NAVER API 키가 없습니다. NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 확인")
    headers = {"X-Naver-Client-Id": NAVER_CLIENT_ID, "X-Naver-Client-Secret": NAVER_CLIENT_SECRET}
    out = []
    got = 0
    start = 1
    while got < total and start <= 100:
        display = min(30, total - got)
        params = {"query": query, "display": display, "start": start, "sort": "sim"}
        r = requests.get(NAVER_BLOG_SEARCH_ENDPOINT, headers=headers, params=params, timeout=20)
        r.raise_for_status()
        items = r.json().get("items", [])
        if not items:
            break
        out.extend(items)
        c = len(items)
        got += c
        start += c
        if c < display:
            break
    return out[:total]

def fetch_html(url: str) -> str:
    r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=25, allow_redirects=True)
    r.raise_for_status()
    return r.text

def extract_main_text(html: str) -> str:
    try:
        doc = Document(html)
        soup = BeautifulSoup(doc.summary(), "html.parser")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return soup.get_text("\n", strip=True)

def tokenize_query_for_hints(q: str):
    toks = re.split(r"[\s,/|·\-–—]+", q or "")
    out = []
    for t in toks:
        t = re.sub(r"[^0-9A-Za-z가-힣]", "", t)
        if len(t) >= 2:
            out.append(t)
    return out

def build_effective_hints(base_hints, query, extra_csv, hint_mode="query", include_query=True):
    base = [h.strip() for h in (base_hints or []) if h.strip()]
    qh = tokenize_query_for_hints(query) if (include_query and query) else []
    eh = [h.strip() for h in (extra_csv or "").split(",") if h.strip()]
    if hint_mode == "fixed":
        return base
    if hint_mode == "query":
        return list(dict.fromkeys(qh + eh))
    if hint_mode == "none":
        return []
    # both
    return list(dict.fromkeys(base + qh + eh))

def extract_candidate_pois(text: str, top_k: int = 60, hints=None):
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    hints = [h.lower() for h in (hints if hints is not None else POI_HINTS)]
    cand = []
    for ln in lines:
        if hints:
            hit = any(h in ln.lower() for h in hints)
            if not hit:
                continue
        for m in re.findall(r"[A-Za-z0-9&\.\-’' ]{2,40}|[가-힣·&\.\-’' ]{2,40}", ln):
            nm = m.strip(" -—·'’\"")
            if len(nm) >= 2 and re.search(r"[가-힣A-Za-z]", nm) and not nm.isdigit():
                cand.append(nm)
    # 짧은 문장 후보도 추가(옵션): 힌트가 있을 때만
    if hints:
        for ln in lines:
            if 2 <= len(ln) <= 30 and re.search(r"[가-힣A-Za-z]", ln) and not re.search(r"(입니다|했어요|했습니다|http)", ln):
                cand.append(ln)

    ban = {"맛집","레스토랑","식당","카페","restaurant","cafe","bar","bakery","market"}
    cleaned, uniq, seen = [], [], set()
    for c in cand:
        c = re.sub(r"\s{2,}", " ", c)
        c = re.sub(r"^\d+\.\s*", "", c).strip()
        if c.lower() not in ban and len(c) <= 40:
            cleaned.append(c)
    for c in cleaned:
        k = c.lower()
        if k not in seen:
            seen.add(k); uniq.append(c)
    return uniq[:top_k]

def google_places_text_search(query: str, language="ko", loc_bias=None, radius_m=None):
    if not GOOGLE_PLACES_API_KEY:
        raise RuntimeError("Google Places API 키가 없습니다. GOOGLE_PLACES_API_KEY 확인")
    base = "https://maps.googleapis.com/maps/api/place/textsearch/json"
    params = {"query": query, "key": GOOGLE_PLACES_API_KEY, "language": language or "ko"}
    if loc_bias and radius_m:
        lat, lng = loc_bias
        params["location"] = f"{lat},{lng}"
        params["radius"]   = str(radius_m)
    url = f"{base}?{urlencode(params)}"
    r = requests.get(url, timeout=25)
    r.raise_for_status()
    return r.json()

def geocode_city_country(area: str, country: str):
    if not GOOGLE_PLACES_API_KEY:
        return None
    target = (area or "").strip()
    if country:  # 한국어 입력: "일본", "필리핀" 등
        target = (f"{area}, {country}" if area else country).strip(", ")
    if not target:
        return None
    base = "https://maps.googleapis.com/maps/api/geocode/json"
    url = f"{base}?{urlencode({'address': target, 'key': GOOGLE_PLACES_API_KEY, 'language':'ko'})}"
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    js = r.json()
    if js.get("results"):
        loc = js["results"][0]["geometry"]["location"]
        return (loc["lat"], loc["lng"])
    return None

def resolve_candidates_to_places(candidates, area, country="",
                                 language="ko", loc_bias=None, radius_m=None, sleep_ms=300):
    out = []
    for name in tqdm(candidates, desc="Resolving with Google Places"):
        try:
            q = " ".join(x for x in [name, area, country] if x).strip()
            js = google_places_text_search(q, language=language, loc_bias=loc_bias, radius_m=radius_m)
            if js.get("results"):
                top = js["results"][0]
                out.append({
                    "candidate_name": name,
                    "resolved_name": top.get("name", ""),
                    "formatted_address": top.get("formatted_address", ""),
                    "place_id": top.get("place_id", ""),
                    "rating": top.get("rating"),
                    "user_ratings_total": top.get("user_ratings_total"),
                    "types": ",".join(top.get("types", [])),
                    "lat": top.get("geometry", {}).get("location", {}).get("lat"),
                    "lng": top.get("geometry", {}).get("location", {}).get("lng"),
                    "google_maps_url": f"https://www.google.com/maps/place/?q=place_id:{top.get('place_id','')}"
                })
        except Exception as e:
            print(f"[WARN] Places failed for {name} | {e}")
        time.sleep(max(0, sleep_ms) / 1000.0)
    return out

def write_csv(rows, path):
    cols = ["candidate_name","resolved_name","formatted_address","place_id","rating",
            "user_ratings_total","types","lat","lng","google_maps_url","source_url"]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols); w.writeheader()
        for r in rows: w.writerow(r)

def write_geojson(rows, path):
    feats = []
    for r in rows:
        if r.get("lat") is None or r.get("lng") is None: continue
        feats.append({
            "type":"Feature",
            "geometry":{"type":"Point","coordinates":[r["lng"], r["lat"]]},
            "properties":{
                "name": r.get("resolved_name") or r.get("candidate_name"),
                "address": r.get("formatted_address"),
                "rating": r.get("rating"),
                "user_ratings_total": r.get("user_ratings_total"),
                "place_id": r.get("place_id"),
                "google_maps_url": r.get("google_maps_url")
            }
        })
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"type":"FeatureCollection","features":feats}, f, ensure_ascii=False, indent=2)

def load_seen_cache(path):
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_seen_cache(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[WARN] cache save failed: {e}")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--country", required=True)         # 한국어 국가 (예: 일본, 필리핀)
    ap.add_argument("--area", default="")               # 한국어 지역/도시 (예: 도쿄 시부야) - 비워도 OK
    ap.add_argument("--query", required=True)           # 검색어 (예: 현지 분위기 좋은 술집)
    ap.add_argument("--extra_hints", default="")        # 쉼표 구분 힌트
    ap.add_argument("--include_query_as_hint", action="store_true", default=False)
    ap.add_argument("--hint_mode", choices=["fixed","query","both","none"], default="query")

    ap.add_argument("--max_posts", type=int, default=30)      # 최대 50까지 지원
    ap.add_argument("--max_candidates", type=int, default=150)
    ap.add_argument("--sleep_ms", type=int, default=300)
    ap.add_argument("--language", default="ko")
    ap.add_argument("--radius_m", type=int, default=10000)    # 10km
    ap.add_argument("--out_name", default=None)
    ap.add_argument("--out_dir", default="/tmp")

    # 중복 방지용 캐시 (이 키에 대해 이전에 본 블로그 URL은 스킵)
    ap.add_argument("--cache_key", default=None)

    args = ap.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
    base_for_slug = (args.area or args.country) + "-" + args.query
    out_base = args.out_name or slugify(base_for_slug)
    csv_path = os.path.join(args.out_dir, f"{out_base}.csv")
    geo_path = os.path.join(args.out_dir, f"{out_base}.geojson")

    print(f"[ENV] NAVER_CLIENT_ID loaded: {bool(NAVER_CLIENT_ID)}")
    print(f"[ENV] NAVER_CLIENT_SECRET loaded: {bool(NAVER_CLIENT_SECRET)}")
    print(f"[ENV] GOOGLE_PLACES_API_KEY loaded: {bool(GOOGLE_PLACES_API_KEY)}")
    print(f"[OUT] dir={args.out_dir} base={out_base}")

    # 0) Naver 검색 (최대 50)
    total_fetch = min(50, max(1, int(args.max_posts)))
    items = naver_blog_search(args.query, total=total_fetch)
    urls_all = [normalize_naver_blog_url(it["link"]) for it in items]
    print(f"[INFO] fetched {len(urls_all)} blog urls")

    # 0.5) 캐시 기반 URL 필터링 (이전 실행과 중복 스킵)
    cache_file = os.path.join(args.out_dir, "crawl_seen_urls.json")
    cache_data = load_seen_cache(cache_file)
    cache_key = args.cache_key or slugify(f"{args.country}|{args.area}|{args.query}|{args.extra_hints}")
    seen_set = set(cache_data.get(cache_key, []))
    urls = [u for u in urls_all if u not in seen_set]
    print(f"[CACHE] seen={len(seen_set)} -> new urls={len(urls)}")
    # 실행 후 새로 본 URL은 캐시에 추가할 예정

    # 1) 위치편향(지오코딩)
    geo = geocode_city_country(args.area, args.country)
    print(f"[GEO] {args.area or args.country} -> {geo}")

    # 2) 힌트 구성 (검색어 토큰 + extra_hints 위주)
    effective_hints = build_effective_hints(
        base_hints=POI_HINTS,
        query=args.query,
        extra_csv=args.extra_hints,
        hint_mode=args.hint_mode,
        include_query=args.include_query_as_hint or True  # 기본적으로 검색어를 포함
    )
    print(f"[HINTS] mode={args.hint_mode} -> {effective_hints}")

    # 3) 모든 글에서 후보만 수집
    all_cands = []
    for url in urls:
        print(f"[INFO] Fetching: {url}")
        try:
            html = fetch_html(url)
        except Exception as e:
            print(f"[WARN] Fetch failed: {url} | {e}")
            continue
        text = extract_main_text(html)
        cands = extract_candidate_pois(text, top_k=60, hints=effective_hints if args.hint_mode!="none" else [])
        print(f"[INFO] Extracted {len(cands)} candidates from this post")
        all_cands.extend(cands)

    # 4) 후보 정리(중복 제거 + 상한)
    seen_c, uniq_cands = set(), []
    for c in all_cands:
        k = c.strip().lower()
        if k and k not in seen_c:
            seen_c.add(k)
            uniq_cands.append(c)
    if len(uniq_cands) > args.max_candidates:
        print(f"[INFO] Trimming candidates {len(uniq_cands)} -> {args.max_candidates}")
        uniq_cands = uniq_cands[:args.max_candidates]
    print(f"[INFO] Total unique candidates: {len(uniq_cands)}")

    # 5) Places 해석
    rows = resolve_candidates_to_places(
        uniq_cands, args.area or "", args.country or "",
        language=args.language or "ko", loc_bias=geo, radius_m=args.radius_m, sleep_ms=args.sleep_ms
    )
    for r in rows:
        r["source_url"] = urls[0] if urls else ""

    # 6) dedupe & 저장
    uniq = {}
    for r in rows:
        k = r.get("place_id") or f"{r.get('resolved_name')}@{r.get('formatted_address')}"
        if k not in uniq or (r.get('user_ratings_total') or 0) > (uniq[k].get('user_ratings_total') or 0):
            uniq[k] = r
    rows_final = list(uniq.values())

    write_csv(rows_final, csv_path)
    write_geojson(rows_final, geo_path)
    print(f"[DONE] Wrote {csv_path} and {geo_path}")

    # 7) 캐시 업데이트 (이번에 본 URL 추가)
    cache_data.setdefault(cache_key, [])
    cache_data[cache_key] = list(set(cache_data[cache_key] + urls))
    save_seen_cache(cache_file, cache_data)

if __name__ == "__main__":
    main()
