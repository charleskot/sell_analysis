"""A judge with the user's taste: reads the whole advert and looks at its photos.

"Hay pisos que parecen bien pero pintan muy feos, como muy para reformar.
Quiero que vayas aprendiendo lo que a mí me gusta, que mires la publicación
completa y las fotos, y que vayas definiendo tú ese piso ideal: para entrar a
vivir o con muy poca reforma, en calles relativamente tranquilas y bonitas."

The filters decide what is in budget, in a zone and big enough; this decides
whether he would want to live there. Every flat that passed the filters and
the occupation check is shown to Claude with its description and a spread of
its photos, and scored 0–10 against his taste. Below the threshold it is not
sent; the daily control lists it instead, so a wrong call is seen the next
morning rather than lost.

It learns from him. Every notification carries 👍 and 👎; the taps arrive on
a private ntfy topic, and the rated flats — with what the judge saw in them —
are shown to the judge as examples. Every few ratings the judge rewrites "the
ideal flat" in a dozen lines from them, and that text guides it from then on.

It fails open. With no API key, past the daily cap, or with the API down, a
flat goes out unjudged rather than being held: losing a good flat costs more
than reading a dull one.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")
# Habitaclia's adverts are served from Fotocasa's image store; the whole
# gallery is in the page's data, each photo once per size.
_FOTOCASA_IMG_RE = re.compile(r"https://static\.fotocasa\.es/images/ads/[0-9a-f-]{36}")

_VERDICTS_KEY = "juez_veredictos"
_IDEAL_KEY = "juez_piso_ideal"
_IDEAL_VOTES_KEY = "juez_piso_ideal_votos"
_IDEAL_SHOWN_KEY = "juez_piso_ideal_mostrado"
_NOTES_KEY = "juez_notas"
_SINCE_KEY = "juez_gustos_desde"
_VOTES_SINCE_KEY = "juez_votos_desde"
_FEEDBACK_NOTE = "ntfy"            # marks votes cast from the notification buttons

STATES = ["para entrar a vivir", "reforma ligera", "reforma importante", "no se puede saber"]
STREETS = ["tranquila", "normal", "ruidosa", "no se puede saber"]

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "puntuacion": {"type": "integer"},
        "estado": {"type": "string", "enum": STATES},
        "calle": {"type": "string", "enum": STREETS},
        "resumen": {"type": "string"},
        "pegas": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["puntuacion", "estado", "calle", "resumen", "pegas"],
    "additionalProperties": False,
}

IDEAL_SCHEMA = {
    "type": "object",
    "properties": {"piso_ideal": {"type": "string"}},
    "required": ["piso_ideal"],
    "additionalProperties": False,
}

DEFAULT_TASTE = (
    "Busco un piso para vivir yo, no para invertir. Para entrar a vivir o con "
    "muy poca reforma (pintar, algún detalle). No quiero pisos que pintan feos: "
    "viejos, oscuros, con cocina y baño antiguos que piden reforma integral, "
    "suelos y carpinterías de hace décadas, o fotos que esconden el estado real. "
    "Me gustan luminosos y exteriores. Prefiero zonas tranquilas y calles "
    "relativamente tranquilas y bonitas: mejor una calle pequeña, arbolada o "
    "peatonal que una avenida con mucho tráfico. Con ascensor o en planta "
    "primera o segunda. Nada de bajos ni sótanos."
)


def _cfg(config: dict) -> dict:
    return config.get("juez") or {}


def _state_json(key: str, default):
    from models.db import get_telegram_state

    try:
        raw = get_telegram_state(key, "")
        return json.loads(raw) if raw else default
    except Exception:
        return default


def _save_json(key: str, value) -> None:
    from models.db import set_telegram_state

    set_telegram_state(key, json.dumps(value, ensure_ascii=False))


def get_verdict(listing_id: str) -> dict | None:
    """The judge's stored verdict on a flat, if it has seen it."""
    return (_state_json(_VERDICTS_KEY, {}) or {}).get(listing_id) if listing_id else None


def _store_verdict(listing_id: str, verdict: dict, keep: int = 1500) -> None:
    data = _state_json(_VERDICTS_KEY, {})
    if not isinstance(data, dict):
        data = {}
    data[listing_id] = verdict
    if len(data) > keep:
        data = dict(sorted(data.items(), key=lambda kv: kv[1].get("at", ""))[-keep:])
    _save_json(_VERDICTS_KEY, data)


def _spread(items: list, n: int) -> list:
    """n items spread over the list: the first few, then evenly to the end.

    A gallery opens on the best room and ends on the bathroom and the
    staircase; the judge needs the whole of it, not three angles of the
    living room.
    """
    if len(items) <= n:
        return list(items)
    head = items[:2]
    rest = items[2:]
    k = n - len(head)
    step = len(rest) / k
    return head + [rest[int(i * step)] for i in range(k)]


def photo_urls(listing: dict, max_photos: int = 8) -> list[str]:
    """The flat's photos: from the advert page for Habitaclia, stored otherwise."""
    stored = listing.get("photo_urls") or []
    if isinstance(stored, str):
        try:
            stored = json.loads(stored)
        except ValueError:
            stored = []
    urls = [u for u in stored if isinstance(u, str) and u.startswith("http")]
    if listing.get("portal") == "habitaclia" or len(urls) < 3:
        page_urls = _gallery_from_page(listing.get("url") or "")
        if len(page_urls) > len(urls):
            urls = page_urls
    return _spread(list(dict.fromkeys(urls)), max_photos)


def _gallery_from_page(url: str) -> list[str]:
    if "habitaclia.com" not in url:
        return []
    try:
        import requests

        page = requests.get(url, headers={"User-Agent": _USER_AGENT}, timeout=20).text
    except Exception as e:
        logger.info(f"juez: no pude leer las fotos de {url[-60:]}: {e}")
        return []
    return [u + "?rule=original" for u in dict.fromkeys(_FOTOCASA_IMG_RE.findall(page))]


def _image_block(url: str, max_side: int = 800) -> dict | None:
    """Download one photo and shrink it: the judge needs the look, not the
    pixels, and a smaller photo costs a third of the tokens."""
    try:
        import requests

        resp = requests.get(url, headers={"User-Agent": _USER_AGENT}, timeout=20)
        if resp.status_code >= 300 or not resp.content:
            return None
        data = resp.content
        media = (resp.headers.get("content-type") or "").split(";")[0].strip()
        try:
            from io import BytesIO
            from PIL import Image

            img = Image.open(BytesIO(data))
            img = img.convert("RGB")
            img.thumbnail((max_side, max_side))
            out = BytesIO()
            img.save(out, format="JPEG", quality=80)
            data, media = out.getvalue(), "image/jpeg"
        except Exception:
            if media not in ("image/jpeg", "image/png", "image/webp", "image/gif"):
                return None
        if len(data) > 4_500_000:
            return None
        return {"type": "image", "source": {"type": "base64", "media_type": media,
                                            "data": base64.standard_b64encode(data).decode()}}
    except Exception as e:
        logger.info(f"juez: foto no descargada: {e}")
        return None


def rated_flats(limit: int = 60) -> list[dict]:
    """The flats he rated from the phone, newest first, his last word on each."""
    from sqlalchemy import select
    from models.db import session_scope
    from models.schema import feedback, listings

    try:
        with session_scope() as conn:
            rows = conn.execute(
                select(feedback.c.listing_id, feedback.c.verdict, feedback.c.created_at,
                       listings.c.price, listings.c.area_m2, listings.c.rooms,
                       listings.c.floor, listings.c.district, listings.c.city)
                .select_from(feedback.join(listings, feedback.c.listing_id == listings.c.id))
                .where(feedback.c.note == _FEEDBACK_NOTE)
                .order_by(feedback.c.created_at.desc(), feedback.c.id.desc())
            ).fetchall()
    except Exception as e:
        logger.info(f"juez: no pude leer las valoraciones guardadas: {e}")
        return []
    seen, out = set(), []
    for r in rows:
        if r.listing_id in seen:
            continue
        seen.add(r.listing_id)
        out.append(dict(r._mapping))
    return out[:limit]


def _facts(listing: dict) -> str:
    parts = []
    if listing.get("price"):
        parts.append(f"{listing['price']:,.0f} €".replace(",", "."))
    if listing.get("area_m2"):
        parts.append(f"{listing['area_m2']:.0f} m²")
    if listing.get("rooms") is not None:
        parts.append(f"{listing['rooms']} hab")
    if listing.get("floor"):
        parts.append(str(listing["floor"]))
    place = ", ".join(p for p in (listing.get("district"), listing.get("city")) if p)
    if place:
        parts.append(place.title())
    return " · ".join(parts)


class TasteJudge:
    def __init__(self, config: dict):
        self.config = config
        self.cfg = _cfg(config)
        self._client = None

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled")) and bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())

    @property
    def min_score(self) -> int:
        return int(self.cfg.get("min_score", 5))

    def _api(self):
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic(timeout=self.cfg.get("timeout_s", 120), max_retries=1)
        return self._client

    # ── Judging ──────────────────────────────────────────────────────────

    def judge(self, listing: dict, metrics: dict | None = None) -> dict | None:
        """The verdict on a flat: {"s", "ok", "estado", "calle", "r", "pegas"}.

        None when the flat could not be judged — no key, over the day's cap,
        an API failure — and the caller sends it as it would have before.
        """
        lid = listing.get("id") or f"{listing.get('portal')}_{listing.get('external_id')}"
        cached = get_verdict(lid)
        if cached:
            return cached
        if not self.enabled:
            return None
        from models.db import bump_daily, daily_counters

        if daily_counters().get("juez_llamadas", 0) >= self.cfg.get("max_per_day", 40):
            return None
        bump_daily("juez_llamadas")
        try:
            answer = self._ask(listing)
        except Exception as e:
            logger.warning(f"juez: no pude juzgar {lid}: {e}")
            return None
        if answer is None:
            return None
        score = max(0, min(10, int(answer.get("puntuacion", 0))))
        verdict = {
            "s": score,
            "ok": score >= self.min_score,
            "estado": answer.get("estado", ""),
            "calle": answer.get("calle", ""),
            "r": (answer.get("resumen") or "").strip()[:220],
            "pegas": [p.strip()[:120] for p in (answer.get("pegas") or [])][:5],
            "fotos": answer.get("_fotos", 0),
            "at": datetime.now(timezone.utc).isoformat(timespec="minutes"),
        }
        _store_verdict(lid, verdict)
        logger.info(f"juez: {lid} {score}/10 ({verdict['estado']}, calle {verdict['calle']}): {verdict['r']}")
        if not verdict["ok"]:
            bump_daily("juez_descarta")
        return verdict

    def _system(self) -> str:
        taste = (self.cfg.get("gustos") or DEFAULT_TASTE).strip()
        lines = [
            "Eres el ojo de un comprador de piso en Barcelona y alrededores. Los filtros "
            "ya han comprobado precio, zona, metros y planta; tú decides si a él le "
            "gustaría vivir ahí, mirando el anuncio entero y sus fotos.",
            "",
            "Lo que busca, con sus palabras:",
            taste,
        ]
        ideal = self.ideal_profile()
        if ideal:
            lines += ["", "Lo aprendido de sus valoraciones (su piso ideal):", ideal]
        notes = _state_json(_NOTES_KEY, [])
        if notes:
            lines += ["", "Cosas que ha dicho él:"] + [f"- {n}" for n in notes[-15:]]
        examples = self._examples()
        if examples:
            lines += ["", "Pisos que ya ha valorado (👍 le gustó, 👎 no):"] + examples
        lines += [
            "",
            "Puntúa de 0 a 10 cuánto encaja con él como casa para vivir:",
            "- 8–10: para entrar a vivir, bonito, luminoso, calle agradable.",
            "- 5–7: correcto; como mucho reforma ligera (pintar, suelos, algún detalle).",
            "- 0–4: pinta feo, viejo u oscuro, pide reforma importante (cocina y baño "
            "antiguos, instalaciones), interior sin luz, en avenida de mucho tráfico, o "
            "las fotos esconden el estado del piso.",
            "La calle: júzgala por la dirección y la zona si las conoces y por lo que se ve "
            "en las fotos (ventanas, vistas, fachada). Si no se puede saber, dilo.",
            "Si hay pocas fotos o no enseñan el piso, no lo hundas por eso solo: juzga con "
            "el texto y dilo en las pegas. Ante la duda, no lo bajes de 5: perder un "
            "buen piso es peor que enseñarle uno flojo.",
            "El resumen: una frase corta en español (máx. 120 caracteres), lo que verías "
            "tú de un vistazo, p. ej. «Reformado y luminoso, calle peatonal tranquila».",
        ]
        return "\n".join(lines)

    def _examples(self, limit: int = 16) -> list[str]:
        out = []
        for r in rated_flats(limit):
            v = get_verdict(r["listing_id"]) or {}
            seen = f" — {v['r']}" if v.get("r") else ""
            out.append(f"{'👍' if r['verdict'] == 'yes' else '👎'} {_facts(r)}{seen}")
        return out

    def _ask(self, listing: dict) -> dict | None:
        max_photos = self.cfg.get("max_photos", 8)
        blocks = [b for b in (_image_block(u, self.cfg.get("photo_px", 800))
                              for u in photo_urls(listing, max_photos)) if b]
        text = (
            f"Anuncio ({listing.get('portal')}): {listing.get('title') or ''}\n"
            f"Datos: {_facts(listing)}\n"
            f"Fotos adjuntas: {len(blocks)}\n\n"
            f"Descripción del vendedor:\n{(listing.get('description') or '').strip()[:6000]}"
        )
        response = self._api().messages.create(
            model=self.cfg.get("model", "claude-opus-5-5"),
            max_tokens=self.cfg.get("max_tokens", 8000),
            system=self._system(),
            messages=[{"role": "user", "content": blocks + [{"type": "text", "text": text}]}],
            output_config={"effort": self.cfg.get("effort", "low"),
                           "format": {"type": "json_schema", "schema": JUDGE_SCHEMA}},
        )
        if response.stop_reason in ("refusal", "max_tokens"):
            logger.warning(f"juez: respuesta incompleta ({response.stop_reason})")
            return None
        body = next((b.text for b in response.content if b.type == "text"), "")
        answer = json.loads(body)
        answer["_fotos"] = len(blocks)
        return answer

    # ── Learning ─────────────────────────────────────────────────────────

    @staticmethod
    def ideal_profile() -> str:
        from models.db import get_telegram_state

        return get_telegram_state(_IDEAL_KEY, "")

    @staticmethod
    def ideal_to_show() -> str:
        """The ideal flat, once each time it changes — for the daily control."""
        from models.db import get_telegram_state, set_telegram_state

        ideal = get_telegram_state(_IDEAL_KEY, "")
        if not ideal or get_telegram_state(_IDEAL_SHOWN_KEY, "") == ideal[:200]:
            return ""
        set_telegram_state(_IDEAL_SHOWN_KEY, ideal[:200])
        return ideal

    @staticmethod
    def _votes() -> int:
        return len(rated_flats(limit=100000)) + len(_state_json(_NOTES_KEY, []))

    def learn(self, force: bool = False) -> bool:
        """Rewrite the ideal flat from his ratings, every few new ones."""
        from models.db import get_telegram_state, set_telegram_state

        if not self.enabled:
            return False
        votes = self._votes()
        before = int(get_telegram_state(_IDEAL_VOTES_KEY, "0") or 0)
        if not force and votes - before < self.cfg.get("relearn_every", 3):
            return False
        examples = self._examples(limit=60)
        notes = _state_json(_NOTES_KEY, [])
        if not examples and not notes:
            return False
        prompt = "\n".join([
            "Lo que busca, con sus palabras:", (self.cfg.get("gustos") or DEFAULT_TASTE).strip(), "",
            "Lo que ha dicho después:", *[f"- {n}" for n in notes[-30:]], "",
            "Pisos que ha valorado (👍 le gustó, 👎 no), con lo que se vio en cada uno:",
            *examples, "",
            "Describe su piso ideal en 8–12 líneas cortas en español, de lo más a lo menos "
            "importante: estado, luz, estilo, calle y entorno, planta, lo que le echa para "
            "atrás. Solo lo que se deduce de sus palabras y valoraciones; si algo no está "
            "claro aún, no lo inventes.",
        ])
        try:
            response = self._api().messages.create(
                model=self.cfg.get("model", "claude-opus-5-5"),
                max_tokens=self.cfg.get("max_tokens", 8000),
                messages=[{"role": "user", "content": prompt}],
                output_config={"effort": self.cfg.get("effort", "low"),
                               "format": {"type": "json_schema", "schema": IDEAL_SCHEMA}},
            )
            if response.stop_reason in ("refusal", "max_tokens"):
                return False
            body = next((b.text for b in response.content if b.type == "text"), "")
            ideal = (json.loads(body).get("piso_ideal") or "").strip()
        except Exception as e:
            logger.warning(f"juez: no pude actualizar el piso ideal: {e}")
            return False
        if not ideal:
            return False
        set_telegram_state(_IDEAL_KEY, ideal[:3000])
        set_telegram_state(_IDEAL_VOTES_KEY, str(votes))
        logger.info("juez: piso ideal actualizado")
        return True


# ── Ratings from the phone ───────────────────────────────────────────────

def feedback_topic() -> str:
    """The private topic the 👍/👎 buttons post to: the alert topic's sibling.

    Derived from the secret topic, never written in this public repository.
    """
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    return f"{topic}-gustos" if topic else ""


def rating_actions(config: dict, listing_id: str, title: str = "") -> list[dict]:
    """The two buttons on a flat's notification.

    A tap posts to his own alert topic, so the phone shows a confirmation
    the moment he taps — "⭐ Guardado en favoritos" — rather than nothing:
    the first version posted to a topic he does not follow, the votes
    arrived, and to him the button "no hace nada". The vote rides in the
    confirmation as "[si <id>]", which is what the bot reads back.
    """
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic or not _cfg(config).get("feedback", True):
        return []
    server = ((config.get("alerts") or {}).get("ntfy") or {}).get("server", "https://ntfy.sh").rstrip("/")
    what = title or "este piso"
    like = (f"⭐ {what}\nGuardado en favoritos: seguiré su precio y te aviso si cambia."
            f"\n[si {listing_id}]")
    dislike = (f"👎 {what}\nAnotado que no te gusta: el juez aprenderá de ello."
               f"\n[no {listing_id}]")
    # Header values stay ASCII: the phone's HTTP client may refuse anything else
    return [
        {"action": "http", "label": "👍 Me gusta", "url": f"{server}/{topic}", "method": "POST",
         "headers": {"Title": "Guardado en favoritos", "Tags": "star", "Priority": "2"},
         "body": like, "clear": True},
        {"action": "http", "label": "👎 No me gusta", "url": f"{server}/{topic}", "method": "POST",
         "headers": {"Title": "Anotado", "Tags": "-1", "Priority": "2"},
         "body": dislike, "clear": True},
    ]


# "[si habitaclia_…]" in a confirmation; "si habitaclia_…" alone from the
# first version of the buttons
_VOTE_RE = re.compile(r"\[(si|sí|no) ([a-z]+_[\w-]+)\]\s*$|^\s*(si|sí|no)\s+([a-z]+_[\w-]+)\s*$", re.I)


def _read_topic(server: str, topic: str, since: str) -> list[dict]:
    import requests

    try:
        resp = requests.get(f"{server}/{topic}/json", params={"poll": "1", "since": since}, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        logger.info(f"juez: no pude leer las valoraciones: {e}")
        return []
    out = []
    for line in resp.text.splitlines():
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if msg.get("event") == "message":
            out.append(msg)
    return out


def _vote(text: str) -> tuple[str, str] | None:
    found = _VOTE_RE.search(text or "")
    if not found:
        return None
    word, listing_id = (found.group(1), found.group(2)) if found.group(1) else (found.group(3), found.group(4))
    return ("no" if word.lower() == "no" else "yes"), listing_id


def poll_ratings(config: dict) -> tuple[int, int]:
    """Read his taps, and anything he wrote about his taste.

    Taps come back on the alert topic as confirmations (and, from the first
    version of the buttons, on the ratings topic). A 👍 also keeps the flat
    as a favourite, whose price is followed from then on; a 👎 drops it.
    Free text on the ratings topic is kept as something he said. Returns
    (votes, notes) newly recorded.
    """
    from models.db import get_telegram_state, record_feedback, set_telegram_state
    from scheduler import favorites

    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        return 0, 0
    server = ((config.get("alerts") or {}).get("ntfy") or {}).get("server", "https://ntfy.sh").rstrip("/")
    votes = notes = 0
    saved_notes = _state_json(_NOTES_KEY, [])
    for name, since_key, notes_allowed in ((topic, _VOTES_SINCE_KEY, False),
                                           (feedback_topic(), _SINCE_KEY, True)):
        messages = _read_topic(server, name, get_telegram_state(since_key, "") or "72h")
        for msg in messages:
            text = (msg.get("message") or "").strip()
            vote = _vote(text)
            if vote:
                verdict, listing_id = vote
                record_feedback(listing_id, verdict, _FEEDBACK_NOTE)
                if verdict == "yes":
                    favorites.add(listing_id)
                else:
                    favorites.remove(listing_id)
                votes += 1
            elif text and notes_allowed:
                saved_notes.append(text[:300])
                notes += 1
        if messages and messages[-1].get("id"):
            set_telegram_state(since_key, messages[-1]["id"])
    if notes:
        _save_json(_NOTES_KEY, saved_notes[-40:])
    if votes or notes:
        logger.info(f"juez: {votes} valoraciones y {notes} notas nuevas")
    return votes, notes
