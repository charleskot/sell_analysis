from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from pathlib import Path
from typing import List

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.config import settings

log = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=select_autoescape(["html", "j2"]),
)


def render_email(opportunities: List[dict], stats: dict, run_date: str) -> str:
    template = _env.get_template("email.html.j2")
    return template.render(
        opportunities=opportunities,
        stats=stats,
        run_date=run_date,
        min_score=settings.min_opportunity_score,
    )


def send_email(subject: str, html: str) -> None:
    if not (settings.smtp_host and settings.smtp_to and settings.smtp_from):
        log.warning("SMTP no configurado completamente. No se envía email.")
        return

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.smtp_from
    msg["To"] = settings.smtp_to
    msg.set_content("Este email requiere un cliente compatible con HTML.")
    msg.add_alternative(html, subtype="html")

    log.info("Enviando email a %s vía %s:%s", settings.smtp_to, settings.smtp_host, settings.smtp_port)
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as s:
        if settings.smtp_use_tls:
            s.starttls()
        if settings.smtp_user and settings.smtp_password:
            s.login(settings.smtp_user, settings.smtp_password)
        s.send_message(msg)
    log.info("Email enviado.")
