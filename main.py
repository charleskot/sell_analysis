"""Entry point: CLI interface for the Real Estate Investment Analyzer."""
import logging
import sys
from pathlib import Path

import typer
import yaml
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table
from rich import print as rprint

load_dotenv()

app = typer.Typer(help="Analizador de Inversión Inmobiliaria España")
console = Console()


def load_config() -> dict:
    config_path = Path(__file__).parent / "config.yaml"
    with open(config_path) as f:
        return yaml.safe_load(f)


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("sell_analysis.log"),
        ],
    )


def init_db(config: dict) -> None:
    from models.db import init_engine, create_all_tables
    db_path = config["database"]["path"]
    if not Path(db_path).is_absolute():
        db_path = str(Path(__file__).parent / db_path)
    init_engine(db_path)
    create_all_tables()


@app.command()
def scrape(
    portal: str = typer.Option("all", help="Portal a scrapear: all, idealista, fotocasa, pisos, habitaclia"),
    once: bool = typer.Option(False, "--once", help="Ejecutar una sola vez y salir (sin scheduler)"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Logging detallado"),
):
    """Ejecuta el scraper. Por defecto arranca el scheduler automático."""
    setup_logging(verbose)
    config = load_config()
    init_db(config)

    # Filter portals if specific portal requested
    if portal != "all":
        for p in config["portals"]:
            if p != portal:
                config["portals"][p]["enabled"] = False

    from scheduler.jobs import PipelineOrchestrator, create_scheduler

    if once:
        console.print(f"[cyan]Ejecutando scraper (modo único)...[/cyan]")
        orchestrator = PipelineOrchestrator(config)
        stats = orchestrator.run_purchase_scrape()
        console.print(f"[green]Completado:[/green] {stats['new']} nuevos, {stats['updated']} actualizados, {stats['errors']} errores")
    else:
        console.print("[cyan]Iniciando scheduler...[/cyan]")
        scheduler, orchestrator = create_scheduler(config)

        # Run immediately on start
        console.print("[cyan]Ejecutando primera pasada...[/cyan]")
        try:
            stats = orchestrator.run_purchase_scrape()
            console.print(f"[green]Primera pasada:[/green] {stats}")
        except Exception as e:
            console.print(f"[red]Error en primera pasada:[/red] {e}")

        console.print(f"[green]Scheduler activo. Próximo scraping en {config['scheduler']['scrape_interval_hours']}h[/green]")
        console.print("Presiona Ctrl+C para detener.")
        try:
            scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            console.print("[yellow]Scheduler detenido.[/yellow]")


@app.command()
def dashboard():
    """Lanza el dashboard de Streamlit."""
    import subprocess
    dashboard_path = Path(__file__).parent / "dashboard" / "app.py"
    console.print("[cyan]Iniciando dashboard en http://localhost:8501[/cyan]")
    subprocess.run(["streamlit", "run", str(dashboard_path)], check=True)


@app.command()
def init_db_cmd():
    """Inicializa la base de datos."""
    config = load_config()
    init_db(config)
    console.print("[green]Base de datos inicializada correctamente.[/green]")


@app.command()
def export(
    output: str = typer.Option("listings_export.csv", help="Ruta del archivo CSV de salida"),
    min_score: float = typer.Option(0, help="Score mínimo para exportar"),
):
    """Exporta los anuncios con métricas a CSV."""
    import pandas as pd
    from sqlalchemy import select
    from models.schema import listings, investment_metrics

    config = load_config()
    init_db(config)

    from models.db import get_engine
    engine = get_engine()

    with engine.connect() as conn:
        query = (
            select(
                listings.c.id,
                listings.c.portal,
                listings.c.url,
                listings.c.price,
                listings.c.area_m2,
                listings.c.price_per_m2,
                listings.c.rooms,
                listings.c.condition,
                listings.c.city,
                listings.c.district,
                listings.c.first_seen_at,
                investment_metrics.c.gross_yield_pct,
                investment_metrics.c.net_yield_pct,
                investment_metrics.c.estimated_monthly_rent,
                investment_metrics.c.payback_years,
                investment_metrics.c.investment_score,
            )
            .join(investment_metrics, listings.c.id == investment_metrics.c.listing_id, isouter=True)
            .where(listings.c.is_active == True)
        )
        if min_score > 0:
            query = query.where(investment_metrics.c.investment_score >= min_score)

        rows = conn.execute(query.order_by(investment_metrics.c.investment_score.desc())).fetchall()

    if not rows:
        console.print("[yellow]No hay datos para exportar.[/yellow]")
        return

    df = pd.DataFrame(rows)
    df.to_csv(output, index=False)
    console.print(f"[green]Exportados {len(df)} anuncios a {output}[/green]")


@app.command()
def show_top(
    n: int = typer.Option(20, help="Número de mejores oportunidades a mostrar"),
    city: str = typer.Option("", help="Filtrar por ciudad"),
    min_yield: float = typer.Option(0, help="Rentabilidad bruta mínima (%)"),
):
    """Muestra las mejores oportunidades de inversión en la terminal."""
    from sqlalchemy import select, and_
    from models.schema import listings, investment_metrics

    config = load_config()
    init_db(config)

    from models.db import get_engine
    engine = get_engine()

    with engine.connect() as conn:
        conditions = [listings.c.is_active == True]
        if city:
            conditions.append(listings.c.city == city.lower())
        if min_yield > 0:
            conditions.append(investment_metrics.c.gross_yield_pct >= min_yield)

        rows = conn.execute(
            select(
                listings.c.city,
                listings.c.district,
                listings.c.price,
                listings.c.area_m2,
                listings.c.rooms,
                listings.c.condition,
                investment_metrics.c.gross_yield_pct,
                investment_metrics.c.net_yield_pct,
                investment_metrics.c.estimated_monthly_rent,
                investment_metrics.c.payback_years,
                investment_metrics.c.investment_score,
                listings.c.url,
            )
            .join(investment_metrics, listings.c.id == investment_metrics.c.listing_id)
            .where(and_(*conditions))
            .order_by(investment_metrics.c.investment_score.desc())
            .limit(n)
        ).fetchall()

    if not rows:
        console.print("[yellow]No hay datos. Ejecuta primero: python main.py scrape --once[/yellow]")
        return

    table = Table(title=f"Top {n} Oportunidades de Inversión", show_lines=True)
    table.add_column("Score", style="bold green", width=6)
    table.add_column("Ciudad/Barrio", width=20)
    table.add_column("Precio", width=12)
    table.add_column("m²/Hab", width=8)
    table.add_column("Rent.Bruta", width=10)
    table.add_column("Rent.Neta", width=9)
    table.add_column("Alq/mes", width=9)
    table.add_column("Payback", width=8)
    table.add_column("Estado", width=12)

    for row in rows:
        city_str = f"{(row[0] or '').title()}"
        if row[1]:
            city_str += f"\n{row[1].title()}"
        score = row[10] or 0
        score_style = "green" if score >= 70 else "yellow" if score >= 50 else "red"

        table.add_row(
            f"[{score_style}]{score:.0f}[/{score_style}]",
            city_str,
            f"{(row[2] or 0):,.0f}€",
            f"{(row[3] or 0):.0f}m²/{row[4] or '?'}h",
            f"{(row[6] or 0):.1f}%",
            f"{(row[7] or 0):.1f}%",
            f"{(row[8] or 0):,.0f}€",
            f"{(row[9] or 0):.0f}a",
            row[5] or "—",
        )

    console.print(table)


if __name__ == "__main__":
    app()
