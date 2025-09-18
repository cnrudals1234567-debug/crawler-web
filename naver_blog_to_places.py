# naver_blog_to_places.py
# -------------------------------------------------------------
# Google Places 단독 수집기 (네이버 사용 안 함)
# - Geocoding으로 중심좌표/국가코드 → TextSearch 페이징 수집
# - 업종(types) include/exclude 필터
# - 최소 평점/최소 리뷰 수 필터
# - dedupe: place_id 기준
# - 결과: CSV / GeoJSON
# -------------------------------------------------------------

import os, re, json, time, csv, argparse
from typing import List, Tuple, Optional, Dict, Any
import requests
from urllib.parse import urlencode
from slugify import slugify

# ─────────────────────────────────────────────────────────────
# GOOGLE KEYS
# ─────────────────────────────────────────────────────────────
GOOGLE_PLACES_API_KEY = os.getenv("GOOGLE_PLACES_API_KEY") or os.getenv("GOOGLE_MAPS_API_KEY")
UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.0 Mobile/15E148 Safari/604.1"

# ─────────────────────────────────────────────────────────────
# 한글 업종 라벨 매핑
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

AREA_ALIAS = {
    "도쿄": "tokyo",
    "시부야": "shibuya",
    "신주쿠": "shinjuku",
    "오사카": "osaka",
    "교토": "kyoto",
    "삿포로": "sapporo",
    "후쿠오카": "fukuoka",
    "세부": "cebu",
    "막탄": "mactan",
}

def to_kor_label(types_list: List[str]) -> str:
    tset = set(types_list or [])
    for label, tps in KOR_TYPE_LABELS:
        if tset.intersection(tps):
            return label
    return "기타"

def ensure_google_key():
    if not GOOGLE_PLACES_API_KEY:
        raise RuntimeError("Google Places API 키가 없습니다. GOOGLE_PLACES_API_KEY 또는 GOOGLE_MAPS_API_KEY 확인")

# ─────────────────────────────────────────────────────────────
# Geocode: 중심좌표 & 국가코드
# ─────────────────────────────────────────────────────────────
def geocode_area_country(area: str, country: str) -> Tuple[Optional[float], Optional[float], Optional[str]]:
    ensure_google_key()
    target = (f"{area}, {country}" if area else country).strip(", ")
    base = "https://maps.googleapis.com/maps/api/geocode/json"
    url = f"{base}?{urlencode({'address': target, 'key': GOOGLE_PLACES_API_KEY, 'language': 'ko'})}"
    r = requests.get(url, timeout=20, headers={"User-Agent": UA})
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
            cc = comp.get("short_name")  # JP/US/FR...
            break
    return lat, lng, cc

# ─────────────────────────────────────────────────────────────
# Places API
# ─────────────────────────────────────────────────────────────
def google_places_text_search(params: Dict[str, Any]) -> Dict[str, Any]:
    ensure_google_key()
    base = "https://maps.googleapis.com/maps/api/place/textsearch/json"
    r = requests.get(base, params=params, timeout=25, headers={"User-Agent": UA})
    r.raise_for_status()
    return r.json()

def get_place_country_code(place_id: str) -> Optional[str]:
    ensure_google_key()
    base = "https://maps.googleapis.com/maps/api/place/details/json"
    params = {"place_id": place_id, "fields": "address_component", "key": GOOGLE_PLACES_API_KEY}
    r = requests.get(base, params=params, timeout=20, headers={"User-Agent": UA})
    r.raise_for_status()
    js = r.json()
    for comp in js.get("result", {}).get("address_components", []):
        if "country" in comp.get("types", []):
            return comp.get("short_name")
    return None

# ─────────────────────────────────────────────────────────────
# Google 전용 수집 (TextSearch 페이징)
# ─────────────────────────────────────────────────────────────
def google_discover_places(
    query: str, area: str, country: str, language: str, loc_bias, radius_m: int,
    include_types: str, exclude_types: str, min_rating: float, min_reviews: int,
    target_country_code: Optional[str], pages: int, sleep_ms: int
) -> List[Dict[str, Any]]:
    include_set = set((include_types or "").split(",")) - {""}
    exclude_set = set((exclude_types or "").split(",")) - {""}

    q = " ".join(x for x in [query, area, country] if x).strip()
    params = {
        "query": q,
        "key": GOOGLE_PLACES_API_KEY,
        "language": language or "ko",
    }
    if loc_bias and radius_m:
        lat, lng = loc_bias
        params["location"] = f"{lat},{lng}"
        params["radius"] = str(radius_m)

    out = []
    page_token = None

    for i in range(max(1, pages)):
        if page_token:
            params["pagetoken"] = page_token
            # Google 요구사항: next_page_token 활성화까지 약간 대기
            time.sleep(2.0)

        js = google_places_text_search(params)
        results = js.get("results", [])

        for top in results:
            fmt = top.get("formatted_address", "") or ""
            place_types = top.get("types", []) or []
            rating = top.get("rating") or 0.0
            reviews = top.get("user_ratings_total") or 0

            # [A] 업종 types 필터
            tset = set(place_types)
            if include_set and not tset.intersection(include_set):
                continue
            if exclude_set and tset.intersection(exclude_set):
                continue

            # [B] 평점/리뷰 수 필터
            if rating < min_rating:
                continue
            if reviews < min_reviews:
                continue

            # [C] 국가코드 엄격 필터
            cc = get_place_country_code(top.get("place_id", ""))
            if target_country_code and cc != target_country_code:
                continue

            # [D] 지역 토큰(한글/영문 alias) 주소 포함 여부 (느슨한 보조 필터)
            area_tokens = [t for t in re.split(r"\s+", (area or "").strip()) if t]
            if area_tokens:
                lowered = fmt.lower()
                if not any(tok.lower() in lowered for tok in area_tokens):
                    if not any(AREA_ALIAS.get(tok, "").lower() in lowered for tok in area_tokens):
                        # 지역명이 주소에 없으면 제외(원하면 완화 가능)
                        continue

            out.append({
                "candidate_name": "",  # Google-only라 비움
                "resolved_name": top.get("name", ""),
                "formatted_address": fmt,
                "place_id": top.get("place_id", ""),
                "rating": rating,
                "user_ratings_total": reviews,
                "types": ",".join(place_types),
                "업종": to_kor_label(place_types),
                "lat": top.get("geometry", {}).get("location", {}).get("lat"),
                "lng": top.get("geometry", {}).get("location", {}).get("lng"),
                "google_maps_url": f"https://www.google.com/maps/place/?q=place_id:{top.get('place_id','')}",
                "resolved_country_code": cc or "",
                "source_url": "",     # 블로그 출처 없음
                "source_urls": "",    # 블로그 출처 없음
            })

        page_token = js.get("next_page_token")
        if not page_token:
            break

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
    # 공통 입력
    ap.add_argument("--country", required=True)                 # 한국어 국가 (예: 일본)
    ap.add_argument("--area", default="")                       # 한국어 지역/도시 (예: 도쿄 시부야) - 비워도 OK
    ap.add_argument("--query", required=True)                   # 검색어 (예: 라멘 맛집)
    ap.add_argument("--language", default="ko")
    ap.add_argument("--radius_m", type=int, default=10000)
    ap.add_argument("--google_result_pages", type=int, default=3)
    ap.add_argument("--sleep_ms", type=int, default=300)
    ap.add_argument("--include_types", default="")              # 예: restaurant,food
    ap.add_argument("--exclude_types", default="")              # 예: tourist_attraction
    ap.add_argument("--min_rating", type=float, default=0.0)
    ap.add_argument("--min_reviews", type=int, default=0)
    ap.add_argument("--out_dir", default="/tmp")
    ap.add_argument("--out_name", default="result")

    # 모드 스위치(호환용): google_only만 사용
    ap.add_argument("--google_only", action="store_true")
    args = ap.parse_args()

    if not GOOGLE_PLACES_API_KEY:
        raise RuntimeError("Google API 키를 찾을 수 없습니다. GOOGLE_PLACES_API_KEY 또는 GOOGLE_MAPS_API_KEY를 설정해주세요.")

    os.makedirs(args.out_dir, exist_ok=True)
    csv_path = os.path.join(args.out_dir, f"{args.out_name}.csv")
    geo_path = os.path.join(args.out_dir, f"{args.out_name}.geojson")

    # 지오코딩으로 중심/국가코드 가져오기
    lat, lng, country_code = geocode_area_country(args.area, args.country)
    loc_bias = (lat, lng) if (lat is not None and lng is not None) else None
    if not country_code:
        # 한국어 국가명 간단 매핑(필요 시 확장)
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

    # Google-only 수집
    print("[MODE] Google-only discovery")
    rows = google_discover_places(
        args.query, args.area or "", args.country or "",
        language=args.language or "ko",
        loc_bias=loc_bias, radius_m=args.radius_m,
        include_types=args.include_types, exclude_types=args.exclude_types,
        min_rating=args.min_rating, min_reviews=args.min_reviews,
        target_country_code=country_code,
        pages=args.google_result_pages, sleep_ms=args.sleep_ms
    )

    # dedupe: place_id 기준
    by_place: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        key = r.get("place_id") or f"{r.get('resolved_name')}@{r.get('formatted_address')}"
        if key not in by_place:
            by_place[key] = r
        else:
            # 리뷰 수 큰 쪽을 대표로 유지
            if (r.get("user_ratings_total") or 0) > (by_place[key].get("user_ratings_total") or 0):
                by_place[key] = r

    rows_final = list(by_place.values())
    print(f"[INFO] Final places: {len(rows_final)}")

    if rows_final:
        write_csv(rows_final, csv_path)
        write_geojson(rows_final, geo_path)
        print(f"[DONE] Wrote {csv_path} and {geo_path}")
    else:
        print("[DONE] No rows to write")

if __name__ == "__main__":
    main()
