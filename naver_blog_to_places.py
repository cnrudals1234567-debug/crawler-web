import os, re, json, time, csv
import argparse
from urllib.parse import urlencode
import requests
from bs4 import BeautifulSoup
from readability import Document
from slugify import slugify
from tqdm import tqdm

# ── Keys
NAVER_CLIENT_ID     = os.getenv("NAVER_CLIENT_ID")
NAVER_CLIENT_SECRET = os.getenv("NAVER_CLIENT_SECRET")
GOOGLE_PLACES_API_KEY = os.getenv("GOOGLE_PLACES_API_KEY") or os.getenv("GOOGLE_MAPS_API_KEY")

# ── Endpoints & UA
NAVER_BLOG_SEARCH_ENDPOINT = "https://openapi.naver.com/v1/search/blog.json"
USER_AGENT = "Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.0 Mobile/15E148 Safari/604.1"

# ── Heuristic hints
POI_HINTS = ["맛집","레스토랑","식당","카페","bar","펍","bistro","restaurant","cafe","brunch",
             "dessert","디저트","라멘","스시","파스타","스테이크","market","bakery","BBQ"]

def normalize_naver_blog_url(url: str) -> str:
    m = re.search(r"https?://blog\.naver\.com/([^/]+)/(\d+)", url)
    if m:
        bid, logno = m.group(1), m.group(2)
        return f"https://m.blog.naver.com/{bid}/{logno}"
    return url

def naver_blog_search(query: str, display: int = 30, start: int = 1):
    if not NAVER_CLIENT_ID or not NAVER_CLIENT_SECRET:
        raise RuntimeError("NAVER API 키가 없습니다. ENV: NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 확인")
    headers = {"X-Naver-Client-Id": NAVER_CLIENT_ID, "X-Naver-Client-Secret": NAVER_CLIENT_SECRET}
    params = {"query": query, "display": display, "start": start, "sort": "sim"}
    r = requests.get(NAVER_BLOG_SEARCH_ENDPOINT, headers=headers, params=params, timeout=20)
    r.raise_for_status()
    return r.json().get("items", [])

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

def extract_candidate_pois(text: str, top_k: int = 60):
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    cand = []
    for ln in lines:
        if any(h.lower() in ln.lower() for h in POI_HINTS):
            for m in re.findall(r"[A-Za-z0-9&\.\-’' ]{2,40}|[가-힣·&\.\-’' ]{2,40}", ln):
                nm = m.strip(" -—·'’\"")
                if len(nm) >= 2 and re.search(r"[가-힣A-Za-z]", nm) and not nm.isdigit():
                    cand.append(nm)
    # short title-like
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

def google_places_text_search(query: str, language=None, loc_bias=None, radius_m=None):
    if not GOOGLE_PLACES_API_KEY:
        raise RuntimeError("Google Places API 키가 없습니다. ENV: GOOGLE_PLACES_API_KEY 확인")
    base = "https://maps.googleapis.com/maps/api/place/textsearch/json"
    params = {"query": query, "key": GOOGLE_PLACES_API_KEY}
    if language: params["language"] = language
    if loc_bias and radius_m:
        lat, lng = loc_bias
        params["location"] = f"{lat},{lng}"
        params["radius"]   = str(radius_m)
    url = f"{base}?{urlencode(params)}"
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    return r.json()

def geocode_city_country(city: str, country: str):
    if not GOOGLE_PLACES_API_KEY:
        return None
    base = "https://maps.googleapis.com/maps/api/geocode/json"
    q = f"{city}, {country}".strip()
    url = f"{base}?{urlencode({'address': q, 'key': GOOGLE_PLACES_API_KEY})}"
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    js = r.json()
    if js.get("results"):
        loc = js["results"][0]["geometry"]["location"]
        return (loc["lat"], loc["lng"])
    return None

def resolve_candidates_to_places(candidates, city, country="", sleep_sec=0.6, language=None, loc_bias=None, radius_m=None):
    out = []
    for name in tqdm(candidates, desc="Resolving with Google Places"):
        try:
            q = f"{name} {city} {country}".strip()
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
        time.sleep(sleep_sec)
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

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", required=True)
    ap.add_argument("--city", required=True)
    ap.add_argument("--country", default="")
    ap.add_argument("--max_posts", type=int, default=15)
    ap.add_argument("--out_name", default=None)
    ap.add_argument("--out_dir", default="/tmp")
    ap.add_argument("--language", default=None)       # e.g., ko, ja, en
    ap.add_argument("--radius_m", type=int, default=30000)
    args = ap.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
    out_base = args.out_name or slugify(f"{args.city}-{args.query}")
    csv_path = os.path.join(args.out_dir, f"{out_base}.csv")
    geo_path = os.path.join(args.out_dir, f"{out_base}.geojson")

    print(f"[ENV] NAVER_CLIENT_ID loaded: {bool(NAVER_CLIENT_ID)}")
    print(f"[ENV] NAVER_CLIENT_SECRET loaded: {bool(NAVER_CLIENT_SECRET)}")
    print(f"[ENV] GOOGLE_PLACES_API_KEY loaded: {bool(GOOGLE_PLACES_API_KEY)}")
    print(f"[OUT] dir={args.out_dir} base={out_base}")
    print(f"[INFO] Searching Naver blogs: '{args.query}' (max_posts={args.max_posts})")

    # Naver search
    items = naver_blog_search(args.query, display=min(args.max_posts, 30), start=1)
    urls = [normalize_naver_blog_url(it["link"]) for it in items]

    # 도시 중심점 지오코딩 (위치편향)
    geo = geocode_city_country(args.city, args.country)
    print(f"[GEO] {args.city}, {args.country} -> {geo}")

    resolved = []
    for url in urls:
        print(f"[INFO] Fetching: {url}")
        try:
            html = fetch_html(url)
        except Exception as e:
            print(f"[WARN] Fetch failed: {url} | {e}")
            continue
        text = extract_main_text(html)
        cands = extract_candidate_pois(text, top_k=60)
        print(f"[INFO] Extracted {len(cands)} candidates from this post")
        rows = resolve_candidates_to_places(
            cands, args.city, args.country or "",
            language=args.language, loc_bias=geo, radius_m=args.radius_m
        )
        for r in rows: r["source_url"] = url
        resolved.extend(rows)

    # dedupe
    uniq = {}
    for r in resolved:
        k = r.get("place_id") or f"{r.get('resolved_name')}@{r.get('formatted_address')}"
        if k not in uniq or (r.get('user_ratings_total') or 0) > (uniq[k].get('user_ratings_total') or 0):
            uniq[k] = r
    rows_final = list(uniq.values())

    write_csv(rows_final, csv_path)
    write_geojson(rows_final, geo_path)
    print(f"[DONE] Wrote {csv_path} and {geo_path}")

if __name__ == "__main__":
    main()
import os, re, json, time, csv
import argparse
from urllib.parse import urlencode
import requests
from bs4 import BeautifulSoup
from readability import Document
from slugify import slugify
from tqdm import tqdm

# ── Keys
NAVER_CLIENT_ID     = os.getenv("NAVER_CLIENT_ID")
NAVER_CLIENT_SECRET = os.getenv("NAVER_CLIENT_SECRET")
GOOGLE_PLACES_API_KEY = os.getenv("GOOGLE_PLACES_API_KEY") or os.getenv("GOOGLE_MAPS_API_KEY")

# ── Endpoints & UA
NAVER_BLOG_SEARCH_ENDPOINT = "https://openapi.naver.com/v1/search/blog.json"
USER_AGENT = "Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.0 Mobile/15E148 Safari/604.1"

# ── Heuristic hints
POI_HINTS = ["맛집","레스토랑","식당","카페","bar","펍","bistro","restaurant","cafe","brunch",
             "dessert","디저트","라멘","스시","파스타","스테이크","market","bakery","BBQ"]

def normalize_naver_blog_url(url: str) -> str:
    m = re.search(r"https?://blog\.naver\.com/([^/]+)/(\d+)", url)
    if m:
        bid, logno = m.group(1), m.group(2)
        return f"https://m.blog.naver.com/{bid}/{logno}"
    return url

def naver_blog_search(query: str, display: int = 30, start: int = 1):
    if not NAVER_CLIENT_ID or not NAVER_CLIENT_SECRET:
        raise RuntimeError("NAVER API 키가 없습니다. ENV: NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 확인")
    headers = {"X-Naver-Client-Id": NAVER_CLIENT_ID, "X-Naver-Client-Secret": NAVER_CLIENT_SECRET}
    params = {"query": query, "display": display, "start": start, "sort": "sim"}
    r = requests.get(NAVER_BLOG_SEARCH_ENDPOINT, headers=headers, params=params, timeout=20)
    r.raise_for_status()
    return r.json().get("items", [])

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

def extract_candidate_pois(text: str, top_k: int = 60):
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    cand = []
    for ln in lines:
        if any(h.lower() in ln.lower() for h in POI_HINTS):
            for m in re.findall(r"[A-Za-z0-9&\.\-’' ]{2,40}|[가-힣·&\.\-’' ]{2,40}", ln):
                nm = m.strip(" -—·'’\"")
                if len(nm) >= 2 and re.search(r"[가-힣A-Za-z]", nm) and not nm.isdigit():
                    cand.append(nm)
    # short title-like
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

def google_places_text_search(query: str, language=None, loc_bias=None, radius_m=None):
    if not GOOGLE_PLACES_API_KEY:
        raise RuntimeError("Google Places API 키가 없습니다. ENV: GOOGLE_PLACES_API_KEY 확인")
    base = "https://maps.googleapis.com/maps/api/place/textsearch/json"
    params = {"query": query, "key": GOOGLE_PLACES_API_KEY}
    if language: params["language"] = language
    if loc_bias and radius_m:
        lat, lng = loc_bias
        params["location"] = f"{lat},{lng}"
        params["radius"]   = str(radius_m)
    url = f"{base}?{urlencode(params)}"
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    return r.json()

def geocode_city_country(city: str, country: str):
    if not GOOGLE_PLACES_API_KEY:
        return None
    base = "https://maps.googleapis.com/maps/api/geocode/json"
    q = f"{city}, {country}".strip()
    url = f"{base}?{urlencode({'address': q, 'key': GOOGLE_PLACES_API_KEY})}"
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    js = r.json()
    if js.get("results"):
        loc = js["results"][0]["geometry"]["location"]
        return (loc["lat"], loc["lng"])
    return None

def resolve_candidates_to_places(candidates, city, country="", sleep_sec=0.6, language=None, loc_bias=None, radius_m=None):
    out = []
    for name in tqdm(candidates, desc="Resolving with Google Places"):
        try:
            q = f"{name} {city} {country}".strip()
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
        time.sleep(sleep_sec)
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

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", required=True)
    ap.add_argument("--city", required=True)
    ap.add_argument("--country", default="")
    ap.add_argument("--max_posts", type=int, default=15)
    ap.add_argument("--out_name", default=None)
    ap.add_argument("--out_dir", default="/tmp")
    ap.add_argument("--language", default=None)       # e.g., ko, ja, en
    ap.add_argument("--radius_m", type=int, default=30000)
    args = ap.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
    out_base = args.out_name or slugify(f"{args.city}-{args.query}")
    csv_path = os.path.join(args.out_dir, f"{out_base}.csv")
    geo_path = os.path.join(args.out_dir, f"{out_base}.geojson")

    print(f"[ENV] NAVER_CLIENT_ID loaded: {bool(NAVER_CLIENT_ID)}")
    print(f"[ENV] NAVER_CLIENT_SECRET loaded: {bool(NAVER_CLIENT_SECRET)}")
    print(f"[ENV] GOOGLE_PLACES_API_KEY loaded: {bool(GOOGLE_PLACES_API_KEY)}")
    print(f"[OUT] dir={args.out_dir} base={out_base}")
    print(f"[INFO] Searching Naver blogs: '{args.query}' (max_posts={args.max_posts})")

    # Naver search
    items = naver_blog_search(args.query, display=min(args.max_posts, 30), start=1)
    urls = [normalize_naver_blog_url(it["link"]) for it in items]

    # 도시 중심점 지오코딩 (위치편향)
    geo = geocode_city_country(args.city, args.country)
    print(f"[GEO] {args.city}, {args.country} -> {geo}")

    resolved = []
    for url in urls:
        print(f"[INFO] Fetching: {url}")
        try:
            html = fetch_html(url)
        except Exception as e:
            print(f"[WARN] Fetch failed: {url} | {e}")
            continue
        text = extract_main_text(html)
        cands = extract_candidate_pois(text, top_k=60)
        print(f"[INFO] Extracted {len(cands)} candidates from this post")
        rows = resolve_candidates_to_places(
            cands, args.city, args.country or "",
            language=args.language, loc_bias=geo, radius_m=args.radius_m
        )
        for r in rows: r["source_url"] = url
        resolved.extend(rows)

    # dedupe
    uniq = {}
    for r in resolved:
        k = r.get("place_id") or f"{r.get('resolved_name')}@{r.get('formatted_address')}"
        if k not in uniq or (r.get('user_ratings_total') or 0) > (uniq[k].get('user_ratings_total') or 0):
            uniq[k] = r
    rows_final = list(uniq.values())

    write_csv(rows_final, csv_path)
    write_geojson(rows_final, geo_path)
    print(f"[DONE] Wrote {csv_path} and {geo_path}")

if __name__ == "__main__":
    main()
