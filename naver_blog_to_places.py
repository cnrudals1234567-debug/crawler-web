# naver_blog_to_places.py

import os, re, json, time, csv, argparse
import requests
from urllib.parse import urlencode
from bs4 import BeautifulSoup
from readability import Document
from slugify import slugify
from tqdm import tqdm

NAVER_CLIENT_ID = os.getenv("NAVER_CLIENT_ID")
NAVER_CLIENT_SECRET = os.getenv("NAVER_CLIENT_SECRET")
GOOGLE_PLACES_API_KEY = os.getenv("GOOGLE_PLACES_API_KEY")

def naver_blog_search(query, total=30):
    headers = {"X-Naver-Client-Id": NAVER_CLIENT_ID, "X-Naver-Client-Secret": NAVER_CLIENT_SECRET}
    endpoint = "https://openapi.naver.com/v1/search/blog.json"
    out, start, got = [], 1, 0
    while got < total and start <= 100:
        display = min(30, total - got)
        params = {"query": query, "display": display, "start": start, "sort": "sim"}
        r = requests.get(endpoint, headers=headers, params=params, timeout=20)
        r.raise_for_status()
        items = r.json().get("items", [])
        if not items: break
        out.extend(items)
        got += len(items); start += len(items)
    return out[:total]

def normalize_naver_blog_url(url):
    m = re.search(r"https?://blog\.naver\.com/([^/]+)/(\d+)", url)
    if m:
        return f"https://m.blog.naver.com/{m.group(1)}/{m.group(2)}"
    return url

def fetch_html(url):
    r = requests.get(url, headers={"User-Agent": "Mozilla"}, timeout=25)
    r.raise_for_status()
    return r.text

def extract_main_text(html):
    try:
        doc = Document(html)
        soup = BeautifulSoup(doc.summary(), "html.parser")
    except:
        soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script","style","noscript"]): tag.decompose()
    return soup.get_text("
", strip=True)

def tokenize_query_for_hints(q):
    return [re.sub(r"[^가-힣a-zA-Z0-9]", "", w) for w in re.split(r"[\s,]+", q or "") if len(w.strip()) >= 2]

def extract_candidate_pois(text, hints, top_k=60):
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    hints = [h.lower() for h in hints]
    cand = []
    for ln in lines:
        if any(h in ln.lower() for h in hints):
            for m in re.findall(r"[가-힣A-Za-z0-9&\.-’' ]{2,40}", ln):
                nm = m.strip(" -—·'’"")
                if len(nm) >= 2 and re.search(r"[가-힣A-Za-z]", nm) and not nm.isdigit():
                    cand.append(nm)
    uniq = []
    seen = set()
    for c in cand:
        k = c.lower()
        if k not in seen: seen.add(k); uniq.append(c)
    return uniq[:top_k]

def google_places_text_search(query, language="ko", loc_bias=None, radius_m=10000):
    base = "https://maps.googleapis.com/maps/api/place/textsearch/json"
    params = {"query": query, "key": GOOGLE_PLACES_API_KEY, "language": language}
    if loc_bias:
        params["location"] = f"{loc_bias[0]},{loc_bias[1]}"
        params["radius"] = str(radius_m)
    r = requests.get(base, params=params, timeout=25)
    r.raise_for_status()
    return r.json()

def get_place_country_code(place_id):
    base = "https://maps.googleapis.com/maps/api/place/details/json"
    params = {"place_id": place_id, "fields": "address_component", "key": GOOGLE_PLACES_API_KEY}
    r = requests.get(base, params=params, timeout=20)
    r.raise_for_status()
    js = r.json()
    for comp in js.get("result", {}).get("address_components", []):
        if "country" in comp.get("types", []):
            return comp.get("short_name")
    return None

def resolve_candidates(candidates, area, country, hints, language, loc_bias, radius_m, sleep_ms, country_code):
    out = []
    area_tokens = [t for t in re.split(r"[\s,]+", area.strip()) if t]
    for name in tqdm(candidates, desc="Places resolving"):
        try:
            query = f"{name} {area} {country}".strip()
            js = google_places_text_search(query, language, loc_bias, radius_m)
            results = js.get("results", [])
            if not results: continue
            top = results[0]
            addr = top.get("formatted_address", "")
            if not any(tok in addr for tok in area_tokens): continue
            cc = get_place_country_code(top["place_id"])
            if cc != country_code: continue
            out.append({
                "candidate_name": name,
                "resolved_name": top.get("name", ""),
                "formatted_address": addr,
                "place_id": top.get("place_id", ""),
                "rating": top.get("rating"),
                "user_ratings_total": top.get("user_ratings_total"),
                "lat": top.get("geometry", {}).get("location", {}).get("lat"),
                "lng": top.get("geometry", {}).get("location", {}).get("lng"),
                "google_maps_url": f"https://www.google.com/maps/place/?q=place_id:{top.get('place_id','')}",
                "resolved_country_code": cc
            })
        except Exception as e:
            print(f"[WARN] Failed {name} | {e}")
        time.sleep(sleep_ms / 1000)
    return out

def write_csv(rows, path):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=rows[0].keys())
        w.writeheader(); w.writerows(rows)

def write_geojson(rows, path):
    feats = []
    for r in rows:
        if not r.get("lat") or not r.get("lng"): continue
        feats.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [r["lng"], r["lat"]]},
            "properties": r
        })
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "features": feats}, f, ensure_ascii=False, indent=2)

def geocode_area(area, country):
    query = f"{area}, {country}" if area else country
    url = f"https://maps.googleapis.com/maps/api/geocode/json?address={query}&key={GOOGLE_PLACES_API_KEY}"
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    res = r.json()["results"][0]
    loc = res["geometry"]["location"]
    cc = ""
    for comp in res.get("address_components", []):
        if "country" in comp.get("types", []):
            cc = comp.get("short_name"); break
    return (loc["lat"], loc["lng"]), cc

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--country", required=True)
    ap.add_argument("--area", default="")
    ap.add_argument("--query", required=True)
    ap.add_argument("--extra_hints", default="")
    ap.add_argument("--include_query_as_hint", action="store_true")
    ap.add_argument("--hint_mode", default="query")
    ap.add_argument("--language", default="ko")
    ap.add_argument("--radius_m", type=int, default=10000)
    ap.add_argument("--max_posts", type=int, default=30)
    ap.add_argument("--max_candidates", type=int, default=150)
    ap.add_argument("--sleep_ms", type=int, default=300)
    ap.add_argument("--out_dir", default="/tmp")
    ap.add_argument("--out_name", default="result")
    ap.add_argument("--no_cache", action="store_true")
    ap.add_argument("--log_urls", action="store_true")
    args = ap.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
    out_base = slugify(f"{args.area or args.country}-{args.query}")
    csv_path = os.path.join(args.out_dir, f"{args.out_name}.csv")
    geo_path = os.path.join(args.out_dir, f"{args.out_name}.geojson")
    log_path = os.path.join(args.out_dir, "crawled_urls.csv")

    blog_items = naver_blog_search(args.query, total=args.max_posts)
    urls = [normalize_naver_blog_url(it["link"]) for it in blog_items]
    all_rows = []
    url_logs = []

    loc_bias, country_code = geocode_area(args.area, args.country)
    full_hints = tokenize_query_for_hints(args.query) + [h.strip() for h in args.extra_hints.split(",") if h.strip()]

    for url in urls:
        try:
            html = fetch_html(url)
            text = extract_main_text(html)
            cands = extract_candidate_pois(text, full_hints, top_k=args.max_candidates)
            rows = resolve_candidates(cands, args.area, args.country, full_hints,
                                      args.language, loc_bias, args.radius_m, args.sleep_ms, country_code)
            for r in rows:
                r["source_url"] = url
            all_rows.extend(rows)
            url_logs.append({"blog_url": url, "used_place_count": len(rows)})
        except Exception as e:
            print(f"[ERR] {url} - {e}")

    uniq = {}
    for r in all_rows:
        key = r.get("place_id") or r.get("resolved_name")
        if key and key not in uniq:
            uniq[key] = r
    rows_final = list(uniq.values())
    if rows_final: write_csv(rows_final, csv_path)
    if rows_final: write_geojson(rows_final, geo_path)
    if args.log_urls: write_csv(url_logs, log_path)

if __name__ == "__main__":
    main()