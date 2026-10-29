# VIBRA: guía sencilla

## Qué hace sola cada semana
Cada **lunes a las 08:00 (hora UTC; 10:00 en España en verano, 09:00 en invierno)**, GitHub ejecuta el programa de VIBRA, que:
1. Lee las últimas noticias de Vogue, Elle, Harper's Bazaar, Telva, WWD, Rolling Stone, Billboard, Pitchfork, Jenesaispop, Mondo Sonoro, Fotogramas, Espinof, SensaCine, Variety e IndieWire.
2. Elige los temas de los que más se habla.
3. Con Gemini (si `GEMINI_API_KEY` está configurada) crea titulares, resúmenes y artículos **propios** en español, siempre con enlace a las fuentes.
4. Añade el top de álbumes y canciones en España (Apple Music) y las películas y series con su nota (TMDB).
5. Publica la nueva edición, guarda la anterior en el **Archivo** y actualiza el RSS y el mapa del sitio para Google.

Tú no tienes que hacer nada. Si quieres lanzar una edición a mano: GitHub → pestaña **Actions** → "Edición semanal" → **Run workflow**.

## Cómo cambiar cosas (sin programar)
Todo se cambia en el archivo **config.yaml** (en GitHub: abre el archivo → icono del lápiz → "Commit changes"). La web se regenera sola en 1–2 minutos.
- Nombre y lema: `site.name`, `site.tagline`
- Añadir o quitar revistas: listas `feeds` de cada sección
- Email de contacto: `site.contact_email` (recomendado; aparece en el aviso legal)

## Cómo ganar dinero (en este orden)
| Paso | Cuándo | Qué hacer | Qué poner en config.yaml |
|---|---|---|---|
| 1. Amazon Afiliados | Ya | Regístrate en afiliados.amazon.es | `amazon_tag: "tuetiqueta-21"` → aparece el bloque "Cómo llevar las tendencias" |
| 2. Newsletter | Ya | Crea una newsletter gratis en beehiiv.com o substack.com y copia el enlace de suscripción | `newsletter_url: "..."` → aparecen los botones "Suscríbete" |
| 3. Apple Música afiliados | Opcional | Apple Services Performance Partners | `apple_affiliate_token: "..."` |
| 4. Google AdSense | Cuando tengas dominio propio y unas semanas de ediciones | adsense.google.com → añade tu web → activa el mensaje de consentimiento de Google (obligatorio en Europa) | `adsense_client: "ca-pub-..."` → anuncios entre secciones + ads.txt automático |

**Importante sobre AdSense:** Google normalmente **no aprueba webs en direcciones gratuitas** tipo `nombre.github.io`; pide un dominio propio. Por eso el plan es: empezar gratis, ganar con afiliados y newsletter, y cuando haya tráfico comprar el dominio (~10 €/año) y pedir AdSense.

## Cuando compres el dominio
1. Compra el dominio (Namecheap, Cloudflare, DonDominio…).
2. En GitHub: Settings → Pages → Custom domain → escribe tu dominio → Save. GitHub te dice qué 4 registros poner en tu proveedor de dominio.
3. Crea en el repositorio un archivo `CNAME` con tu dominio dentro (ej. `vibra.es`).
4. Pon el dominio en `site.base_url` de config.yaml.
Todo lo demás sigue igual y gratis.

## Para crecer
- Comparte cada lunes en Instagram/TikTok el "Top 3" de cada sección con el enlace.
- Da de alta la web en Google Search Console (gratis) y envía `sitemap.xml`.
- Sé constante: Google tarda 3–6 meses en mandar visitas de verdad.

## Claves (secretos)
Se guardan en GitHub → Settings → Secrets and variables → Actions:
- `GEMINI_API_KEY`: resúmenes con IA (gratis en aistudio.google.com/apikey)
- `TMDB_API_KEY`: notas de cine y series (gratis en themoviedb.org)
Sin ellas la web sigue funcionando, pero con titulares y extractos en vez de resúmenes propios.
