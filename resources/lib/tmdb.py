"""Optional TMDb lookups for fanart, landscape art, clear logos and missing details."""

import json
import re
import threading
import time
from urllib.parse import urlencode

from . import cache, common, perf

API_HOST = "api.themoviedb.org"
IMG = "https://image.tmdb.org/t/p/"
TTL = 14 * 24 * 3600
MISS_TTL = 24 * 3600
SEARCH_TTL = 24 * 3600
DISCOVER_TTL = 6 * 3600
# Discover results with fewer votes than this are mostly junk.
MIN_VOTES = 50
# Seconds the artwork queue must stay unchanged before the service fetches it (see fetch_pending).
PENDING_QUIET = 3

# Errors a lookup can end with; callers treat them all as "no TMDb data".
# (http.client errors are turned into OSError in _get.)
LOOKUP_ERRORS = (OSError, ValueError)

# details() results for this plugin run, filled by prefetch so building items needs no more lookups.
_memo = {}
# One kept-alive HTTPS connection per thread: a new TLS handshake costs 0.2-1 s, sometimes far more.
_local = threading.local()


def _key():
    return common.setting("tmdb_key").strip()


def enabled():
    return bool(_key())


def _language():
    return common.setting("tmdb_language", "en-GB").strip() or "en-GB"


def _get(path, params):
    key = _key()
    headers = {"Accept": "application/json"}
    query = dict(params)
    # A v4 read access token is a long JWT; a v3 key is a 32 character hex string.
    if len(key) > 40 and "." in key:
        headers["Authorization"] = "Bearer {}".format(key)
    else:
        query["api_key"] = key
    url = "/3/{}?{}".format(path, urlencode(query))
    import http.client

    with perf.span("tmdb_http"):
        for attempt in (1, 2):
            conn = getattr(_local, "conn", None)
            if conn is None:
                conn = _local.conn = http.client.HTTPSConnection(API_HOST, timeout=10)
            try:
                conn.request("GET", url, headers=headers)
                resp = conn.getresponse()
                body = resp.read()
            except (OSError, http.client.HTTPException) as exc:
                conn.close()
                _local.conn = None
                # A kept-alive connection the server has since closed: retry once on a new one.
                if attempt == 2:
                    raise OSError(str(exc) or exc.__class__.__name__)
                continue
            if resp.status != 200:
                raise OSError("HTTP {}".format(resp.status))
            return json.loads(body.decode("utf-8"))


def _image(path, size):
    return "{}{}{}".format(IMG, size, path) if path else ""


# Image sizes offered per art type (settings image_<type> pick by index), smallest first.
# Kodi downloads, decodes and caches every image at the size asked for, so smaller is faster
# on a slow TV; fanart and landscape share one size so a backdrop used for both is fetched once.
IMAGE_SIZES = {
    "poster": ("w185", "w342", "w500", "w780", "original"),
    "backdrop": ("w780", "w1280", "original"),
    "clearlogo": ("w300", "w500", "original"),
    "still": ("w300", "w780", "original"),
}
# Suits a 1080p screen: posters fill a row card, backdrops the whole screen.
DEFAULT_IMAGE_SIZE = {"poster": "w342", "backdrop": "w1280", "clearlogo": "w500", "still": "w780"}
_IMG_SIZE = re.compile(r"^(https?://image\.tmdb\.org/t/p/)[^/]+/")
# Some TMDb logos are SVGs, which Kodi can't decode; TMDb renders any image as PNG on request.
_SVG = re.compile(r"\.svg$", re.IGNORECASE)


def image_size(art_type):
    """The TMDb size picked in settings for 'poster', 'backdrop', 'clearlogo' or 'still'."""
    sizes = IMAGE_SIZES[art_type]
    default = DEFAULT_IMAGE_SIZE[art_type]
    index = common.setting_int("image_" + art_type, sizes.index(default))
    return sizes[index] if 0 <= index < len(sizes) else default


def sized(url, size):
    """A TMDb image URL at another size, an SVG as its PNG; any other URL as it is.

    URLs stored with an older size (tally.db posters, cached details) all come out the same, so
    one item always has one URL and Kodi's texture cache keeps hitting.
    """
    if not url or not _IMG_SIZE.match(url):
        return url
    return _SVG.sub(".png", _IMG_SIZE.sub(r"\g<1>{}/".format(size), url))


def art_url(url, art_type):
    """``url`` at the size picked in settings for ``art_type``."""
    return sized(url, image_size(art_type))


def _pick_logo(logos, language):
    lang = language.split("-")[0]
    for wanted in (lang, "en", None):
        for logo in logos:
            if logo.get("iso_639_1") == wanted and logo.get("file_path"):
                return logo["file_path"]
    return logos[0]["file_path"] if logos else None


def _pick_landscape(backdrops, language):
    lang = language.split("-")[0]
    for wanted in (lang, "en"):
        for backdrop in backdrops:
            if backdrop.get("iso_639_1") == wanted and backdrop.get("file_path"):
                return backdrop["file_path"]
    return None


def _details_key(kind, tmdb_id, language):
    return "tmdb:{}:{}:{}".format("movie" if kind == "movie" else "tv", tmdb_id, language)


def details(kind, tmdb_id, fetch=True):
    """Return a compact details/artwork dict for a movie or TV show, cached.

    With ``fetch`` off, only what is already cached (or prefetched this run) is returned.
    """
    if not enabled() or not tmdb_id:
        return {}
    tmdb_type = "movie" if kind == "movie" else "tv"
    language = _language()
    cache_key = _details_key(kind, tmdb_id, language)
    if cache_key in _memo:
        return _memo[cache_key]
    hit = cache.get(cache_key)
    if hit is not None:
        return hit
    if not fetch:
        return {}
    lang = language.split("-")[0]
    try:
        data = _get(
            "{}/{}".format(tmdb_type, tmdb_id),
            {
                "language": language,
                "append_to_response": "images,external_ids",
                "include_image_language": "{},en,null".format(lang),
            },
        )
    except LOOKUP_ERRORS as exc:
        common.log("TMDb lookup failed for {} {}: {}".format(tmdb_type, tmdb_id, exc))
        cache.set(cache_key, {}, MISS_TTL)
        return {}

    images = data.get("images") or {}
    backdrops = images.get("backdrops") or []
    external = data.get("external_ids") or {}
    date = data.get("release_date") if tmdb_type == "movie" else data.get("first_air_date")
    runtime = data.get("runtime")
    if not runtime and data.get("episode_run_time"):
        runtime = data["episode_run_time"][0]
    result = {
        "title": data.get("title") or data.get("name") or "",
        "plot": data.get("overview") or "",
        "premiered": date or "",
        "rating": data.get("vote_average") or 0,
        "votes": data.get("vote_count") or 0,
        "genres": [g.get("name") for g in data.get("genres") or [] if g.get("name")],
        "runtime": int(runtime or 0) * 60,
        "studio": [c.get("name") for c in (data.get("production_companies") or data.get("networks") or [])[:2] if c.get("name")],
        "status": data.get("status") or "",
        "imdb": external.get("imdb_id") or data.get("imdb_id") or "",
        "tvdb": str(external.get("tvdb_id") or ""),
        "poster": _image(data.get("poster_path"), "w500"),
        "fanart": _image(data.get("backdrop_path"), "w1280"),
        "landscape": _image(_pick_landscape(backdrops, language), "w1280"),
        "clearlogo": _image(_pick_logo(images.get("logos") or [], language), "w500"),
        "seasons": data.get("number_of_seasons") or 0,
        "episodes": data.get("number_of_episodes") or 0,
    }
    cache.set(cache_key, result, TTL)
    return result


def season_episodes(tmdb_id, season, fetch=True):
    """Map episode number -> {title, plot, runtime (minutes), thumb, premiered} for one TV season, cached.

    One request per season.
    """
    if not enabled() or not tmdb_id or season is None:
        return {}
    language = _language()
    cache_key = "tmdb:tvseason2:{}:{}:{}".format(tmdb_id, season, language)
    hit = cache.get(cache_key)
    if hit is None and not fetch:
        return {}
    if hit is None:
        try:
            data = _get("tv/{}/season/{}".format(tmdb_id, season), {"language": language})
        except LOOKUP_ERRORS as exc:
            common.log("TMDb season lookup failed for tv {} season {}: {}".format(tmdb_id, season, exc))
            cache.set(cache_key, {}, MISS_TTL)
            return {}
        # JSON keys are strings, so episode numbers are stored as strings.
        hit = {
            str(ep["episode_number"]): {
                "title": ep.get("name") or "",
                "plot": ep.get("overview") or "",
                "runtime": int(ep.get("runtime") or 0),
                "thumb": _image(ep.get("still_path"), "w780"),
                "premiered": ep.get("air_date") or "",
            }
            for ep in data.get("episodes") or [] if ep.get("episode_number") is not None
        }
        cache.set(cache_key, hit, TTL)
    return {int(number): ep for number, ep in hit.items()}


def prefetch(pairs, deadline=None):
    """Load details for (kind, tmdb_id) pairs: cached ones in one query, the rest in parallel.

    With a ``deadline`` (seconds), lookups not finished by then are left to the service,
    which fetches them and reloads the widgets; the items show the stored poster meanwhile.
    """
    if not enabled():
        return
    language = _language()
    wanted = {_details_key(kind, tmdb_id, language): (kind, str(tmdb_id))
              for kind, tmdb_id in pairs if tmdb_id}
    _memo.update(cache.get_many(wanted))
    missing = [pair for key, pair in wanted.items() if key not in _memo]
    if not missing:
        return
    from concurrent.futures import ThreadPoolExecutor, wait

    def load(pair):
        _memo[_details_key(pair[0], pair[1], language)] = details(*pair)

    pool = ThreadPoolExecutor(max_workers=8)
    futures = {pool.submit(load, pair): pair for pair in missing}
    _, not_done = wait(futures, timeout=deadline)
    # Lookups already running finish in the background; ones not started are dropped.
    pool.shutdown(wait=False)
    for future in not_done:
        future.cancel()
    if not_done:
        for _ in not_done:
            perf.add("tmdb_deferred")
        cache.queue_tmdb(futures[f] for f in not_done)


def pending_settled(newest, now, quiet=PENDING_QUIET):
    """True once nothing has been queued for ``quiet`` seconds (``newest``: latest queue time)."""
    return newest is not None and now - newest >= quiet


def fetch_pending(stop=None):
    """Service side: fetch artwork widgets did not wait for. True when anything new arrived.

    Waits until widgets stop queueing, then fetches the whole queue, so a refresh that leaves
    artwork in several rows ends in one widget reload rather than one per row.
    """
    if not enabled() or not pending_settled(cache.pending_newest(), time.time()):
        return False
    from concurrent.futures import ThreadPoolExecutor

    def load(pair):
        if stop is not None and stop.is_set():
            return None
        return details(*pair)

    arrived = False
    with ThreadPoolExecutor(max_workers=4) as pool:
        while not (stop is not None and stop.is_set()):
            pairs = cache.pending_tmdb()
            if not pairs:
                break
            results = list(pool.map(load, pairs))
            finished = [pair for pair, result in zip(pairs, results) if result is not None]
            cache.done_tmdb(finished)
            arrived = arrived or any(results)
            if len(finished) < len(pairs):
                break
    return arrived

def _cached(key, ttl, fetch):
    hit = cache.get(key)
    if hit is not None:
        return hit
    try:
        data = fetch()
    except LOOKUP_ERRORS as exc:
        common.log("TMDb request failed for {}: {}".format(key, exc))
        return None
    cache.set(key, data, ttl)
    return data


def search(kind, query):
    """TMDb search for 'movie', 'tv', 'collection' or 'person' (first page)."""
    if not enabled() or not query:
        return []
    lang = _language()
    data = _cached(
        "tmdb:search:{}:{}:{}".format(kind, lang, query.lower()), SEARCH_TTL,
        lambda: _get("search/{}".format(kind),
                     {"query": query, "language": lang, "include_adult": "false"}))
    return (data or {}).get("results") or []


def collection_parts(collection_id):
    if not enabled() or not collection_id:
        return []
    lang = _language()
    data = _cached("tmdb:collection:{}:{}".format(collection_id, lang), TTL,
                   lambda: _get("collection/{}".format(collection_id), {"language": lang}))
    return (data or {}).get("parts") or []


def person_credits(person_id):
    """A person's acting credits, most popular first, without talk/news shows."""
    if not enabled() or not person_id:
        return []
    lang = _language()
    data = _cached("tmdb:person:{}:{}".format(person_id, lang), TTL,
                   lambda: _get("person/{}/combined_credits".format(person_id), {"language": lang}))
    cast = [c for c in (data or {}).get("cast") or []
            if not set(c.get("genre_ids") or []) & {10763, 10767}]
    cast.sort(key=lambda c: c.get("popularity") or 0, reverse=True)
    return cast


def genre_ids(tmdb_type):
    """Map lower-cased English genre name -> id for 'movie' or 'tv', cached."""
    if not enabled():
        return {}
    # English names whatever the display language: callers ask for genres by English name.
    data = _cached("tmdb:genres:{}".format(tmdb_type), TTL,
                   lambda: _get("genre/{}/list".format(tmdb_type), {"language": "en-US"}))
    return {g["name"].lower(): g["id"] for g in (data or {}).get("genres") or []
            if g.get("name") and g.get("id")}


def discover(tmdb_type, genres=(), language="", keywords="", limit=20):
    """Popular movies or shows with any of ``genres`` (ids), most popular first, cached per page."""
    if not enabled():
        return []
    params = {
        "language": _language(),
        "sort_by": "popularity.desc",
        "vote_count.gte": MIN_VOTES,
        "include_adult": "false",
    }
    if genres:
        params["with_genres"] = "|".join(str(g) for g in genres)
    if tmdb_type == "tv":
        # News and talk shows are tagged Comedy and the like; leave them out unless asked for.
        skip = [str(g) for g in (10763, 10767) if g not in genres]
        if skip:
            params["without_genres"] = ",".join(skip)
    if language:
        params["with_original_language"] = language
    if keywords:
        params["with_keywords"] = keywords
    base = "tmdb:discover:{}:{}".format(tmdb_type, urlencode(sorted(params.items())))
    results = []
    page = 1
    while len(results) < limit:
        query = dict(params, page=page)
        data = _cached("{}:{}".format(base, page), DISCOVER_TTL,
                       lambda: _get("discover/{}".format(tmdb_type), query))
        rows = (data or {}).get("results") or []
        results.extend(r for r in rows if not r.get("adult"))
        if not rows or page >= min((data or {}).get("total_pages") or 1, 500):
            break
        page += 1
    return results[:limit]


def to_entry(result, media_type):
    """Turn a TMDb movie/tv result into the add-on's entry dict."""
    movie = media_type == "movie"
    date = (result.get("release_date") if movie else result.get("first_air_date")) or ""
    try:
        year = int(date[:4]) if date else None
    except ValueError:
        year = None
    return {
        "kind": "movie" if movie else "tvshow",
        "tmdb": str(result.get("id") or ""),
        "imdb": "",
        "tvdb": "",
        "source": "tmdb",
        "media_id": str(result.get("id") or ""),
        "title": result.get("title") or result.get("name") or "",
        "plot": result.get("overview") or "",
        "poster": _image(result.get("poster_path"), "w500"),
        "premiered": date,
        "year": year,
        "rating": result.get("vote_average") or 0,
    }

SHOW_TTL = 24 * 3600
LIST_TTL = 6 * 3600
# Lists Tally offers straight from TMDb: name -> path ({type} is movie or tv).
LISTS = {
    "trending": "trending/{type}/week",
    "popular": "{type}/popular",
    "top_rated": "{type}/top_rated",
    "upcoming": "movie/upcoming",
    "on_the_air": "tv/on_the_air",
}


def show_info(tmdb_id, refresh=False):
    """A show's seasons and airing state, cached a day: {title, poster, fanart, imdb, tvdb, ended,
    seasons: [{season, name, episodes, poster, premiered}], last_aired: [season, episode] or None}.

    ``refresh`` asks TMDb again whatever is cached (the service, once a day per watched show).
    Returns None when TMDb can't be asked.
    """
    if not enabled() or not tmdb_id:
        return None
    key = "tmdb:show:{}:{}".format(tmdb_id, _language())
    if not refresh:
        hit = cache.get(key)
        if hit is not None:
            return hit
    try:
        data = _get("tv/{}".format(tmdb_id), {"language": _language(), "append_to_response": "external_ids"})
    except LOOKUP_ERRORS as exc:
        common.log("TMDb show lookup failed for {}: {}".format(tmdb_id, exc))
        return None
    last = data.get("last_episode_to_air") or {}
    external = data.get("external_ids") or {}
    info = {
        "title": data.get("name") or "",
        "year": int((data.get("first_air_date") or "0")[:4] or 0) or None,
        "poster": _image(data.get("poster_path"), "w500"),
        "fanart": _image(data.get("backdrop_path"), "w1280"),
        "imdb": external.get("imdb_id") or "",
        "tvdb": str(external.get("tvdb_id") or ""),
        "ended": (data.get("status") or "") in ("Ended", "Canceled"),
        "last_aired": [last["season_number"], last["episode_number"]]
        if last.get("season_number") is not None and last.get("episode_number") is not None else None,
        "seasons": [
            {"season": s["season_number"], "name": s.get("name") or "",
             "episodes": int(s.get("episode_count") or 0),
             "poster": _image(s.get("poster_path"), "w500"), "premiered": s.get("air_date") or ""}
            for s in data.get("seasons") or [] if s.get("season_number") is not None
        ],
    }
    cache.set(key, info, SHOW_TTL)
    return info


def movie_meta(tmdb_id):
    """{title, year, poster, imdb} of a movie, from the cached details."""
    found = details("movie", tmdb_id)
    if not found:
        return {}
    return {"title": found.get("title"), "poster": found.get("poster"), "imdb": found.get("imdb"),
            "year": int(found["premiered"][:4]) if found.get("premiered") else None}


def find_id(kind, imdb="", tvdb=""):
    """The TMDb id of a movie or show (kind "movie" / "tv") from an IMDb or TVDb id, cached.

    An episode's own id finds its show too.
    """
    if not enabled():
        return ""
    for source, value in (("imdb_id", imdb), ("tvdb_id", tvdb)):
        if not value:
            continue
        data = _cached("tmdb:find:{}:{}".format(source, value), TTL,
                       lambda: _get("find/{}".format(value), {"external_source": source}))
        data = data or {}
        if kind == "movie":
            rows = data.get("movie_results") or []
            if rows:
                return str(rows[0]["id"])
        else:
            rows = data.get("tv_results") or []
            if rows:
                return str(rows[0]["id"])
            rows = data.get("tv_episode_results") or []
            if rows and rows[0].get("show_id"):
                return str(rows[0]["show_id"])
    return ""


def find_by_title(kind, title, year=None):
    """The TMDb id of the movie or show whose title is exactly ``title`` (year breaks ties)."""
    wanted = (title or "").strip().lower()
    if not wanted:
        return ""
    results = search("movie" if kind == "movie" else "tv", title)
    exact = [r for r in results
             if wanted in {(r.get(k) or "").strip().lower()
                           for k in ("title", "name", "original_title", "original_name")}]
    if year:
        dated = [r for r in exact
                 if str(r.get("release_date") or r.get("first_air_date") or "")[:4] == str(year)]
        exact = dated or exact
    return str(exact[0]["id"]) if exact else ""


def tmdb_list(name, tmdb_type, limit=20, page=0):
    """(results, has_more) of a TMDb list (LISTS) for 'movie' or 'tv', TMDb pages cached."""
    if not enabled() or name not in LISTS:
        return [], False
    path = LISTS[name].format(type=tmdb_type)
    lang = _language()
    wanted = (page + 1) * limit + 1
    results = []
    tmdb_page = 1
    while len(results) < wanted:
        params = {"language": lang, "page": tmdb_page}
        data = _cached("tmdb:list:{}:{}:{}".format(path, lang, tmdb_page), LIST_TTL,
                       lambda: _get(path, params))
        rows = (data or {}).get("results") or []
        results.extend(r for r in rows if not r.get("adult"))
        if not rows or tmdb_page >= min((data or {}).get("total_pages") or 1, 500):
            break
        tmdb_page += 1
    start = page * limit
    return results[start:start + limit], len(results) > start + limit
