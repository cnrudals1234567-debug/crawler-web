# naver_blog_to_places.py
# -------------------------------------------------------------
# 네이버 블로그 검색 → 본문 크롤링 → 장소 후보 추출 → Google Places 매핑
# - 국가/지역 필터 (국가코드 엄격 일치)
# - 업종(types) include/exclude 필터
# - dedupe 시 place_id 기준 + source_urls(모든 출처) 누적
# - 결과: CSV/GeoJSON + 블로그 URL 로그(옵션)
# -------------------------------------------------------------

import os, re, json, time, csv, argparse
from typing import List, Tuple, Optional, Dict, Any
import requests
from urllib.parse import urlencode
from bs4 import BeautifulSoup
from readability import Document
from slugify import slugify
from tqdm import tqdm

# ─────────────────────────────────────────────────────────────
# 환경변수 / 키
# ─────────────────────────────────────────────────────────────
NAVER_CLIENT_ID = os.getenv("NAVER_CLIENT_ID")
NAVER_CLIENT_SECRET = os.getenv("NAVER_CLIENT_SECRET")
# GOOGLE_PLACES_API_KEY 또는 GOOGLE_MAPS_API_KEY 둘 중 하나 지원
GOOGLE_PLACES_API_KEY = os.getenv("GOOGLE_PLACES_API_KEY") or os.getenv("GOOGLE_MAPS_API_KEY")

NAVER_BLOG_SEARCH_ENDPOINT = "https://openapi.naver.com/v1/search/blog.json"
UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.0 Mobile/15E148 Safari/604.1"

# ─────────────────────────────────────────────────────────────
# 한글 업종 라벨 매핑 (CSV에 '업종' 컬럼으로 추가)
# ─────────────────────────────────────────────────────────────
KOR_TYPE_LABELS = [
    ("식당", ["restaurant", "food"]),
    ("카페", ["cafe"]),
    ("바/술집", ["bar", "night_club"]),
    ("관광지", ["tourist_attraction"]),
    ("숙소", ["lodging"]),
    ("미용실", ["hair_care", "beauty_salon"]),
    ("쇼핑", ["shopping_mall", "clothing_store", "store"]),
    ("병원/약국", ["hospital", "pharmacy", "doctor"]),
    ("편의점", ["convenience_store"]),
]

# 간단 지역 한글→영문 alias (필요시 확장)
AREA_ALIAS = {
    "도쿄": "tokyo",
    "시부야": "shibuya",
    "신주쿠": "shinjuku",
    "오사카": "osaka",
    "교토": "kyoto",
    "삿포로": "sapporo",
    "후쿠오카": "fukuoka",
}


# ─────────────────────────────────────────────────────────────
# 유틸
# ─────────────────────────────────────────────────────────────
def to_kor_label(types_list: List[str]) -> str:
    tset = set(types_list or [])
    for label, tps in KOR_TYPE_LABELS:
        if tset.intersection(tps):
            return label
    return "기타"


def ensure_keys():
    if not NAVER_CLIENT_ID or not NAVER_CLIENT_SECRET:
        raise RuntimeError("NAVER API 키가 없습니다. NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 확인")
    if not GOOGLE_PLACES_API_KEY:
        raise RuntimeError("Google Places API 키가 없습니다. GOOGLE_PLACES_API_KEY 또는 GOOGLE_MAPS_API_KEY 확인")


# ─────────────────────────────────────────────────────────────
# NAVER BLOG
# ─────────────────────────────────────────────────────────────
def naver_blog_search(query: str, total: int = 30) -> List[Dict[str, Any]]:
    ensure_keys()
    headers = {
        "X-Naver-Client-Id": NAVER_CLIENT_ID,
        "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
    }
    out, start, got = [], 1, 0
    while got < total and start <= 100:
        display = min(30, total - got)
        params = {"query": query, "display": display, "start": start, "sort": "sim"}
        r = requests.get(NAVER_BLOG_SEARCH_ENDPOINT, headers=headers, params=params, timeout=20)
        r.raise_for_status()
        items = r.json().get("items", [])
        if not items:
            break
        out.extend(items)
        got += len(items)
        start += len(items)
    return out[:total]


def normalize_naver_blog_url(url: str) -> str:
    m = re.search(r"https?://blog\.naver\.com/([^/]+)/(\d+)", url)
    if m:
        return f"https://m.blog.naver.com/{m.group(1)}/{m.group(2)}"
    return url


def fetch_html(url: str) -> str:
    r = requests.get(url, headers={"User-Agent": UA}, timeout=25, allow_redirects=True)
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


def tokenize_query_for_hints(q: str) -> List[str]:
    toks = re.split(r"[\s,/|·\-–—]+", q or "")
    out = []
    for t in toks:
        t = re.sub(r"[^0-9A-Za-z가-힣]", "", t)
        if len(t) >= 2:
            out.append(t)
    return out


def extract_candidate_pois(text: str, hints: List[str], top_k: int = 60) -> List[str]:
    """
    - 힌트가 있으면: 힌트가 포함된 라인에서 후보 추출 (+짧은 제목형 라인 보조 허용)
    - 힌트가 없으면: 전 라인 스캔
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    use_hints = [h.lower() for h in (hints or [])]
    cand = []
    for ln in lines:
        if use_hints:
            if not any(h in ln.lower() for h in use_hints):
                # 짧은 제목형 라인은 보조 허용(너무 좁지 않도록)
                if not (2 <= len(ln) <= 30 and re.search(r"[가-힣A-Za-z]", ln)):
                    continue
        for m in re.findall(r"[A-Za-z0-9&\.\-’' ]{2,40}|[가-힣·&\.\-’' ]{2,40}", ln):
            nm = m.strip(" -—·'’\"")
            if len(nm) >= 2 and re.search(r"[가-힣A-Za-z]", nm) and not nm.isdigit():
                cand.append(nm)
    # dedupe
    uniq, seen = [], set()
    for c in cand:
        k = c.lower()
        if k not in seen:
            seen.add(k)
            uniq.append(c)
    return uniq[:top_k]


# ─────────────────────────────────────────────────────────────
# Google Geocoding / Places
# ─────────────────────────────────────────────────────────────
def geocode_area_country(area: str, country: str) -> Tuple[Optional[float], Optional[float], Optional[str]]:
    if not GOOGLE_PLACES_API_KEY:
        return None, None, None
    target = (f"{area}, {country}" if area else country).strip(", ")
    base = "https://maps.googleapis.com/maps/api/geocode/json"
    url = f"{base}?{urlencode({'address': target, 'key': GOOGLE_PLACES_API_KEY, 'language':'ko'})}"
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    js = r.json()
    if not js.get("results"):
        return None, None, None
    res = js["results"][0]
    loc = res["geometry"]["location"]
    lat, lng = loc["lat"], loc["lng"]
    cc = None
    for comp in res.get("address_components", []):
        if "country" in comp.get("types", []):
            cc = comp.get("short_name")
            break
    return lat, lng, cc


def google_places_text_search(query: str, language="ko", loc_bias=None, radius_m=None) -> Dict[str, Any]:
    if not GOOGLE_PLACES_API_KEY:
        raise RuntimeError("Google Places API 키가 없습니다.")
    base = "https://maps.googleapis.com/maps/api/place/textsearch/json"
    params = {"query": query, "key": GOOGLE_PLACES_API_KEY, "language": language or "ko"}
    if loc_bias and radius_m:
        lat, lng = loc_bias
        params["location"] = f"{lat},{lng}"
        params["radius"] = str(radius_m)
    r = requests.get(base, params=params, timeout=25)
    r.raise_for_status()
    return r.json()


def get_place_country_code(place_id: str) -> Optional[str]:
    base = "https://maps.googleapis.com/maps/api/place/details/json"
    params = {"place_id": place_id, "fields": "address_component", "key": GOOGLE_PLACES_API_KEY}
    r = requests.get(base, params=params, timeout=20)
    r.raise_for_status()
    js = r.json()
    for comp in js.get("result", {}).get("address_components", []):
        if "country" in comp.get("types", []):
            return comp.get("short_name")
    return None


def resolve_candidates_to_places(
    candidates: List[str], area: str, country: str = "",
    language: str = "ko", loc_bias: Optional[Tuple[float, float]] = None, radius_m: Optional[int] = None,
    sleep_ms: int = 300, target_country_code: Optional[str] = None, require_area_in_address: bool = True,
    include_types: Optional[str] = None, exclude_types: Optional[str] = None
) -> List[Dict[str, Any]]:
    out = []
    area_tokens = [t for t in re.split(r"\s+", (area or "").strip()) if t]
    include_set = set((include_types or "").split(",")) - {""}
    exclude_set = set((exclude_types or "").split(",")) - {""}

    for name in tqdm(candidates, desc="Resolving with Google Places"):
        try:
            q = " ".join(x for x in [name, area, country] if x).strip()
            js = google_places_text_search(q, language=language, loc_bias=loc_bias, radius_m=radius_m)
            results = js.get("results", [])
            if not results:
                time.sleep(max(0, sleep_ms) / 1000.0)
                continue

            top = results[0]
            fmt = top.get("formatted_address", "") or ""
            place_types = top.get("types", []) or []

            # [A] 지역 토큰 검사 (한글/영문 보조)
            if require_area_in_address and area_tokens:
                lowered = fmt.lower()
                if not any(tok.lower() in lowered for tok in area_tokens):
                    # 간단 alias (한글→영문)
                    if not any(AREA_ALIAS.get(tok, "") in lowered for tok in area_tokens):
                        # 지역 표시가 주소에 전혀 없으면 제외
                        time.sleep(max(0, sleep_ms) / 1000.0)
                        continue

            # [B] 국가코드 엄격 필터 (cc가 None이어도 배제)
            cc = get_place_country_code(top.get("place_id", ""))
            if target_country_code and cc != target_country_code:
                time.sleep(max(0, sleep_ms) / 1000.0)
                continue

            # [C] 업종 types 필터
            tset = set(place_types)
            if include_set and not tset.intersection(include_set):
                time.sleep(max(0, sleep_ms) / 1000.0)
                continue
            if exclude_set and tset.intersection(exclude_set):
                time.sleep(max(0, sleep_ms) / 1000.0)
                continue

            out.append({
                "candidate_name": name,
                "resolved_name": top.get("name", ""),
                "formatted_address": fmt,
                "place_id": top.get("place_id", ""),
                "rating": top.get("rating"),
                "user_ratings_total": top.get("user_ratings_total"),
                "types": ",".join(place_types),
                "업종": to_kor_label(place_types),
                "lat": top.get("geometry", {}).get("location", {}).get("lat"),
                "lng": top.get("geometry", {}).get("location", {}).get("lng"),
                "google_maps_url": f"https://www.google.com/maps/place/?q=place_id:{top.get('place_id','')}",
                "resolved_country_code": cc or "",
            })
        except Exception as e:
            print(f"[WARN] Places failed for {name} | {e}")
        time.sleep(max(0, sleep_ms) / 1000.0)

    return out


# ─────────────────────────────────────────────────────────────
# 저장
# ─────────────────────────────────────────────────────────────
def write_csv(rows: List[Dict[str, Any]], path: str):
    cols = list(rows[0].keys())
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def write_geojson(rows: List[Dict[str, Any]], path: str):
    feats = []
    for r in rows:
        if r.get("lat") is None or r.get("lng") is None:
            continue
        props = dict(r)
        feats.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [r["lng"], r["lat"]]},
            "properties": props
        })
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "features": feats}, f, ensure_ascii=False, indent=2)


# ─────────────────────────────────────────────────────────────
# 메인
# ─────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--country", required=True)                 # 한국어 국가 (예: 일본)
    ap.add_argument("--area", default="")                       # 한국어 지역/도시 (예: 도쿄 시부야) - 비워도 OK
    ap.add_argument("--query", required=True)                   # 네이버 검색어
    ap.add_argument("--extra_hints", default="")                # 쉼표구분 힌트
    ap.add_argument("--include_query_as_hint", action="store_true")
    ap.add_argument("--hint_mode", default="query")             # reserved
    ap.add_argument("--language", default="ko")
    ap.add_argument("--radius_m", type=int, default=10000)      # 10km
    ap.add_argument("--max_posts", type=int, default=30)
    ap.add_argument("--max_candidates", type=int, default=150)
    ap.add_argument("--sleep_ms", type=int, default=300)
    ap.add_argument("--out_dir", default="/tmp")
    ap.add_argument("--out_name", default="result")
    ap.add_argument("--no_cache", action="store_true")          # 현재 캐시 미사용
    ap.add_argument("--log_urls", action="store_true")          # 블로그 URL 로그 저장
    ap.add_argument("--include_types", default="")              # 쉼표구분 e.g. restaurant,food
    ap.add_argument("--exclude_types", default="")              # 쉼표구분 e.g. tourist_attraction
    args = ap.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
    csv_path = os.path.join(args.out_dir, f"{args.out_name}.csv")
    geo_path = os.path.join(args.out_dir, f"{args.out_name}.geojson")
    log_path = os.path.join(args.out_dir, "crawled_urls.csv")

    print(f"[ENV] NAVER_CLIENT_ID loaded: {bool(NAVER_CLIENT_ID)}")
    print(f"[ENV] NAVER_CLIENT_SECRET loaded: {bool(NAVER_CLIENT_SECRET)}")
    print(f"[ENV] GOOGLE_PLACES_API_KEY loaded: {bool(GOOGLE_PLACES_API_KEY)}")

    # 0) 네이버 블로그 검색
    total_fetch = min(50, max(1, int(args.max_posts)))
    blog_items = naver_blog_search(args.query, total=total_fetch)
    urls = [normalize_naver_blog_url(it["link"]) for it in blog_items]
    print(f"[INFO] fetched {len(urls)} blog urls")

    # 1) 지오코딩: 중심좌표 + 국가코드
    lat, lng, country_code = geocode_area_country(args.area, args.country)
    loc_bias = (lat, lng) if (lat is not None and lng is not None) else None
    if not country_code:
        # 사용자가 명확히 "일본" 등 국가를 넣었다면, 최소 방어용 강제 코드 설정 가능
        # 국가명이 일본인 경우 등, 간단 매핑 예시:
        if "일본" in args.country:
            country_code = "JP"
        elif "필리핀" in args.country:
            country_code = "PH"
        elif "태국" in args.country:
            country_code = "TH"
        elif "프랑스" in args.country:
            country_code = "FR"
        elif "미국" in args.country:
            country_code = "US"
    print(f"[GEO] {args.area or args.country} -> center={loc_bias}, country={country_code}")

    # 2) 힌트 구성 (검색어 토큰 + extra_hints)
    hints = []
    if args.include_query_as_hint:
        hints.extend(tokenize_query_for_hints(args.query))
    if args.extra_hints:
        hints.extend([h.strip() for h in args.extra_hints.split(",") if h.strip()])
    # 중복 제거
    seen = set()
    eff_hints = []
    for h in hints:
        hl = h.lower()
        if hl not in seen:
            seen.add(hl)
            eff_hints.append(h)
    print(f"[HINTS] {eff_hints}")

    # 3) 블로그별 처리
    all_rows: List[Dict[str, Any]] = []
    url_logs: List[Dict[str, Any]] = []
    for url in urls:
        print(f"[INFO] Fetching: {url}")
        try:
            html = fetch_html(url)
        except Exception as e:
            print(f"[WARN] Fetch failed: {url} | {e}")
            continue

        text = extract_main_text(html)
        cands = extract_candidate_pois(text, eff_hints, top_k=args.max_candidates)
        print(f"[INFO] Extracted {len(cands)} candidates from this post")

        rows = resolve_candidates_to_places(
            cands, args.area or "", args.country or "",
            language=args.language or "ko",
            loc_bias=loc_bias, radius_m=args.radius_m, sleep_ms=args.sleep_ms,
            target_country_code=country_code, require_area_in_address=True,
            include_types=args.include_types, exclude_types=args.exclude_types
        )

        # 블로그별 source_url 부여
        for r in rows:
            r["source_url"] = url

        all_rows.extend(rows)
        url_logs.append({"blog_url": url, "used_place_count": len(rows)})

    # 4) dedupe (place_id 기준) + 모든 출처(source_urls) 보존
    by_place: Dict[str, Dict[str, Any]] = {}
    for r in all_rows:
        key = r.get("place_id") or f"{r.get('resolved_name')}@{r.get('formatted_address')}"
        if not key:
            continue
        entry = by_place.get(key)
        if not entry:
            r["source_urls"] = {r.get("source_url")} if r.get("source_url") else set()
            by_place[key] = r
        else:
            # 리뷰 수가 더 큰 쪽을 대표로 유지
            if (r.get("user_ratings_total") or 0) > (entry.get("user_ratings_total") or 0):
                r["source_urls"] = entry.get("source_urls", set())
                if r.get("source_url"):
                    r["source_urls"].add(r["source_url"])
                by_place[key] = r
            else:
                if r.get("source_url"):
                    entry.setdefault("source_urls", set()).add(r["source_url"])

    # set → 문자열 변환 및 대표 source_url 유지
    rows_final: List[Dict[str, Any]] = []
    for r in by_place.values():
        srcs = sorted(list(r.get("source_urls", set())))
        r["source_urls"] = ", ".join(srcs) if srcs else ""
        if not r.get("source_url") and srcs:
            r["source_url"] = srcs[0]
        rows_final.append(r)

    print(f"[INFO] Final places: {len(rows_final)}")

    # 5) 저장
    if rows_final:
        write_csv(rows_final, csv_path)
        write_geojson(rows_final, geo_path)
        print(f"[DONE] Wrote {csv_path} and {geo_path}")
    else:
        print("[DONE] No rows to write")

    # 6) 블로그 URL 로그 저장
    if args.log_urls:
        with open(log_path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=["blog_url", "used_place_count"])
            w.writeheader()
            for row in url_logs:
                w.writerow(row)
        print(f"[LOG] Wrote {log_path}")


if __name__ == "__main__":
    main()
