# VIBRA

Revista digital semanal y automática de moda, música y cine. Guía en español: [GUIA.md](GUIA.md).

```bash
pip install -r requirements.txt
GEMINI_API_KEY=... TMDB_API_KEY=... python scripts/build.py   # nueva edición + web en site/
python scripts/build.py --render                               # solo regenerar la web
```
