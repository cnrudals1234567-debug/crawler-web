# naver_blog_to_places.py — Fast 100-cap version
import os, re, json, time, csv, argparse, math
from typing import List, Tuple, Optional, Dict, Any, Iterable
import requests
from urllib.parse import urlencode
from slugify import slugify

API_KEY = os.getenv("GOOGLE_PLACES_API_KEY") or os.getenv("GOOGLE_MAPS_API_KEY")
UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.0 Mobile/15E148 Safari/604.1"

AREA_ALIAS = {"도쿄":"tokyo","시부야":"shibuya","신주쿠":"shinjuku","오사카":"osaka","교토":"kyoto","삿포로":"sapporo","후쿠오카":"fukuoka","세부":"cebu","막탄":"mactan","방콕":"bangkok"}

def ensure_key():
    if not API_KEY: raise RuntimeError("Google API 키가 없습니다.")

def _get(url, params):
    r = requests.get(url, params=params, headers={"User-Agent": UA}, timeout=20)
    r.raise_for_status()
    return r.json()

def geocode(area: str, country: str) -> Tuple[Optional[float], Optional[float]]:
    ensure_key()
    target = (f"{area}, {country}" if area else country).strip(", ")
    url = "https://maps.googleapis.com/maps/api/geocode/json"
    js = _get(url, {"address": target, "key": API_KEY, "language": "ko"})
    if not js.get("results"): return None, None
    loc = js["results"][0]["geometry"]["location"]
    return loc["lat"], loc["lng"]

def area_pass(fmt: str, area: str, mode: str) -> bool:
    if mode == "none": return True
    if not area: return True
    low = (fmt or "").lower()
    toks = [t for t in re.split(r"\s+", area.strip()) if t]
    if any(t.lower() in low for t in toks): return True
    if any(AREA_ALIAS.get(t,"").lower() in low for t in toks): return True
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

def text_search(query, area, country, language, loc, radius_m, pages, include_types, min_rating, min_reviews, sleep_ms, max_results, area_filter):
    ensure_key()
    params = {"query": " ".join(x for x in [query, area, country] if x).strip(),
              "key": API_KEY, "language": language}
    if loc and radius_m:
        params["location"] = f"{loc[0]},{loc[1]}"; params["radius"] = str(radius_m)

    out = []
    page_token = None
    for _ in range(max(1, pages)):
        if page_token:
            params["pagetoken"] = page_token; time.sleep(2.0)
        js = _get("https://maps.googleapis.com/maps/api/place/textsearch/json", params)
        results = js.get("results", [])
        for p in results:
            fmt = p.get("formatted_address", "") or ""
            if not area_pass(fmt, area, area_filter): continue
            if include_types:
                tset = set(p.get("types", []) or [])
                if not tset.intersection(set(include_types.split(","))): continue
            rating = p.get("rating") or 0.0
            reviews = p.get("user_ratings_total") or 0
            if rating < min_rating or reviews < min_reviews: continue
            out.append({
                "name": p.get("name",""),
                "formatted_address": fmt,
                "place_id": p.get("place_id",""),
                "rating": rating,
                "user_ratings_total": reviews,
                "types": ",".join(p.get("types",[]) or []),
                "lat": p.get("geometry",{}).get("location",{}).get("lat"),
                "lng": p.get("geometry",{}).get("location",{}).get("lng"),
                "google_maps_url": f"https://www.google.com/maps/place/?q=place_id:{p.get('place_id','')}",
            })
            if len(out) >= max_results: return out
        page_token = js.get("next_page_token")
        if not page_token: break
        time.sleep(max(0, sleep_ms)/1000.0)
    return out

def nearby(center_lat, center_lng, area, language, radius_m, grid_steps, include_types, min_rating, min_reviews, sleep_ms, max_results, area_filter):
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
                    params["pagetoken"] = page_token; time.sleep(2.0)
                js = _get("https://maps.googleapis.com/maps/api/place/nearbysearch/json", params)
                results = js.get("results", [])
                for p in results:
                    fmt = p.get("vicinity") or p.get("formatted_address","") or ""
                    if not area_pass(fmt, area, area_filter): continue
                    rating = p.get("rating") or 0.0
                    reviews = p.get("user_ratings_total") or 0
                    if rating < min_rating or reviews < min_reviews: continue
                    out.append({
                        "name": p.get("name",""),
                        "formatted_address": fmt,
                        "place_id": p.get("place_id",""),
                        "rating": rating,
                        "user_ratings_total": reviews,
                        "types": ",".join(p.get("types",[]) or []),
                        "lat": p.get("geometry",{}).get("location",{}).get("lat"),
                        "lng": p.get("geometry",{}).get("location",{}).get("lng"),
                        "google_maps_url": f"https://www.google.com/maps/place/?q=place_id:{p.get('place_id','')}",
                    })
                    if len(out) >= max_results: return out
                page_token = js.get("next_page_token")
                if not page_token: break
                time.sleep(max(0, sleep_ms)/1000.0)
    return out

def details_enrich(rows, sleep_ms):
    # 속도 위해 최소 필드만
    fields = "name,place_id,website,formatted_phone_number,opening_hours/weekday_text,price_level,url"
    for r in rows:
        pid = r.get("place_id"); 
        if not pid: continue
        try:
            js = _get("https://maps.googleapis.com/maps/api/place/details/json",
                      {"place_id": pid, "key": API_KEY, "language": "ko", "fields": fields})
            res = js.get("result", {})
            r["website"] = res.get("website","")
            r["phone"] = res.get("formatted_phone_number","")
            r["opening_weekday_text"] = "; ".join(res.get("opening_hours",{}).get("weekday_text",[]) or [])
            r["price_level"] = res.get("price_level","")
            r["google_maps_url"] = res.get("url", r.get("google_maps_url"))
        except Exception:
            pass
        time.sleep(max(0, sleep_ms)/1000.0)
    return rows

def write_csv(rows, path):
    cols = sorted(set().union(*[set(r.keys()) for r in rows]))
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols); w.writeheader()
        for r in rows: w.writerow(r)

def write_geojson(rows, path):
    feats = []
    for r in rows:
        if r.get("lat") is None or r.get("lng") is None: continue
        feats.append({"type":"Feature","geometry":{"type":"Point","coordinates":[r["lng"],r["lat"]]},"properties":r})
    with open(path,"w",encoding="utf-8") as f:
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
    ap.add_argument("--max_results", type=int, default=100)  # 하드 제한
    ap.add_argument("--out_dir", default="/tmp")
    ap.add_argument("--out_name", default="result")
    args = ap.parse_args()

    ensure_key()
    os.makedirs(args.out_dir, exist_ok=True)
    csv_path = os.path.join(args.out_dir, f"{args.out_name}.csv")
    geo_path = os.path.join(args.out_dir, f"{args.out_name}.geojson")

    lat, lng = geocode(args.area, args.country)
    loc = (lat, lng) if (lat is not None and lng is not None) else None
    print(f"[GEO] {args.area or args.country} → center={loc}")

    cap = min(100, max(1, args.max_results))  # 코드 내부 하드 캡
    if args.mode == "text":
        rows = text_search(args.query, args.area, args.country, args.language,
                           loc, args.radius_m, args.google_result_pages,
                           args.include_types, args.min_rating, args.min_reviews,
                           args.sleep_ms, cap, args.area_filter)
    else:
        if not loc: raise RuntimeError("Nearby 모드는 중심 좌표가 필요합니다.")
        rows = nearby(loc[0], loc[1], args.area, args.language, args.radius_m,
                      args.grid_steps, args.include_types, args.min_rating,
                      args.min_reviews, args.sleep_ms, cap, args.area_filter)

    # place_id 기준 간단 dedupe
    uniq = {}
    for r in rows:
        pid = r.get("place_id")
        if pid and pid not in uniq:
            uniq[pid] = r
    rows = list(uniq.values())[:cap]

    print(f"[INFO] collected={len(rows)} (≤100)")

    if args.details and rows:
        rows = details_enrich(rows, args.sleep_ms)

    if rows:
        write_csv(rows, csv_path); write_geojson(rows, geo_path)
        print(f"[DONE] Wrote {csv_path} & {geo_path}")
    else:
        print("[DONE] No rows to write")

if __name__ == "__main__":
    main()
