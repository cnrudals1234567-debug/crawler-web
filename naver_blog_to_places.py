# naver_blog_to_places.py — Fast 100-cap (robust)
import os, re, json, time, csv, argparse, math, random
from typing import List, Tuple, Optional, Dict, Any, Set
import requests

API_KEY = os.getenv("GOOGLE_PLACES_API_KEY") or os.getenv("GOOGLE_MAPS_API_KEY")
UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.0 Mobile/15E148 Safari/604.1"

AREA_ALIAS = {
    "도쿄":"tokyo","시부야":"shibuya","신주쿠":"shinjuku","오사카":"osaka","교토":"kyoto",
    "삿포로":"sapporo","후쿠오카":"fukuoka","세부":"cebu","막탄":"mactan","방콕":"bangkok"
}

def ensure_key():
    if not API_KEY:
        raise RuntimeError("Google API 키가 없습니다. 환경변수 GOOGLE_PLACES_API_KEY 또는 GOOGLE_MAPS_API_KEY 설정 필요.")

def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA})
    return s

def _get_json(sess: requests.Session, url: str, params: Dict[str, Any], max_retries: int = 5) -> Dict[str, Any]:
    backoff = 1.0
    for _ in range(max_retries):
        r = sess.get(url, params=params, timeout=30)
        r.raise_for_status()
        js = r.json()
        status = js.get("status") or js.get("Status")
        if status in (None, "OK", "ZERO_RESULTS"):
            return js
        if status == "INVALID_REQUEST" and "pagetoken" in params:
            time.sleep(2.2); continue
        if status in ("OVER_QUERY_LIMIT", "RESOURCE_EXHAUSTED"):
            time.sleep(backoff + random.random())
            backoff = min(backoff * 2, 8); continue
        return js
    return js

def geocode(sess: requests.Session, area: str, country: str) -> Tuple[Optional[float], Optional[float]]:
    ensure_key()
    target = (f"{area}, {country}" if area else country).strip(", ")
    js = _get_json(sess, "https://maps.googleapis.com/maps/api/geocode/json",
                   {"address": target, "key": API_KEY, "language": "ko"})
    res = js.get("results") or []
    if not res: return None, None
    loc = res[0]["geometry"]["location"]
    return loc["lat"], loc["lng"]

def area_pass(fmt: str, area: str, mode: str) -> bool:
    if mode == "none": return True
    if not area: return True
    low = (fmt or "").lower()
    toks = [t for t in re.split(r"\s+", area.strip()) if t]
    if any(t.lower() in low for t in toks): return True
    if any(AREA_ALIAS.get(t, "").lower() in low for t in toks): return True
    return False

def km_to_deg_lat(km): return km/110.574
def km_to_deg_lng(km, lat): return km/(111.320*math.cos(math.radians(lat))+1e-9)

def grid(center_lat, center_lng, radius_m, steps):
    if steps < 1: steps = 1
    half = steps
    step_km = (radius_m/1000)/steps
    dlat = km_to_deg_lat(step_km); dlng = km_to_deg_lng(step_km, center_lat)
    for dy in range(-half, half+1):
        for dx in range(-half, half+1):
            yield center_lat + dy*dlat, center_lng + dx*dlng

BASE_FIELDS = [
    "name","formatted_address","place_id","rating","user_ratings_total","types",
    "lat","lng","business_status","google_maps_url","source_mode"
]

def normalize_place(p: Dict[str, Any], source_mode: str) -> Dict[str, Any]:
    loc = (p.get("geometry",{}) or {}).get("location",{}) or {}
    fmt = p.get("formatted_address") or p.get("vicinity") or ""
    types = ",".join(p.get("types",[]) or [])
    return {
        "name": p.get("name",""),
        "formatted_address": fmt,
        "place_id": p.get("place_id",""),
        "rating": p.get("rating") or 0.0,
        "user_ratings_total": p.get("user_ratings_total") or 0,
        "types": types,
        "lat": loc.get("lat"),
        "lng": loc.get("lng"),
        "business_status": p.get("business_status",""),
        "google_maps_url": f"https://www.google.com/maps/place/?q=place_id:{p.get('place_id','')}",
        "source_mode": source_mode,
    }

def text_search(sess, query, area, country, language, loc, radius_m,
                pages, include_types, min_rating, min_reviews, sleep_ms, max_results, area_filter,
                seen: Set[str]) -> List[Dict[str, Any]]:
    ensure_key()
    params = {"query": " ".join(x for x in [query, area, country] if x).strip(),
              "key": API_KEY, "language": language}
    if loc and radius_m:
        params["location"] = f"{loc[0]},{loc[1]}"; params["radius"] = str(radius_m)
    out = []
    page_token = None
    for _ in range(max(1, pages)):
        if page_token:
            params["pagetoken"] = page_token; time.sleep(2.2)
        js = _get_json(sess, "https://maps.googleapis.com/maps/api/place/textsearch/json", params)
        for p in js.get("results", []) or []:
            r = normalize_place(p, "text")
            if not r["place_id"] or r["place_id"] in seen: continue
            if not area_pass(r["formatted_address"], area, area_filter): continue
            if include_types:
                tset = set((p.get("types") or []))
                if not tset.intersection(set(include_types.split(","))): continue
            if r["rating"] < min_rating or r["user_ratings_total"] < min_reviews: continue
            seen.add(r["place_id"]); out.append(r)
            if len(out) >= max_results: return out
        page_token = js.get("next_page_token")
        if not page_token: break
        time.sleep(max(0, sleep_ms)/1000.0)
    return out

def nearby(sess, center_lat, center_lng, area, language, radius_m, grid_steps,
           include_types, min_rating, min_reviews, sleep_ms, max_results, area_filter,
           seen: Set[str]) -> List[Dict[str, Any]]:
    ensure_key()
    include_list = [t for t in (include_types or "").split(",") if t] or ["restaurant"]
    out = []
    for lat, lng in grid(center_lat, center_lng, radius_m, grid_steps):
        for tp in include_list:
            params = {"location": f"{lat},{lng}", "radius": str(max(1500, radius_m//max(1,grid_steps))),
                      "type": tp, "key": API_KEY, "language": language}
            page_token = None
            while True:
                if page_token:
                    params["pagetoken"] = page_token; time.sleep(2.2)
                js = _get_json(sess, "https://maps.googleapis.com/maps/api/place/nearbysearch/json", params)
                for p in js.get("results", []) or []:
                    r = normalize_place(p, "nearby")
                    if not r["place_id"] or r["place_id"] in seen: continue
                    if not area_pass(r["formatted_address"], area, area_filter): continue
                    if r["rating"] < min_rating or r["user_ratings_total"] < min_reviews: continue
                    seen.add(r["place_id"]); out.append(r)
                    if len(out) >= max_results: return out
                page_token = js.get("next_page_token")
                if not page_token: break
                time.sleep(max(0, sleep_ms)/1000.0)
    return out

def details_enrich(sess, rows: List[Dict[str, Any]], sleep_ms: int) -> List[Dict[str, Any]]:
    fields = "name,place_id,website,formatted_phone_number,opening_hours/weekday_text,price_level,url"
    for r in rows:
        pid = r.get("place_id")
        if not pid: continue
        try:
            js = _get_json(sess, "https://maps.googleapis.com/maps/api/place/details/json",
                           {"place_id": pid, "key": API_KEY, "language": "ko", "fields": fields})
            res = js.get("result", {}) or {}
            r["website"] = res.get("website","")
            r["phone"] = res.get("formatted_phone_number","")
            r["opening_weekday_text"] = "; ".join((res.get("opening_hours",{}) or {}).get("weekday_text",[]) or [])
            r["price_level"] = res.get("price_level","")
            r["google_maps_url"] = res.get("url", r.get("google_maps_url"))
        except Exception:
            pass
        time.sleep(max(0, sleep_ms)/1000.0)
    return rows

FIXED_COLS = BASE_FIELDS + ["website","phone","opening_weekday_text","price_level"]

def write_csv(rows: List[Dict[str, Any]], path: str):
    extra = sorted(set().union(*[set(r.keys()) for r in rows]) - set(FIXED_COLS))
    cols = FIXED_COLS + extra
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols); w.writeheader()
        for r in rows: w.writerow({k: r.get(k, "") for k in cols})

def write_geojson(rows: List[Dict[str, Any]], path: str):
    feats = []
    for r in rows:
        lat, lng = r.get("lat"), r.get("lng")
        if lat is None or lng is None: continue
        props = {k: v for k, v in r.items() if k not in ("lat","lng")}
        feats.append({"type":"Feature","geometry":{"type":"Point","coordinates":[lng, lat]},"properties":props})
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"type":"FeatureCollection","features":feats}, f, ensure_ascii=False, indent=2)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["text","nearby"], default="text")
    ap.add_argument("--country", required=True)
    ap.add_argument("--area", default="")
    ap.add_argument("--query", required=True)
    ap.add_argument("--language", default="ko")
    ap.add_argument("--include_types", default="")
    ap.add_argument("--min_rating", type=float, default=3.8)
    ap.add_argument("--min_reviews", type=int, default=20)
    ap.add_argument("--sleep_ms", type=int, default=200)
    ap.add_argument("--google_result_pages", type=int, default=2)
    ap.add_argument("--radius_m", type=int, default=3000)
    ap.add_argument("--grid_steps", type=int, default=2)
    ap.add_argument("--area_filter", choices=["loose","none"], default="none")
    ap.add_argument("--details", action="store_true")
    ap.add_argument("--max_results", type=int, default=100)
    ap.add_argument("--out_dir", default="/tmp")
    ap.add_argument("--out_name", default="result")
    args = ap.parse_args()

    ensure_key()
    os.makedirs(args.out_dir, exist_ok=True)
    csv_path = os.path.join(args.out_dir, f"{args.out_name}.csv")
    geo_path = os.path.join(args.out_dir, f"{args.out_name}.geojson")

    sess = _session()
    lat, lng = geocode(sess, args.area, args.country)
    loc = (lat, lng) if (lat is not None and lng is not None) else None
    print(f"[GEO] {args.area or args.country} → center={loc}")

    cap = min(100, max(1, args.max_results))
    seen: Set[str] = set()
    rows: List[Dict[str, Any]] = []

    if args.mode == "text":
        rows = text_search(sess, args.query, args.area, args.country, args.language,
                           loc, args.radius_m, max(1, args.google_result_pages),
                           args.include_types, args.min_rating, args.min_reviews,
                           args.sleep_ms, cap, args.area_filter, seen)
    else:
        if not loc: raise RuntimeError("Nearby 모드는 중심 좌표가 필요합니다. (지오코딩 실패)")
        rows = nearby(sess, loc[0], loc[1], args.area, args.language, args.radius_m,
                      max(1, args.grid_steps), args.include_types, args.min_rating,
                      args.min_reviews, args.sleep_ms, cap, args.area_filter, seen)

    rows = rows[:cap]
    print(f"[INFO] collected={len(rows)} (≤100)")

    if args.details and rows:
        rows = details_enrich(sess, rows, args.sleep_ms)

    if rows:
        write_csv(rows, csv_path); write_geojson(rows, geo_path)
        print(f"[DONE] Wrote {csv_path} & {geo_path}")
    else:
        print("[DONE] No rows to write")

if __name__ == "__main__":
    main()
