from __future__ import annotations

import json
import logging
from typing import Optional

import anthropic
from pydantic import BaseModel, Field

from app.config import settings
from app.scrapers.base import AuctionItem

log = logging.getLogger(__name__)


class OpportunityAnalysis(BaseModel):
    """Structured opportunity analysis output from Claude."""
    opportunity_score: int = Field(ge=0, le=100, description="0-100 score; >=70 strong, <40 weak")
    estimated_market_value_eur: Optional[float] = Field(default=None, description="Realistic market value")
    estimated_discount_pct: Optional[float] = Field(default=None, description="(market - starting) / market * 100")
    estimated_rental_yield_pct: Optional[float] = Field(default=None, description="Annual gross rental yield")
    estimated_flip_roi_pct: Optional[float] = Field(default=None, description="ROI assuming light reform + 6-month hold")
    summary: str = Field(description="2-3 sentence executive summary in Spanish")
    pros: list[str] = Field(default_factory=list, description="Up to 5 pros, in Spanish")
    cons: list[str] = Field(default_factory=list, description="Up to 5 cons, in Spanish")
    legal_risks: list[str] = Field(default_factory=list, description="Cargas, ocupación, hipotecas previas, embargos. In Spanish")
    recommendation: str = Field(description='One of: "comprar", "investigar", "descartar"')


SYSTEM_PROMPT = """Eres un analista experto en subastas inmobiliarias en España, especializado en Cataluña y el área metropolitana de Barcelona. Tu tarea es evaluar oportunidades de compra a partir de listings de subastas (BOE, bancos, plataformas privadas).

Para cada inmueble debes:
1. Estimar el valor de mercado realista basándote en la ubicación, superficie, tipo y datos históricos generales que conozcas (€/m² típicos por zona).
2. Calcular el descuento implícito sobre el valor de mercado.
3. Estimar rentabilidades realistas:
   - Alquiler: yield bruto anual típico de la zona.
   - Flip (reforma + venta): ROI tras reforma ligera (10-15% del precio) y 6 meses de holding.
4. Identificar riesgos legales: cargas no canceladas, ocupación, deudas comunidad, hipotecas previas, embargos.
5. Asignar una puntuación de oportunidad 0-100:
   - 80-100: Oportunidad excepcional, descuento >35%, riesgos manejables, ubicación líquida.
   - 60-79: Buena oportunidad, descuento >20%, requiere due diligence.
   - 40-59: Marginal, descuento bajo o riesgos significativos.
   - 0-39: Descartar.

Sé conservador: ante datos faltantes, baja la puntuación y marca el item como "investigar". Devuelve siempre JSON válido conforme al schema."""


_client: Optional[anthropic.Anthropic] = None


def get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        if not settings.anthropic_api_key:
            raise RuntimeError("ANTHROPIC_API_KEY no configurada")
        _client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    return _client


def _build_user_prompt(item: AuctionItem) -> str:
    return (
        "Analiza esta subasta inmobiliaria:\n\n"
        + json.dumps(item.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
    )


def analyze(item: AuctionItem) -> OpportunityAnalysis:
    """Send one auction to Claude and return a structured analysis.

    Uses prompt caching on the system prompt (constant across all auctions in
    a daily run) so repeated calls only pay full price for the per-item user
    turn.
    """
    client = get_client()

    schema = OpportunityAnalysis.model_json_schema()

    response = client.messages.create(
        model=settings.analysis_model,
        max_tokens=2000,
        system=[{
            "type": "text",
            "text": SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": _build_user_prompt(item)}],
        thinking={"type": "adaptive"},
        output_config={
            "format": {"type": "json_schema", "schema": schema},
            "effort": "medium",
        },
    )

    text = next((b.text for b in response.content if b.type == "text"), None)
    if not text:
        raise RuntimeError("Claude no devolvió bloque de texto")

    log.debug(
        "Tokens — in:%s cached_read:%s cached_write:%s out:%s",
        response.usage.input_tokens,
        response.usage.cache_read_input_tokens,
        response.usage.cache_creation_input_tokens,
        response.usage.output_tokens,
    )

    return OpportunityAnalysis.model_validate_json(text)
