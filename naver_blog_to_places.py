# naver_blog_to_places.py
# -------------------------------------------------------------
# Google Places 단독 수집기 (네이버 사용 안 함)
# - 모드:
#   1) text: TextSearch 페이징
#   2) nearby_grid: 중심을 기준으로 그리드 포인트 생성 후 NearbySearch로 대량 수집
# - 업종(types) include/exclude, 최소 평점/리뷰 수
# - 영업 상태 필터(business_status), 현재 영업중만(open_now_only)
# - Place Details: 영업시간(현재/보조), 가격대, 전화, 웹사이트, Google URL, 편집 요약, UTC 오프셋
# - 파생: distance_km, popularity_score
# - 운영 기능: seen 캐시(증분), 캐시 리셋, 리뷰 스냅샷 CSV
# - dedupe: place_id 기준, 리뷰 수 큰 것 우선
# - 결과: CSV / GeoJSON (+ 리뷰 CSV 옵션)
# -------------------------------------------------------------

import os, re, json, time, csv, argparse, math, datetime
from typing import List, Tuple, Optional, Dict, Any, Iterable
import requests
from urllib.parse import urlencode
from slugify import slugify

GOOGLE_PLACES_API_KEY = os.getenv("GOOGLE_PLACES_API_KEY") or os.getenv("GOOGLE_MAPS_API_KEY")
UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.0 Mobile/15E148 Safari/604.1"

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
    "방콕": "bangkok",
}

# ─────────────────────────────────────────────────────────────
# Utils
# ─────────────────────────────────────────────────────────────
def ensure_google_key():
    if not GOOGLE_PLACES_API_KEY:
        raise RuntimeError("Google Places API 키가 없습니다. GOOGLE_PLACES_API_KEY 또는 GOOGLE_MAPS_API_KEY 확인")

def to_kor_label(types_list: List[str]) -> str:
    tset = set(types_list or [])
    for label, tps in KOR_TYPE_LABELS:
        if tset.intersection(tps):
            return label
    return "기타"

def price_text(level):
    mapping = {0: "무료/알수없음", 1: "저렴", 2: "보통", 3: "다소 비쌈", 4: "매우 비쌈"}
    return mapping.get(level, "")

def haversine(lat1, lng1, lat2, lng2):
    R = 6371.0
    dlat = math.radians((lat2 or 0) - (lat1 or 0))
    dlng = math.radians((lng2 or 0) - (lng1 or 0))
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1 or 0))*math.cos(math.radians(lat2 or 0))*math.sin(dlng/2)**2
    return 2*R*math.asin(math.sqrt(max(a, 0.0)))

def km_to_deg_lat(km: float) -> float:
    return km / 110.574

def km_to_deg_lng(km: float, lat_deg: float) -> float:
    return km / (111.320 * math.cos(math.radians(lat_deg)) + 1e-9)

def make_grid(center_lat: float, center_lng: float, radius_m: int, steps: int) -> Iterable[Tuple[float, float]]:
    if steps < 1:
        steps = 1
    half = steps
    lat_step_km = (radius_m / 1000.0) / steps
    lng_step_km = lat_step_km
    lat_deg_step = km_to_deg_lat(lat_step_km)
    lng_deg_step = km_to_deg_lng(lng_step_km, center_lat)
    for dy in range(-half, half + 1):
        for dx in range(-half, half + 1):
            yield center_lat + dy * lat_deg_step, center_lng + dx * lng_deg_step

def normalize_address(fmt: str, area: str) -> bool:
    if not area:
        return True
    lowered = (fmt or "").lower()
    toks = [t for t in re.split(r"\s+", area.strip()) if t]
    if any(tok.lower() in lowered for tok in toks):
        return True
    if any(AREA_ALIAS.get(tok, "").lower() in lowered for tok in toks):
        return True
    return False

# ─────────────────────────────────────────────────────────────
# Geocode & Places API
# ─────────────────────────────────────────────────────────────
def geocode_area_country(area: str, country: str) -> Tuple[Optional[float], Optional[float], Optional[str]]:
    ensure_google_key()
    target = (f"{area}, {country}" if area else country).strip(", ")
    base = "https://maps.googleapis.com/maps/api/geocode/json"
    url = f"{base}?{urlencode({'address': target, 'key': GOOGLE_PLACES_API_KEY, 'language':'ko'})}"
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
            cc = comp.get("short_name")
            break
    return lat, lng, cc

def _get(url: str, params: Dict[str, Any]) -> Dict[str, Any]:
    r = requests.get(url, params=params, headers={"User-Agent": UA}, timeout=25)
    r.raise_for_status()
    return r.json()

def text_search(params: Dict[str, Any]) -> Dict[str, Any]:
    ensure_google_key()
    return _get("https://maps.googleapis.com/maps/api/place/textsearch/json", params)

def nearby_search(params: Dict[str, Any]) -> Dict[str, Any]:
    ensure_google_key()
    return _get("https://maps.googleapis.com/maps/api/place/nearbysearch/json", params)

def place_details(place_id: str, want_reviews: bool = False) -> Dict[str, Any]:
    ensure_google_key()
    fields = [
        "name","place_id","formatted_address","geometry/location","types",
        "rating","user_ratings_total","price_level","business_status",
        "current_opening_hours/weekday_text","current_opening_hours/open_now",
        "secondary_opening_hours/weekday_text",
        "formatted_phone_number","international_phone_number",
        "website","url","editorial_summary","utc_offset_minutes",
        "primary_type","plus_code"
    ]
    if want_reviews:
        fields.append("reviews")
    params = {"place_id": place_id, "key": GOOGLE_PLACES_API_KEY, "language": "ko", "fields": ",".join(fields)}
    return _get("https://maps.googleapis.com/maps/api/place/details/json", params)

# ─────────────────────────────────────────────────────────────
# Collectors
# ─────────────────────────────────────────────────────────────
def collect_textsearch(query: str, area: str, country: str, language: str,
                       loc_bias, radius_m: int, pages: int,
                       include_types: str, exclude_types: str,
                       min_rating: float, min_reviews: int,
                       target_country_code: Optional[str],
                       sleep_ms: int, max_results: int,
                       open_now_only: bool, biz_filter: Optional[str],
                       skip_seen: bool, seen_ids: set) -> List[Dict[str, Any]]:
    out = []
    include_set = set((include_types or "").split(",")) - {""}
    exclude_set = set((exclude_types or "").split(",")) - {""}

    params = {"query": " ".join(x for x in [query, area, country] if x).strip(),
              "key": GOOGLE_PLACES_API_KEY, "language": language or "ko"}
    if loc_bias and radius_m:
        lat, lng = loc_bias
        params["location"] = f"{lat},{lng}"
        params["radius"] = str(radius_m)
    if open_now_only:
        params["opennow"] = "true"

    page_token = None
    for _ in range(max(1, pages)):
        if page_token:
            params["pagetoken"] = page_token
            time.sleep(2.0)

        js = text_search(params)
        results = js.get("results", [])
        for top in results:
            pid = top.get("place_id", "")
            if skip_seen and pid in seen_ids:
                continue

            fmt = top.get("formatted_address", "") or ""
            if not normalize_address(fmt, area):
                continue

            place_types = top.get("types", []) or []
            tset = set(place_types)
            if include_set and not tset.intersection(include_set):
                continue
            if exclude_set and tset.intersection(exclude_set):
                continue

            rating  = top.get("rating") or 0.0
            reviews = top.get("user_ratings_total") or 0
            if rating < min_rating or reviews < min_reviews:
                continue

            cc = get_country_code_safe(pid)
            if target_country_code and cc != target_country_code:
                continue

            # business_status는 Details에 더 신뢰성 있게 있으나, TextSearch에도 있을 수 있음
            biz_status = top.get("business_status", "")
            if biz_filter and biz_filter != "무관" and biz_filter != biz_status:
                # TextSearch에서 비어 있으면 Details에서 다시 거르므로 일단 통과시켜도 됨.
                pass

            out.append({
                "resolved_name": top.get("name", ""),
                "formatted_address": fmt,
                "place_id": pid,
                "rating": rating,
                "user_ratings_total": reviews,
                "types": ",".join(place_types),
                "업종": to_kor_label(place_types),
                "lat": top.get("geometry", {}).get("location", {}).get("lat"),
                "lng": top.get("geometry", {}).get("location", {}).get("lng"),
                "resolved_country_code": cc or "",
                "business_status": biz_status or "",
            })
            if len(out) >= max_results:
                break
        if len(out) >= max_results:
            break

        page_token = js.get("next_page_token")
        if not page_token:
            break
        time.sleep(max(0, sleep_ms) / 1000.0)
    return out


def collect_nearby_grid(area_lat: float, area_lng: float, area: str, country: str, language: str,
                        radius_m: int, grid_steps: int,
                        include_types: str, exclude_types: str,
                        min_rating: float, min_reviews: int,
                        target_country_code: Optional[str],
                        sleep_ms: int, max_results: int,
                        open_now_only: bool, biz_filter: Optional[str],
                        skip_seen: bool, seen_ids: set) -> List[Dict[str, Any]]:
    out = []
    include_list = [t for t in (include_types or "").split(",") if t]
    exclude_set = set((exclude_types or "").split(",")) - {""}

    if not include_list:
        include_list = ["restaurant"]  # 안전 기본값

    for lat, lng in make_grid(area_lat, area_lng, radius_m, grid_steps):
        for place_type in include_list:
            params = {
                "location": f"{lat},{lng}",
                "radius": str(max(1500, radius_m // max(1, grid_steps))),  # 포인트 반경
                "type": place_type,
                "key": GOOGLE_PLACES_API_KEY,
                "language": language or "ko",
            }
            if open_now_only:
                params["opennow"] = "true"

            page_token = None
            while True:
                if page_token:
                    params["pagetoken"] = page_token
                    time.sleep(2.0)
                js = nearby_search(params)
                results = js.get("results", [])
                for top in results:
                    pid = top.get("place_id", "")
                    if skip_seen and pid in seen_ids:
                        continue

                    fmt = top.get("vicinity") or top.get("formatted_address", "") or ""
                    if not normalize_address(fmt, area):
                        continue

                    place_types = top.get("types", []) or []
                    tset = set(place_types)
                    if exclude_set and tset.intersection(exclude_set):
                        continue

                    rating  = top.get("rating") or 0.0
                    reviews = top.get("user_ratings_total") or 0
                    if rating < min_rating or reviews < min_reviews:
                        continue

                    cc = get_country_code_safe(pid)
                    if target_country_code and cc != target_country_code:
                        continue

                    biz_status = top.get("business_status", "")
                    if biz_filter and biz_filter != "무관" and biz_filter != biz_status:
                        pass

                    out.append({
                        "resolved_name": top.get("name", ""),
                        "formatted_address": fmt,
                        "place_id": pid,
                        "rating": rating,
                        "user_ratings_total": reviews,
                        "types": ",".join(place_types),
                        "업종": to_kor_label(place_types),
                        "lat": top.get("geometry", {}).get("location", {}).get("lat"),
                        "lng": top.get("geometry", {}).get("location", {}).get("lng"),
                        "resolved_country_code": cc or "",
                        "business_status": biz_status or "",
                    })
                    if len(out) >= max_results:
                        return out

                page_token = js.get("next_page_token")
                if not page_token:
                    break
                time.sleep(max(0, sleep_ms) / 1000.0)
    return out

def get_country_code_safe(place_id: str) -> Optional[str]:
    try:
        det = place_details(place_id, want_reviews=False)
        for comp in det.get("result", {}).get("address_components", []):
            if "country" in comp.get("types", []):
                return comp.get("short_name")
        return None
    except Exception:
        return None

# ─────────────────────────────────────────────────────────────
# Details 확장(필수 정보 + 리뷰 스냅샷 옵션)
# ─────────────────────────────────────────────────────────────
def enrich_with_details(rows: List[Dict[str, Any]], sleep_ms: int,
                        center_lat: Optional[float], center_lng: Optional[float],
                        want_reviews: bool, reviews_csv_path: Optional[str]) -> List[Dict[str, Any]]:
    review_rows = []
    for r in rows:
        pid = r.get("place_id")
        if not pid:
            continue
        try:
            js = place_details(pid, want_reviews=want_reviews)
            res = js.get("result", {})

            # 정확한 business_status/price/opening/연락처/링크/요약/오프셋
            r["business_status"] = res.get("business_status", r.get("business_status",""))
            level = res.get("price_level", None)
            r["price_level"] = level if level is not None else ""
            r["price_level_text"] = price_text(level) if level is not None else ""
            coh = res.get("current_opening_hours", {})
            r["opening_open_now"] = coh.get("open_now", "")
            r["opening_weekday_text"] = "; ".join(coh.get("weekday_text", []) or [])
            soh = res.get("secondary_opening_hours", {})
            if isinstance(soh, list):
                # 보조 영업시간 배열 -> 한 줄 텍스트로
                merged = []
                for obj in soh:
                    merged += (obj.get("weekday_text", []) or [])
                r["opening_secondary_weekday_text"] = "; ".join(merged)
            else:
                r["opening_secondary_weekday_text"] = ""
            r["phone"] = res.get("formatted_phone_number", "") or res.get("international_phone_number", "")
            r["website"] = res.get("website", "")
            r["google_maps_url"] = res.get("url", f"https://www.google.com/maps/place/?q=place_id:{pid}")
            r["editorial_summary"] = (res.get("editorial_summary", {}) or {}).get("overview", "")
            r["utc_offset_minutes"] = res.get("utc_offset_minutes", "")

            # 대표 타입(있으면)
            r["primary_type"] = res.get("primary_type", "")

            # 파생 지표
            if center_lat is not None and center_lng is not None and r.get("lat") is not None and r.get("lng") is not None:
                r["distance_km"] = round(haversine(center_lat, center_lng, r["lat"], r["lng"]), 2)
            else:
                r["distance_km"] = ""
            rating = r.get("rating") or 0.0
            reviews = r.get("user_ratings_total") or 0
            r["popularity_score"] = round(rating * math.log1p(reviews), 2)

            # 리뷰 스냅샷 (최대 3개)
            if want_reviews and res.get("reviews"):
                for rv in res["reviews"][:3]:
                    review_rows.append({
                        "place_id": pid,
                        "author": rv.get("author_name"),
                        "rating": rv.get("rating"),
                        "relative_time": rv.get("relative_time_description"),
                        "text": rv.get("text"),
                        "lang": rv.get("language"),
                    })
        except Exception:
            # 세부 정보 실패해도 넘어감
            pass
        time.sleep(max(0, sleep_ms) / 1000.0)

    if want_reviews and reviews_csv_path and review_rows:
        # 리뷰 CSV 저장
        cols = sorted(set().union(*[set(r.keys()) for r in review_rows]))
        with open(reviews_csv_path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            for row in review_rows:
                w.writerow(row)

    return rows

# ─────────────────────────────────────────────────────────────
# Save
# ─────────────────────────────────────────────────────────────
def write_csv(rows: List[Dict[str, Any]], path: str):
    cols = sorted(set().union(*[set(r.keys()) for r in rows]))
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
# Cache (증분 수집)
# ─────────────────────────────────────────────────────────────
def load_seen(path: str) -> set:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()

def save_seen(path: str, s: set):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(sorted(list(s)), f, ensure_ascii=False, indent=2)
    except Exception:
        pass

# ─────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="nearby_grid", choices=["text", "nearby_grid"])
    ap.add_argument("--country", required=True)
    ap.add_argument("--area", default="")
    ap.add_argument("--query", required=True)
    ap.add_argument("--language", default="ko")
    ap.add_argument("--radius_m", type=int, default=10000)
    ap.add_argument("--google_result_pages", type=int, default=4)
    ap.add_argument("--grid_steps", type=int, default=3)
    ap.add_argument("--sleep_ms", type=int, default=300)
    ap.add_argument("--include_types", default="")
    ap.add_argument("--exclude_types", default="")
    ap.add_argument("--min_rating", type=float, default=4.2)
    ap.add_argument("--min_reviews", type=int, default=100)
    ap.add_argument("--max_results", type=int, default=600)
    ap.add_argument("--details", action="store_true")
    ap.add_argument("--open_now_only", action="store_true")
    ap.add_argument("--business_status_filter", default="OPERATIONAL(영업중)")
    ap.add_argument("--skip_seen", action="store_true")
    ap.add_argument("--reset_seen", action="store_true")
    ap.add_argument("--save_reviews", action="store_true")
    ap.add_argument("--out_dir", default="/tmp")
    ap.add_argument("--out_name", default="result")
    args = ap.parse_args()

    ensure_google_key()
    os.makedirs(args.out_dir, exist_ok=True)
    csv_path = os.path.join(args.out_dir, f"{args.out_name}.csv")
    geo_path = os.path.join(args.out_dir, f"{args.out_name}.geojson")
    reviews_csv_path = os.path.join(args.out_dir, f"{args.out_name}_reviews.csv")

    # seen 캐시
    seen_path = os.path.join(args.out_dir, "seen_place_ids.json")
    if args.reset_seen and os.path.exists(seen_path):
        try:
            os.remove(seen_path)
        except Exception:
            pass
    seen_ids = load_seen(seen_path) if args.skip_seen else set()

    lat, lng, country_code = geocode_area_country(args.area, args.country)
    loc_bias = (lat, lng) if (lat is not None and lng is not None) else None
    if not country_code:
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
    print(f"[MODE] {args.mode}")
    print(f"[FILTER] rating≥{args.min_rating}, reviews≥{args.min_reviews}, open_now_only={args.open_now_only}, business_status={args.business_status_filter}")

    # 수집
    if args.mode == "text":
        rows = collect_textsearch(
            args.query, args.area or "", args.country or "", args.language,
            loc_bias, args.radius_m, args.google_result_pages,
            args.include_types, args.exclude_types,
            args.min_rating, args.min_reviews,
            country_code, args.sleep_ms, args.max_results,
            args.open_now_only, args.business_status_filter,
            args.skip_seen, seen_ids
        )
    else:
        if not loc_bias:
            raise RuntimeError("nearby_grid 모드는 중심 좌표가 필요합니다. (지오코딩 실패)")
        rows = collect_nearby_grid(
            loc_bias[0], loc_bias[1], args.area or "", args.country or "", args.language,
            args.radius_m, args.grid_steps,
            args.include_types, args.exclude_types,
            args.min_rating, args.min_reviews,
            country_code, args.sleep_ms, args.max_results,
            args.open_now_only, args.business_status_filter,
            args.skip_seen, seen_ids
        )

    # dedupe: place_id 기준, 리뷰 수 큰 것 우선
    by_place: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        pid = r.get("place_id")
        if not pid:
            key = f"{r.get('resolved_name')}@{r.get('formatted_address')}"
        else:
            key = pid
        if key not in by_place:
            by_place[key] = r
        else:
            if (r.get("user_ratings_total") or 0) > (by_place[key].get("user_ratings_total") or 0):
                by_place[key] = r
    rows_final = list(by_place.values())
    print(f"[INFO] uniques: {len(rows_final)} (before details)")

    # Details 확장 + 리뷰 스냅샷
    if args.details and rows_final:
        rows_final = enrich_with_details(
            rows_final, args.sleep_ms, loc_bias[0] if loc_bias else None, loc_bias[1] if loc_bias else None,
            want_reviews=args.save_reviews, reviews_csv_path=(reviews_csv_path if args.save_reviews else None)
        )

    # business_status 최종 필터 (Details 반영 후 엄격 적용)
    bf = args.business_status_filter
    if bf and bf != "무관":
        rows_final = [r for r in rows_final if (r.get("business_status") or "") == bf.split("(")[0]]

    print(f"[INFO] Final rows: {len(rows_final)}")

    # seen 캐시 갱신
    if args.skip_seen and rows_final:
        seen_ids.update([r.get("place_id") for r in rows_final if r.get("place_id")])
        save_seen(seen_path, seen_ids)
        print(f"[CACHE] seen_place_ids.json updated ({len(seen_ids)} ids)")

    if rows_final:
        write_csv(rows_final, csv_path)
        write_geojson(rows_final, geo_path)
        print(f"[DONE] Wrote {csv_path} and {geo_path}")
        if args.save_reviews and os.path.exists(reviews_csv_path):
            print(f"[DONE] Wrote {reviews_csv_path}")
    else:
        print("[DONE] No rows to write")

if __name__ == "__main__":
    main()
