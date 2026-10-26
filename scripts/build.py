    for sid, sec in cfg["sections"].items():
        log(f"Sección {sec['title']}")
        items = [it for f in sec["feeds"] for it in fetch_feed(f, since)]
        ranked = rank_items(items)
        candidates = diverse_pick(ranked, 40, per_source=8)
        stories = []
        if gkey and candidates and models:
            stories = ai_section(sec["title"], candidates, n, gkey, models)
        if not stories:
            if gkey:
                log("  ! IA no disponible: uso modo sin IA")
            stories = plain_section(candidates, n)
        elif gkey and models:
            ai_articles(sec["title"], stories, gkey, models)
        for s in stories:
            s.pop("_ctx", None)
        fill_images(stories)
        edition["sections"][sid] = {"title": sec["title"], "stories": stories, "headlines": [
            {"title": c["title"], "source": c["source"], "url": c["link"], "image": c.get("image", "")} for c in diverse_pick(ranked, 8, per_source=2)]}
