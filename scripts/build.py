import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import time
import unicodedata
from pathlib import Path
from urllib.parse import quote_plus

from dotenv import load_dotenv
import feedparser
import requests
import yaml
from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env", override=False)
DATA = ROOT / "data" / "editions"
OUT = ROOT / "site"
UA = "Mozilla/5.0 (compatible; VibraBot/1.0; +https://github.com)"
STOPWORDS = set("""a al algo como con de del ella en era este esto la las le los me mi mux no o or para pero por que quien si sin su sus te the a an and are as at be by for from has have in is its of on or that the this to was were will with new your you his her their after over how why what who""".split())


def log(*a):
    print(*a, file=sys.stderr, flush=True)


BOILERPLATE = re.compile(r"La entrada .{0,300}? se publicó primero en .*?\|The post .{0,300}? appeared first on .*?\|Leer más.*?\|Continue reading.*?", re.I | re.M)


def clean_text(raw, limit=None):
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = html.unescape(re.sub(r"\s+", " ", text)).strip()
    text = BOILERPLATE.sub("", text)
    text = text.strip()
    if limit and len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0].rstrip(".,;:") + "…"
    return text


# Palabras que aparecen en casi todos los titulares de moda/belleza: NO identifican un tema.
GENERIC = set("""otro inviernos primavera verano 2024 2025 2026 2027 tendencia tendencias looks look moda estilo estilos
mejores mejor nueva nuevas ahora estas todos toda son hace hacen vuelve vuelven regresa clavé
imprescindibles imprescindible compra compras ideas modelos disloque disloques más bonitas favoritos favorito
zara mango massimo dutti corte piezas stradivarius bershka lefties primark indie de corte inglés
season fall winter spring summer best new trend trends style outfit outfits fashion week""".split())
SHOPPING = re.compile(r"\b(compras|shopping|chollos|ofertas)\b", re.I)


def tokens(text):
    return {w for w in re.findall(r"[a-záéíóúñü]{3,}", text.lower()) if w not in STOPWORDS}


def topic_tokens(text):
    """Palabras que sí identifican el tema de un titular (nombres, marcas, eventos...)."""
    return tokens(text) - GENERIC


# ---------------------------------------------------------------- recogida

def feed_image(e):
    for key in ("media_content", "media_thumbnail"):
        for m in e.get(key) or []:
            if m.get("url") and not m.get("url", "").endswith((".mp4", ".mp3")):
                return m["url"]
    for l in e.get("links", []):
        if l.get("rel") == "enclosure" and l.get("type", "").startswith("image"):
            return l["href"]
    for blob in [e.get("summary", "")] + [c.get("value", "") for c in e.get("content", [])]:
        m = re.search(r"<img[^>]+src=[\"']([^\"']+)", blob or "")
        if m and not m.group(1).endswith(".gif"):
            return html.unescape(m.group(1))
    return ""


def og_image(url):
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=15)
        m = re.search(r'<meta[^>]+(?:property|name)=["\'](?:og:image|twitter:image)["\'][^>]+content=["\']([^"\']+)', r.text) or \
            re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\'](?:og:image|twitter:image)', r.text)
        return html.unescape(m.group(1)) if m else ""
    except Exception:
        return ""


def page_text(url, limit=4000):
    """Texto de los párrafos de la noticia original, solo como material de referencia para la IA."""
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=15)
        r.raise_for_status()
    except Exception:
        return ""
    raw = re.sub(r"(?is)<(script|style|noscript|figure|aside|nav|footer)[^>]*>.*?</\1>", " ", r.text)
    out = ""
    for m in re.finditer(r"(?is)<p[^>]*>(.*?)</p>", raw):
        t = clean_text(m.group(1))
        if len(t) > 60:
            out += t + "\n"
        if len(out) > limit:
            break
    return out[:limit]


def image_matches_story(story, image_url):
    if not image_url or not isinstance(image_url, str):
        return False

    image_url = html.unescape(image_url).lower()
    text = " ".join([
        story.get("title") or "",
        story.get("summary") or "",
        story.get("why") or "",
        " ".join(src.get("name", "") for src in story.get("sources", []) if isinstance(src, dict)),
    ])
    story_tokens = topic_tokens(clean_text(text))
    if not story_tokens:
        return True

    image_tokens = set(re.findall(r"[a-záéíóúñü]{4,}", re.sub(r"^https?://", "", image_url)))
    if story_tokens & image_tokens:
        return True

    source_text = " ".join(
        (src.get("name", "") + " " + src.get("url", ""))
        for src in story.get("sources", []) if isinstance(src, dict)
    ).lower()
    for token in sorted(story_tokens)[:5]:
        if token in source_text:
            return True
    return False


def normalize_story_images(stories):
    for story in stories:
        image = (story.get("image") or "").strip()
        if not image:
            continue
        if image_matches_story(story, image):
            continue

        candidates = []
        for src in story.get("sources", []) or []:
            if not isinstance(src, dict):
                continue
            cand = og_image(src.get("url", ""))
            if cand and cand != image:
                candidates.append(cand)
        candidates.append(image)

        for cand in candidates:
            if image_matches_story(story, cand):
                story["image"] = cand
                if story.get("image_credit") in (None, ""):
                    story["image_credit"] = (story.get("sources", [{}])[0] or {}).get("name", "")
                break
        else:
            story["image"] = ""
            story["image_credit"] = ""


def fill_images(stories):
    for s in stories:
        if not s.get("image") and s.get("sources"):
            s["image"] = og_image(s["sources"][0]["url"])
            s["image_credit"] = s["sources"][0]["name"] if s["image"] else ""
    normalize_story_images(stories)


def fetch_feed(feed, since):
    try:
        resp = requests.get(feed["url"], headers={"User-Agent": UA}, timeout=20)
        resp.raise_for_status()
        parsed = feedparser.parse(resp.content)
    except Exception as exc:
        log(f"  ! {feed['name']}: {exc}")
        return []
    items = []
    for e in parsed.entries:
        pub = e.get("published_parsed") or e.get("updated_parsed")
        when = dt.datetime(*pub[:6], tzinfo=dt.timezone.utc) if pub else None
        if when and when < since:
            continue
        title = clean_text(e.get("title"))
        if not title or not e.get("link"):
            continue
        items.append({
            "title": title,
            "link": e["link"],
            "image": feed_image(e),
            "source": feed["name"],
            "lang": feed.get("lang", "es"),
            "published": when.isoformat() if when else None,
            "summary": clean_text(e.get("summary"), 400),
        })
    log(f"  • {feed['name']}: {len(items)} noticias")
    return items


def get_json(url, retries=3, **kw):
    headers = {"User-Agent": UA, **(kw.pop("headers", {}))}
    for attempt in range(retries):
        try:
            resp = requests.get(url, headers=headers, timeout=30, **kw)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            status = getattr(exc.response, "status_code", None)
            if attempt == retries - 1 or (status and 400 <= status < 500 and status != 429):
                raise
            time.sleep(5 * (attempt + 1))


def apple_music_chart(country, kind, limit=10):
    try:
        data = get_json(f"https://rss.applemarketingtools.com/api/v2/{country}/music/most-played/{limit}/{kind}.json")
    except Exception as exc:
        log(f"  ! Apple Music {kind}: {exc}")
        return []
    return [{
        "position": i + 1,
        "title": r["name"],
        "artist": r.get("artistName", ""),
        "image": r.get("artworkUrl100", "").replace("100x100", "300x300"),
        "url": r.get("url", ""),
        "release_date": r.get("releaseDate", ""),
    } for i, r in enumerate(data["feed"]["results"]) ]


def tmdb_request(path, key, **params):
    headers = {}
    if len(key) > 50:
        headers["Authorization"] = f"Bearer {key}"
    else:
        params["api_key"] = key
    return get_json(f"https://api.themoviedb.org/3{path}", params=params, headers=headers)


def tmdb_lists(key, country):
    region = country.upper()
    out = {}
    try:
        movies = tmdb_request("/movie/now_playing", key, language="es-ES", region=region)["results"]
        tv = tmdb_request("/trending/tv/week", key, language="es-ES")["results"]
    except Exception as exc:
        log(f"  ! TMDB: {exc}")
        return None

    def item(r, kind):
        return {
            "title": r.get("title") or r.get("name"),
            "overview": clean_text(r.get("overview"), 220),
            "score": round(r.get("vote_average") or 0, 1),
            "votes": r.get("vote_count") or 0,
            "date": r.get("release_date") or r.get("first_air_date") or "",
            "image": f"https://image.tmdb.org/t/p/w342{r['poster_path']}" if r.get("poster_path") else "",
            "url": f"https://www.themoviedb.org/{kind}/{r['id']}",
            "popularity": r.get("popularity") or 0,
        }

    out["movies"] = sorted((item(r, "movie") for r in movies[:10]), key=lambda x: -x["popularity"])
    out["tv"] = [item(r, "tv") for r in tv[:10]]
    return out


def itunes_movies(country, limit=10):
    try:
        data = get_json(f"https://itunes.apple.com/{country}/rss/topmovies/limit={limit}/json")
    except Exception as exc:
        log(f"  ! iTunes películas: {exc}")
        return []
    res = []
    for i, e in enumerate(data["feed"].get("entry", [])):
        imgs = e.get("im:image", [])
        res.append({
            "position": i + 1,
            "title": e["im:name"]["label"],
            "genre": e.get("category", {}).get("attributes", {}).get("label", ""),
            "image": imgs[-1]["label"].replace("170x170", "300x300") if imgs else "",
            "url": e.get("link", {}).get("attributes", {}).get("href", "") if isinstance(e.get("link"), dict) else "",
            "overview": clean_text(e.get("summary", {}).get("label", ""), 200),
        })
    return res


# ---------------------------------------------------------------- selección

def rank_items(items):
    """Puntúa cada noticia: cuántas otras fuentes hablan del mismo tema + novedad + idioma."""
    toks = [topic_tokens(item["title"]) for item in items]
    now = dt.datetime.now(dt.timezone.utc)
    for idx, it in enumerate(items):
        echoes = {items[j]["source"] for j in range(len(items)) if j != idx and items[j]["source"] != it["source"] and len(toks[idx] & toks[j]) >= 2}
        age_days = (now - dt.datetime.fromisoformat(it["published"]) if it["published"] else dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=7)).days
        it["score"] = len(echoes) * 3 + max(0, 7 - age_days) * 0.4 + (1 if it["lang"] == "es" else 0)
        if SHOPPING.search(it["title"]) or SHOPPING.search(it["link"]):
            it["score"] -= 2.5
    return sorted(items, key=lambda x: -x["score"])


def diverse_pick(ranked, n, per_source=2, overlap=2, avoid_tokens=(), avoid_urls=()):
    """Elige noticias de temas distintos. Descarta las que repiten tema (o URL) de otra y ya publicó."""
    picked, count, seen = [], {}, []
    avoid_urls = set(avoid_urls)
    for it in ranked:
        t = topic_tokens(it["title"])
        if it["link"] in avoid_urls or count.get(it["source"], 0) >= per_source:
            continue
        if any(len(t & s) >= overlap for s in seen) or any(len(t & s) >= overlap + 1 for s in avoid_tokens):
            continue
        picked.append(it)
        seen.append(t)
        count[it["source"]] = count.get(it["source"], 0) + 1
        if len(picked) == n:
            break
    return picked


def norm_title(text):
    return re.sub(r"[^a-z0-9]+", " ", unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()).strip()


def previous_used(current_id, editions=4):
    """URLs y títulos ya publicados en las últimas ediciones, para no repetirlos este semana."""
    urls, titles = set(), set()
    files = sorted(DATA.glob("*.json"), reverse=True)
    for p in files[:editions]:
        try:
            ed = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        for sec in ed.get("sections", {}).values():
            for st in sec.get("stories", []):
                urls.update(x.get("url", "") for x in st.get("sources", []))
                titles.add(norm_title(st.get("title", "")))
            for h in sec.get("headlines", []):
                urls.add(h.get("url", ""))
    return urls, titles


def previous_titles(current_id, sid, limit):
    out = []
    for p in sorted(DATA.glob("*.json"), reverse=True):
        if p.stem == current_id:
            continue
        try:
            ed = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        out += [st["title"] for sec in ed.get("sections", {}).values() for st in sec.get("stories", []) if sec.get("stories")]
        if len(out) >= limit:
            break
    return out[:limit]


def filter_items(items, exclude_keywords, used_urls, used_titles):
    """Quita duplicados, temas ajenos y noticias ya publicadas en ediciones anteriores."""
    out, seen_urls, seen_titles = [], set(), set()
    kws = [k.lower() for k in exclude_keywords or []]
    for it in items:
        key = norm_title(it["title"])
        hay = f"{it['title']} {it['link']}".lower()
        if it["link"] in seen_urls or key in seen_titles:
            continue
        if any(k in hay for k in kws):
            continue
        if it["link"] in used_urls or key in used_titles:
            continue
        t = topic_tokens(it["title"])
        if any(len(t & u) >= 3 for u in seen_titles):
            continue
        seen_urls.add(it["link"])
        seen_titles.add(key)
        out.append(it)
    return out


# ---------------------------------------------------------------- IA (Gemini, opcional)

GEMINI_GAP = 13  # segundos entre llamadas: el plan gratuito limita las peticiones por minuto
MAX_MODELS = 4  # cuántos modelos distintos se publican, como máximo
_last_call = [0.0]
_available_models = {}

# Modelos de texto recomendados, por orden de preferencia (se usan solo si tienen cuota activa).
DEFAULT_GEMINI_MODELS = [
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-3-flash-preview",
    "gemini-flash-latest",
    "gemini-flash-lite-latest",
]

# Modelos que NO sirven para escribir texto/JSON (imágenes, voz, tiempo real, embeddings...).
NOT_TEXT_MODELS = re.compile(
    r"image|imagen|tts|audio|live|embed|robotics|computer-use|aqa|gemma|learnlm"
)


def normalize_model_name(name):
    text = (name or "").strip().lower().replace("_", "-")
    for prefix in ("models/", "publishers/google/models/", "publishers/google/"):
        if text.startswith(prefix):
            text = text[len(prefix):]
    return text


def is_text_model(name):
    return name.startswith("gemini") and not NOT_TEXT_MODELS.search(name)


def gemini_models(key, configured):
    """Devuelve los modelos de TEXTO activos para esta API key, por orden de preferencia."""
    cache_key = key[-12:]
    if cache_key in _available_models:
        return _available_models[cache_key]
    configured_norm = [normalize_model_name(m) for m in configured if m]
    try:
        available, token = set(), None
        while True:
            params = {"pageSize": 1000}
            if token:
                params["pageToken"] = token
            response = requests.get(
                "https://generativelanguage.googleapis.com/v1beta/models",
                headers={"x-goog-api-key": key},
                params=params,
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()
            available |= {
                normalize_model_name(item.get("name", ""))
                for item in data.get("models", [])
                if "generateContent" in item.get("supportedGenerationMethods", [])
            }
            token = data.get("nextPageToken")
            if not token:
                break
        text_models = {m for m in available if is_text_model(m)}
        ordered = []
        for m in configured_norm + DEFAULT_GEMINI_MODELS:
            if m in text_models and m not in ordered:
                ordered.append(m)
        extras = sorted(m for m in text_models if "flash" in m and m not in ordered)
        ordered += extras
        selected = ordered[:MAX_MODELS]
        if not selected:
            log("  ! Tu clave no tiene modelos de texto disponibles. Modelos vistos: %s" % ", ".join(sorted(text_models)) or "ninguno")
            return []
        log(f"  • Modelos Gemini que se usarán: {', '.join(selected)}")
        _available_models[cache_key] = selected
        return selected
    except Exception as exc:
        log(f"  ! No se pudo consultar la lista de modelos Gemini: {exc}")
        fallback = [m for m in configured_norm if is_text_model(m)] or DEFAULT_GEMINI_MODELS[:MAX_MODELS]
        return fallback[:MAX_MODELS]


def parse_gemini_json(text):
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip(), flags=re.I)
    match = re.search(r"\{.*\}", cleaned, re.S)
    if match:
        cleaned = match.group(0)
    if not cleaned:
        raise ValueError("Gemini devolvió una respuesta vacía")
    return json.loads(cleaned)


def quota_info(resp):
    """Extrae de un error 429 cuántas cuotas se han agotado y cuánto hay que esperar."""
    quotes = list(dict.fromkeys(re.findall(r'"quotaInfo"\s*:\s*\{.*?\}', resp.text, re.S)))
    delay = re.search(r'"retryDelay"\s*:\s*"([^\"]+)"', resp.text)
    return quotes, float(delay.group(1).replace("s", "")) if delay else None


def gemini(prompt, key, models, schema=None):
    config = {"responseMimeType": "application/json", "temperature": 0.7, "maxOutputTokens": 32768}
    if schema:
        config["responseSchema"] = schema
    active_models = gemini_models(key, models)
    if not active_models:
        return None
    for model in active_models:
        cfr = dict(config)
        if model.startswith("gemini-2.5-flash"):
            cfr["thinkingConfig"] = {"thinkingBudget": 0}
        body = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": cfr}
        for attempt in range(3):
            time.sleep(max(0, GEMINI_GAP - (time.time() - _last_call[0])))
            _last_call[0] = time.time()
            try:
                r = requests.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                    headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                    json=body,
                    timeout=240,
                )
                if r.status_code == 429:
                    quotes, delay = quota_info(r)
                    log(f"  ! Gemini {model}: HTTP 429 {', '.join(quotes) or r.text[:300]}" )
                    if any("PerDay" in q for q in quotes) or (not quotes and "PerDay" in r.text):
                        break
                    if delay:
                        time.sleep(min(delay, 90))
                        continue
                if r.status_code >= 500:
                    log(f"  ! Gemini {model}: HTTP {r.status_code} {r.text[:200]}.")
                    time.sleep(20 * (attempt + 1))
                    continue
                if r.status_code == 404:
                    log(f"  ! Gemini {model}: modelo no encontrado; probamos el siguiente.")
                    break
                if r.status_code >= 400:
                    log(f"  ! Gemini {model}: HTTP {r.status_code} {r.text[:200]}.")
                    break
                payload = r.json()
                candidates = payload.get("candidates") or []
                if not candidates:
                    log(f"  ! Gemini {model}: respuesta sin candidatos: {payload}")
                    break
                parts = candidates[0].get("content", {}).get("parts", [])
                text = "".join(p.get("text", "") for p in parts if p.get("text"))
                if not text:
                    log(f"  ! Gemini {model}: respuesta sin texto. {payload}")
                    break
                return parse_gemini_json(text)
            except (json.JSONDecodeError, ValueError) as exc:
                log(f"  ! Gemini {model}: JSON no válido ({exc}); reintentando.")
                continue
            except Exception as exc:
                log(f"  ! Gemini {model}: {exc}")
                break
    return None


SECTION_PROMPT = """Eres editora de VIBRA, una revista digital en español de cultura pop (moda, música y cine).
Abajo tienes las noticias de la semana {section}. Debes escribir un resumen editorial claro y atractivo.

Reglas de selección:
- Cada historia debe tratar un tema distinto; no elijas dos noticias sobre lo mismo.
- Prioriza historias reales (desfiles, campañas, colecciones, lanzamientos, estrenos, premios, actrices, artistas, etc.) sobre rumores o contenido de baja calidad.
- Descarta las que no pertenezcan a la sección (restaurantes, recetas, viajes, horóscopo, etc.).
- Si la noticia no es útil para la sección, ignórala.
- No inventes datos que no estén en las noticias.
- La respuesta debe ser JSON con este formato:
{
  "historias": [
    {"titulo": "...", "resumen": "...", "clave": "...", "compra": "...", "fuentes": [{"nombre": "...", "url": "..."}]}
  ]
}

Formato exacto:
- 'titulo': 60-100 caracteres, atractivo y específico.
- 'resumen': 2-3 frases originales en español (50-80 palabras).
- 'clave': una frase corta con valor editorial.
- 'compra': una frase con un producto real relacionado; si no hay nada útil, "".
- 'fuentes': lista de 1-3 fuentes fiables, con nombre y URL.

No inventes marcas ni productos ni URLs.
"""

INTRO_PROMPT = """Eres editora de VIBRA (revista digital en español de moda, música y cine).
Con estos titulares y resúmenes de la semana, escribe una introducción editorial atractiva para la edición.

Reglas:
- 50-70 palabras.
- Tone editorial, claro, atractivo.
- No cites fuentes ni enlaces.
- Devuelve JSON: {"titulo": "...", "intro": "..."}
"""


def ai_section(name, candidates, n, key, models, avoid_titles=()):
    listing = "\n".join(f"- {c['source']}: {c['title']} | {c['link']} | {c['summary'][:280]}" for c in candidates[:40])
    avoid = "\n".join(f"- {t}" for t in avoid_titles[:30])
    prompt = SECTION_PROMPT.format(section=name, n=n)
    prompt += "\nCandidatos:\n" + listing
    if avoid:
        prompt += "\nYA PUBLICADAS EN OTRAS EDICIONES:\n" + avoid
    ask = n + 3
    prompt = prompt + f"\nNecesito {ask} historias, pero elige solo las mejores {n}."
    data = gemini(prompt, key, models) or {}
    if not isinstance(data, dict) or not isinstance(data.get("historias"), list):
        return []
    stories = []
    for s in data["historias"][:n]:
        title = str(s.get("titulo") or "").strip()
        summary = str(s.get("resumen") or "").strip()
        if not title or not summary:
            continue
        stories.append({
            "title": title,
            "summary": summary[:400],
            "why": str(s.get("clave") or "").strip(),
            "shop": str(s.get("compra") or "").strip()[:60],
            "sources": [{
                "name": str(f.get("nombre") or "").strip(),
                "url": str(f.get("url") or "").strip(),
            } for f in (s.get("fuentes") or []) if str(f.get("url") or "").strip()],
            "image": "",
            "image_credit": "",
            "ai": True,
        })
    return stories


def plain_section(candidates, n):
    stories = []
    for c in candidates[:n]:
        title = str(c.get("title") or "").strip()
        if not title:
            continue
        stories.append({
            "title": title,
            "summary": (c.get("summary") or title)[:350],
            "why": "Noticia destacada de la semana.",
            "shop": "",
            "sources": [{"name": str(c.get("source") or "").strip(), "url": str(c.get("link") or "").strip()}],
            "image": c.get("image") or "",
            "image_credit": str(c.get("source") or "").strip(),
            "ai": False,
        })
    return stories


ARTICLE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "articulos": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "id": {"type": "INTEGER"},
                    "desatacado": {"type": "STRING"},
                    "bloques": {
                        "type": "ARRAY",
                        "items": {
                            "type": "OBJECT",
                            "properties": {
                                "subtitulo": {"type": "STRING"},
                                "parrafos": {"type": "ARRAY", "items": {"type": "STRING"}},
                            },
                            "required": ["parrafos"],
                        },
                    },
                },
                "required": ["id", "bloques"],
            },
        }
    },
    "required": ["articulos"],
}


def ai_articles(name, stories, key, models, batch=2):
    done = 0
    for start in range(0, len(stories), batch):
        chunk = stories[start:start + batch]
        blocks = []
        for i, s in enumerate(chunk):
            refs = "\n".join(f"- {c['source']}: {c['title']} | {c['link']} | {c['summary'][:200]}" for c in s.get("_ctx", [])[:5])
            blocks.append(f"## #{i + 1}\nTitulo: {s['title']}\nResumen: {s['summary']}\nClave: {s['why']}\nMaterial de referencia:\n{refs}")
        prompt = f"Eres editora de VIBRA, escribe un artículo en español para {name}.\n\n" + "\n\n".join(blocks) + "\n\nReglas: 1) no inventes datos ni fuentes; 2) usa 3-5 bloques con subtitulos; 3) cada bloque debe tener 2-4 párrafos; 4) devuelve JSON con {\"articulos\":[{\"id\": 0, \"desatacado\": \"...\", \"bloques\":[{\"subtitulo\": \"...\", \"parrafos\":[\"...\"]}]}]}"
        data = gemini(prompt, key, models, ARTICLE_SCHEMA) or {}
        if not isinstance(data, dict) or not isinstance(data.get("articulos"), list):
            continue
        for a in data["articulos"]:
            idx = a.get("id")
            if not isinstance(idx, int) or idx < 0 or idx >= len(chunk):
                continue
            story = chunk[idx]
            story["highlight"] = str(a.get("desatacado") or "").strip()[:220]
            story["body"] = []
            for block in a.get("bloques") or []:
                sub = str(block.get("subtitulo") or "").strip()
                paras = [str(p).strip() for p in (block.get("parrafos") or []) if str(p).strip()]
                if not paras:
                    continue
                if sub:
                    story["body"].append(f"## {sub}")
                story["body"].extend(paras)
            done += 1
    return done


def article_texts(name, stories, key, models):
    if not stories:
        return []
    if not key:
        return []
    return ai_articles(name, stories, key, models, batch=2)


def make_edition(cfg):
    now = dt.datetime.now(dt.timezone.utc)
    since = now - dt.timedelta(days=8)
    iso = now.isocalendar()
    monday = (now - dt.timedelta(days=now.weekday())).date()
    edition = {
        "id": f"{iso[0]}-S{iso[1]:02d}",
        "week": iso[1],
        "year": iso[0],
        "date": monday.isoformat(),
        "generated_at": now.isoformat(timespec="seconds"),
        "sections": {},
    }
    gkey = os.environ.get("GEMINI_API_KEY", "").strip()
    tkey = os.environ.get("TMDB_API_KEY", "").strip()
    if not gkey:
        raise RuntimeError("Falta GEMINI_API_KEY: no se pueden crear titulares ni artículos por IA")
    n = cfg["ai"]["items_per_section"]
    country = cfg["charts"]["country"]
    used_urls, used_titles = previous_used(edition["id"])
    for sid, sec in cfg["sections"].items():
        log(f"Sección {sec['title']}")
        items = [it for f in sec["feeds"] for it in fetch_feed(f, since)]
        total = len(items)
        items = filter_items(items, sec.get("exclude_keywords", []), used_urls, used_titles)
        log(f"  • {len(items)} noticias válidas de {total} (sin duplicados, sin temas ajenos, sin repetidas)")
        ranked = rank_items(items)
        candidates = diverse_pick(ranked, 40, per_source=8, overlap=2, avoid_tokens=(), avoid_urls=tuple(used_urls))
        stories = ai_section(sec["title"], candidates, n, gkey, cfg["ai"]["models"], avoid_titles=previous_titles(edition["id"], sid, n * 2)) if gkey and candidates else []
        if not stories:
            if candidates:
                raise RuntimeError(
                    f"Gemini no generó historias válidas para {sec['title']}; revisa la cuota de la API y la respuesta "
                    "de la IA."
                )
            stories = plain_section(candidates, n)
        else:
            article_texts(sec["title"], stories, gkey, cfg["ai"]["models"])
        for s in stories:
            s["_ctx"] = [i for i in candidates if i["title"] != s["title"]][:5]
        fill_images(stories)

        heads = diverse_pick(ranked, 8, per_source=2, avoid_tokens=(), avoid_urls=tuple(used_urls))
        edition["sections"][sid] = {"title": sec["title"], "stories": stories, "headlines": [
            {"title": c["title"], "source": c["source"], "url": c["link"], "image": c["image"]}
            for c in heads
        ]}

    log("Listas y valoraciones")
    edition["sections"]["musica"]["albums"] = apple_music_chart(country, "albums")
    edition["sections"]["musica"]["songs"] = apple_music_chart(country, "songs")
    tm = tmdb_lists(tkey, country) if tkey else None
    if tm:
        edition["sections"]["cine"]["movies"] = tm["movies"]
        edition["sections"]["cine"]["tv"] = tm["tv"]
    else:
        edition["sections"]["cine"]["digital"] = itunes_movies(country)

    if gkey:
        heads = "\n".join(f"- {s['title']}" for sec in edition["sections"].values() for s in sec["stories"][:3])
        intro = gemini(INTRO_PROMPT.format(items=heads), gkey, cfg["ai"]["models"]) or {}
        edition["headline"] = intro.get("titulo", "")
        edition["intro"] = intro.get("intro", "")
    return edition


def nice_date(iso):
    d = dt.date.fromisoformat(iso[:10])
    return f"{d.day} de {['enero', 'febrero', 'marzo', 'abril', 'mayo', 'junio', 'julio', 'agosto', 'septiembre', 'octubre', 'noviembre', 'diciembre'][d.month - 1]} de {d.year}"


def slugify(text):
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-") or "articulo"


def assign_slugs(edition):
    seen = set()
    for sec in edition["sections"].values():
        for s in sec.get("stories", []):
            slug = base = slugify(s["title"])
            n = 2
            while slug in seen:
                slug = f"{base}-{n}"
                n += 1
            seen.add(slug)
            s["slug"] = slug


def render(cfg):
    editions = sorted((json.loads(p.read_text(encoding="utf-8")) for p in DATA.glob("*.json")), key=lambda e: e["date"], reverse=True)
    if not editions:
        sys.exit("No hay ediciones para renderizar. Ejecuta --render primero.")
    for e in editions:
        assign_slugs(e)
    base_url = (cfg["site"].get("base_url") or os.environ.get("SITE_URL") or "").rstrip("/")
    money = cfg.get("monetization", {})
    env = Environment(loader=FileSystemLoader(str(ROOT / "templates")), autoescape=select_autoescape(["html", "xml"]))
    env.filters["date"] = nice_date
    env.globals.update(site=cfg["site"], money=money, light=lambda score, votes: "verde" if votes >= 20 and score >= 7.3 else "ambar" if score >= 6 else "rojo" if score else "gris", quote=quote_plus, base_url=base_url)

    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True, exist_ok=True)
    shutil.copytree(ROOT / "static", OUT / "static")

    def write(path, template, depth, **ctx):
        dest = OUT / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(env.get_template(template).render(root="../" * depth, path=path, **ctx), encoding="utf-8")

    write("index.html", "edition.html", 0, e=editions[0], latest=True)
    for e in editions:
        write(f"semana/{e['id']}/index.html", "edition.html", 2, e=e, latest=False)
        flat = [(sid, sec, s) for sid, sec in e["sections"].items() for s in sec["stories"]]
        for sid, sec, s in flat:
            related = []
            for xsid, xsec, x in flat:
                if xsid == sid and x["title"] == s["title"]:
                    continue
                related.append((xsid, xsec, x))
            write(f"semana/{e['id']}/{s['slug']}/index.html", "article.html", 3, e=e, sid=sid, sec=sec, s=s, related=related[:6])
    write("archivo/index.html", "archive.html", 1, editions=editions)
    for page in ("aviso-legal", "privacidad", "cookies", "sobre-vibra"):
        write(f"{page}/index.html", f"{page}.html", 1)
    write("rss.xml", "rss.xml", 0, editions=editions[:20])
    write("sitemap.xml", "sitemap.xml", 0, editions=editions)
    write("manifest.webmanifest", "manifest.webmanifest", 0)
    (OUT / "robots.txt").write_text(f"User-agent: *\nAllow: /\nSitemap: {base_url}/sitemap.xml\n" if base_url else "User-agent: *\nAllow: /\n", encoding="utf-8")
    (OUT / "ads.txt").write_text(f"google.com, pub-4375001471432617, DIRECT, f0c84d...\n", encoding="utf-8")
    log(f"Web generada en {OUT} ({len(editions)} ediciones)")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--render", action="store_true", help="solo regenerar la web con las ediciones existentes")
    args = parser.parse_args()
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    if not args.render:
        edition = make_edition(cfg)
        DATA.mkdir(parents=True, exist_ok=True)
        (DATA / f"{edition['id']}.json").write_text(json.dumps(edition, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"Edición {edition['id']} guardada")
    render(cfg)


if __name__ == "__main__":
    main()
