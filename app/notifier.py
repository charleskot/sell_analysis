from __future__ import annotations

from pathlib import Path
from typing import List

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.config import settings

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=select_autoescape(["html", "j2"]),
)


def render_email(opportunities: List[dict], stats: dict, run_date: str) -> str:
    """Render the HTML report. Used by the FastAPI /preview endpoint."""
    template = _env.get_template("email.html.j2")
    return template.render(
        opportunities=opportunities,
        stats=stats,
        run_date=run_date,
        min_score=settings.min_opportunity_score,
    )
