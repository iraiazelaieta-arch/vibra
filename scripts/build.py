#!/usr/bin/env python3
"""VIBRA: genera la edición semanal y la web estática.

Uso:
  python scripts/build.py            # recoge datos, crea la edición de esta semana y genera la web
  python scripts/build.py --render   # solo regenera la web con las ediciones ya guardadas

Claves opcionales (variables de entorno): GEMINI_API_KEY, TMDB_API_KEY, SITE_URL
"""
import argparse
import datetime as dt
import html
import json
import os
import re
import shutil
import sys
import time
import unicodedata
from pathlib import Path
from urllib.parse import quote_plus

import feedparser
import requests
import yaml
from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "editions"
OUT = ROOT / "site"
UA = "Mozilla/5.0 (compatible; VibraBot/1.0; +https://github.com)"
STOPWORDS = set("""a al algo como con de del el ella en era es esta este esto la las lo los mas más me mi muy no o para pero por que qué se sin sobre su sus tu un una uno y ya
the a an and are as at be by for from has have in is it its of on or that the this to was were will with new your you his her their after over how why what who""".split())


def log(*a):
    print(*a, file=sys.stderr, flush=True)


BOILERPLATE = re.compile(r"(La entrada .{0,300}? se public[oó] primero en .*$|The post .{0,300}? appeared first on .*$|Leer m[aá]s.*$|Read more.*$|Continue reading.*$|\s\S[^.]{0,200}? es de [\w.-]+\.(com|es)\s*\.?$)", re.I)


def clean_text(raw, limit=None):
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = html.unescape(re.sub(r"\s+", " ", text)).strip()
    text = BOILERPLATE.sub("", text).strip()
    if limit and len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0].rstrip(",.;:") + "…"
    return text


def tokens(text):
    return {w for w in re.findall(r"[a-záéíóúñü0-9]{3,}", text.lower()) if w not in STOPWORDS}


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


def fill_images(stories):
    for s in stories:
        if not s.get("image") and s.get("sources"):
            s["image"] = og_image(s["sources"][0]["url"])
            s["image_credit"] = s["sources"][0]["name"] if s["image"] else ""


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
    log(f"  · {feed['name']}: {len(items)} noticias")
    return items


def get_json(url, retries=3, **kw):
    headers = {"User-Agent": UA, **kw.pop("headers", {})}
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
    } for i, r in enumerate(data["feed"]["results"])]


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

    out["movies"] = sorted((item(r, "movie") for r in movies), key=lambda x: -x["popularity"])[:10]
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
    """Puntúa cada noticia: cuántas otras fuentes hablan de lo mismo + novedad + idioma."""
    toks = [tokens(i["title"]) for i in items]
    now = dt.datetime.now(dt.timezone.utc)
    for idx, it in enumerate(items):
        echoes = {items[j]["source"] for j in range(len(items))
                  if j != idx and items[j]["source"] != it["source"] and len(toks[idx] & toks[j]) >= 2}
        age_days = (now - dt.datetime.fromisoformat(it["published"])).days if it["published"] else 7
        it["score"] = len(echoes) * 3 + max(0, 7 - age_days) * 0.4 + (1 if it["lang"] == "es" else 0)
    return sorted(items, key=lambda x: -x["score"])


def diverse_pick(ranked, n, per_source=2, overlap=3):
    picked, count, seen = [], {}, []
    for it in ranked:
        t = tokens(it["title"])
        if count.get(it["source"], 0) >= per_source or any(len(t & s) >= overlap for s in seen):
            continue
        picked.append(it)
        seen.append(t)
        count[it["source"]] = count.get(it["source"], 0) + 1
        if len(picked) == n:
            break
    return picked


# ---------------------------------------------------------------- IA (Gemini, opcional)
GEMINI_GAP = 13  # segundos entre llamadas: el plan gratuito limita las peticiones por minuto
_last_call = [0.0]


def gemini(prompt, key, models, schema=None):
    config = {"responseMimeType": "application/json", "temperature": 0.7, "maxOutputTokens": 32768}
    if schema:
        config["responseSchema"] = schema
    body = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": config}
    for model in models:
        for attempt in range(3):
            time.sleep(max(0, GEMINI_GAP - (time.time() - _last_call[0])))
            _last_call[0] = time.time()
            try:
                r = requests.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                    headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                    json=body, timeout=240)
                if r.status_code == 429 or r.status_code >= 500:
                    quota = re.findall(r'"quotaId":\s*"([^"]+)"', r.text)
                    log(f"  ! Gemini {model}: HTTP {r.status_code} {', '.join(dict.fromkeys(quota)) or r.text[:300]!r}")
                    if "PerDay" in r.text:
                        break
                    time.sleep(20 * (attempt + 1))
                    continue
                if r.status_code == 404:
                    break
                r.raise_for_status()
                parts = r.json()["candidates"][0]["content"]["parts"]
                text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
                text = re.sub(r"^```(?:json)?|```$", "", text.strip()).strip()
                return json.loads(text)
            except Exception as exc:
                log(f"  ! Gemini {model}: {exc}")
                break
    return None


SECTION_PROMPT = """Eres editora de VIBRA, una revista digital en español de cultura pop (moda, música y cine).
Abajo tienes las noticias de la última semana de la sección "{section}", publicadas por medios de referencia.
Elige las {n} historias MÁS importantes e interesantes para un público joven hispanohablante (evita repetir el mismo tema).
Para cada una escribe con tus propias palabras (NUNCA copies frases de la fuente):
- "titulo": titular atractivo en español (máx. 90 caracteres)
- "resumen": 2-3 frases originales en español (50-80 palabras) explicando qué ha pasado
- "clave": una frase corta de por qué importa o qué significa (máx. 20 palabras)
- "compra": búsqueda de 2-5 palabras para encontrar en Amazon un producto real relacionado (p. ej. "mocasines mujer piel", "Rosalía Lux vinilo", "Dune Blu-ray"); vacío si no hay nada lógico
- "fuentes": lista de números [n] de las noticias en las que se basa (una o varias)
No inventes datos que no estén en las noticias.
Devuelve SOLO JSON: {{"historias": [{{"titulo": "", "resumen": "", "clave": "", "compra": "", "fuentes": [0]}}]}}

NOTICIAS:
{items}"""

INTRO_PROMPT = """Eres editora de VIBRA (revista digital de moda, música y cine en español).
Con estos titulares de la semana, escribe:
- "titular": un titular de portada para la edición (máx. 70 caracteres)
- "intro": "La semana en 30 segundos", un párrafo cercano y con chispa de 50-70 palabras que conecte lo más destacado de moda, música y cine.
No inventes nada que no esté en los titulares. Devuelve SOLO JSON: {{"titular": "", "intro": ""}}

TITULARES:
{items}"""


def ai_section(name, candidates, n, key, models):
    listing = "\n".join(f"[{i}] ({c['source']}) {c['title']} — {c['summary'][:280]}" for i, c in enumerate(candidates))
    data = gemini(SECTION_PROMPT.format(section=name, n=n, items=listing), key, models)
    stories = []
    for s in (data or {}).get("historias", [])[:n]:
        idxs = [i for i in s.get("fuentes", []) if isinstance(i, int) and 0 <= i < len(candidates)]
        if not idxs or not s.get("titulo") or not s.get("resumen"):
            continue
        stories.append({
            "title": s["titulo"].strip(),
            "summary": s["resumen"].strip(),
            "why": (s.get("clave") or "").strip(),
            "shop": (s.get("compra") or "").strip()[:60],
            "sources": [{"name": candidates[i]["source"], "url": candidates[i]["link"]} for i in dict.fromkeys(idxs)],
            "image": next((candidates[i]["image"] for i in idxs if candidates[i].get("image")), ""),
            "image_credit": next((candidates[i]["source"] for i in idxs if candidates[i].get("image")), ""),
            "ai": True,
            "_ctx": [candidates[i] for i in dict.fromkeys(idxs)],
        })
    return stories


ARTICLE_PROMPT = """Eres redactora jefa de VIBRA, una revista digital en español de moda, música y cine para un público joven (18-35 años).
Escribe un artículo propio, largo y atractivo, sobre cada una de las historias de la sección "{section}" que tienes abajo.

Cómo tiene que ser cada artículo:
- Entre 600 y 900 palabras, en español de España, con tono cercano, vivo y periodístico, como en Vogue, Rolling Stone o Fotogramas.
- Empieza con un primer párrafo gancho que atrape desde la primera frase.
- Organízalo en 3 o 4 bloques, cada uno con un subtítulo corto y llamativo (máx. 8 palabras) y 2 o 3 párrafos.
- Cuenta qué ha pasado con detalle, da contexto (trayectoria, antecedentes, tendencias relacionadas), explica por qué importa y qué se puede esperar a continuación.
- Termina con un párrafo de cierre que invite a opinar o a seguir la historia.
- "destacado": una frase potente del propio artículo para destacar (máx. 25 palabras). No pongas citas entre comillas atribuidas a nadie.

Reglas de rigor:
- Los hechos concretos (fechas, cifras, nombres, declaraciones) solo pueden salir del material de referencia. Como contexto puedes añadir datos de cultura general ampliamente conocidos y ciertos; si dudas de algo, no lo pongas.
- No inventes citas. Escribe con tus propias palabras: no copies ni traduzcas literalmente frases del material.
- Puedes atribuir información al medio original (p. ej., "según Variety"). No hables de ti misma ni del "material".
- Sin markdown ni emojis.

HISTORIAS:
{items}"""

ARTICLE_SCHEMA = {
    "type": "OBJECT",
    "properties": {"articulos": {"type": "ARRAY", "items": {
        "type": "OBJECT",
        "properties": {
            "id": {"type": "INTEGER"},
            "destacado": {"type": "STRING"},
            "bloques": {"type": "ARRAY", "items": {
                "type": "OBJECT",
                "properties": {"subtitulo": {"type": "STRING"},
                               "parrafos": {"type": "ARRAY", "items": {"type": "STRING"}}},
                "required": ["parrafos"]}},
        },
        "required": ["id", "bloques"]}}},
    "required": ["articulos"],
}


def ai_articles(name, stories, key, models, batch=2):
    done = 0
    for start in range(0, len(stories), batch):
        chunk = stories[start:start + batch]
        blocks = []
        for i, s in enumerate(chunk):
            refs = "\n".join(f"Material de {c['source']}: {page_text(c['link']) or c['summary']}" for c in s.get("_ctx", []))
            blocks.append(f"### [{i}] {s['title']}\nResumen: {s['summary']}\n{refs}")
        data = gemini(ARTICLE_PROMPT.format(section=name, items="\n\n".join(blocks)), key, models, ARTICLE_SCHEMA) or {}
        for a in data.get("articulos", []):
            i = a.get("id")
            if not isinstance(i, int) or not 0 <= i < len(chunk):
                continue
            body = []
            for b in a.get("bloques") or []:
                sub = re.sub(r"[*#_]+", "", b.get("subtitulo") or "").strip()
                paras = [re.sub(r"\*\*|__", "", p).strip() for p in b.get("parrafos") or [] if isinstance(p, str) and p.strip()]
                if paras:
                    body += ([f"## {sub}"] if sub and body else []) + paras
            words = sum(len(p.split()) for p in body if not p.startswith("## "))
            if words >= 250:
                chunk[i]["body"] = body
                chunk[i]["highlight"] = (a.get("destacado") or "").strip().strip('"«»')
                done += 1
            else:
                log(f"  ! artículo demasiado corto ({words} palabras): {chunk[i]['title'][:60]}")
    log(f"  · artículos propios: {done}/{len(stories)}")


# ---------------------------------------------------------------- edición
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
    n = cfg["ai"]["items_per_section"]
    country = cfg["charts"]["country"]

    for sid, sec in cfg["sections"].items():
        log(f"Sección {sec['title']}")
        items = [it for f in sec["feeds"] for it in fetch_feed(f, since)]
        ranked = rank_items(items)
        candidates = diverse_pick(ranked, 40, per_source=8)
        stories = ai_section(sec["title"], candidates, n, gkey, cfg["ai"]["models"]) if gkey and candidates else []
        if len(stories) < 3:
            if gkey:
                log("  ! IA no disponible: uso modo sin IA")
            stories = plain_section(candidates, n)
        else:
            ai_articles(sec["title"], stories, gkey, cfg["ai"]["models"])
        for s in stories:
            s.pop("_ctx", None)
        fill_images(stories)
        edition["sections"][sid] = {"title": sec["title"], "stories": stories, "headlines": [
            {"title": c["title"], "source": c["source"], "url": c["link"], "image": c.get("image", "")} for c in diverse_pick(ranked, 8, per_source=2)]}

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
        edition["headline"] = intro.get("titular", "")
        edition["intro"] = intro.get("intro", "")
    return edition


# ---------------------------------------------------------------- web
def light(score, votes):
    if votes < 20:
        return "gris"
    return "verde" if score >= 7.3 else "ambar" if score >= 6 else "rojo"


MONTHS = "enero febrero marzo abril mayo junio julio agosto septiembre octubre noviembre diciembre".split()


def nice_date(iso):
    d = dt.date.fromisoformat(iso[:10])
    return f"{d.day} de {MONTHS[d.month - 1]} de {d.year}"


def slugify(text):
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")[:70].rstrip("-") or "articulo"


def assign_slugs(edition):
    seen = set()
    for sec in edition["sections"].values():
        for s in sec.get("stories", []):
            slug = base = slugify(s["title"])
            n = 2
            while slug in seen:
                slug, n = f"{base}-{n}", n + 1
            seen.add(slug)
            s["slug"] = slug


def render(cfg):
    editions = sorted((json.loads(p.read_text()) for p in DATA.glob("*.json")), key=lambda e: e["date"], reverse=True)
    if not editions:
        sys.exit("No hay ediciones todavía. Ejecuta sin --render primero.")
    for e in editions:
        assign_slugs(e)
    base_url = (cfg["site"].get("base_url") or os.environ.get("SITE_URL") or "").rstrip("/")
    money = cfg.get("monetization", {})
    env = Environment(loader=FileSystemLoader(ROOT / "templates"), autoescape=select_autoescape(["html", "xml", "webmanifest"]))
    env.filters["date"] = nice_date
    env.globals.update(site=cfg["site"], money=money, light=light, base_url=base_url,
                       year=dt.date.today().year, quote=quote_plus)

    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir()
    shutil.copytree(ROOT / "static", OUT / "static")

    def write(path, template, depth, **ctx):
        dest = OUT / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(env.get_template(template).render(root="../" * depth or "./", path=path, **ctx), encoding="utf-8")

    write("index.html", "edition.html", 0, e=editions[0], latest=True)
    for e in editions:
        write(f"semana/{e['id']}/index.html", "edition.html", 2, e=e, latest=False)
        flat = [(sid, sec, s) for sid, sec in e["sections"].items() for s in sec.get("stories", [])]
        for sid, sec, s in flat:
            related = [x for x in flat if x[0] == sid and x[2] is not s] + [x for x in flat if x[0] != sid]
            write(f"semana/{e['id']}/{s['slug']}/index.html", "article.html", 3,
                  e=e, sid=sid, sec=sec, s=s, related=related[:6])
    write("archivo/index.html", "archive.html", 1, editions=editions)
    for page in ("aviso-legal", "privacidad", "cookies", "sobre-vibra"):
        write(f"{page}/index.html", f"{page}.html", 1)
    write("rss.xml", "rss.xml", 0, editions=editions[:20])
    write("manifest.webmanifest", "manifest.webmanifest", 0)
    write("sw.js", "sw.js", 0, build_id=dt.datetime.now().strftime("%Y%m%d%H%M%S"))
    write("sitemap.xml", "sitemap.xml", 0, editions=editions)
    (OUT / "robots.txt").write_text(f"User-agent: *\nAllow: /\n" + (f"Sitemap: {base_url}/sitemap.xml\n" if base_url else ""))
    if money.get("adsense_client"):
        pub = money["adsense_client"].replace("ca-", "")
        (OUT / "ads.txt").write_text(f"google.com, {pub}, DIRECT, f08c47fec0942fa0\n")
    (OUT / ".nojekyll").write_text("")
    if (ROOT / "CNAME").exists():
        shutil.copy(ROOT / "CNAME", OUT / "CNAME")
    log(f"Web generada en {OUT} ({len(editions)} ediciones)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--render", action="store_true", help="solo regenerar la web")
    args = ap.parse_args()
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    if not args.render:
        edition = make_edition(cfg)
        DATA.mkdir(parents=True, exist_ok=True)
        (DATA / f"{edition['id']}.json").write_text(json.dumps(edition, ensure_ascii=False, indent=1), encoding="utf-8")
        log(f"Edición {edition['id']} guardada")
    render(cfg)


if __name__ == "__main__":
    main()
