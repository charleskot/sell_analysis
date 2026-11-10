"""Bank alerts name the street; their links name the town."""
import pytest

import models.db as models_db
from ingest.email_parsers import town_from_url


@pytest.mark.parametrize("portal,url,town", [
    ("solvia", "http://www.solvia.es/es/propiedades/comprar/Piso-Barcelona-2-dormitorio-105551-134515",
     "barcelona"),
    ("solvia", "http://www.solvia.es/es/propiedades/comprar/Chalet-adosado-Rubi-2-dormitorio-155754-193083",
     "rubi"),
    ("solvia", "http://www.solvia.es/es/propiedades/comprar/Piso-Barbera-del-Valles-2-dormitorio-1-2",
     "barbera del valles"),
    ("servihabitat",
     "https://www.servihabitat.com/es/venta/vivienda-piso/barcelona-baixllobregat-santboidellobregat/60445535",
     "sant boi de llobregat"),
    ("servihabitat",
     "https://www.servihabitat.com/es/venta/vivienda-piso/barcelona-barcelones-lhospitaletdellobregat/1234567",
     "hospitalet de llobregat"),
    ("habitaclia", "https://www.habitaclia.com/comprar-piso-barcelona-i1.htm", None),
])
def test_the_town_comes_from_the_link(portal, url, town):
    assert town_from_url(portal, url) == town


@pytest.fixture
def db(tmp_path):
    models_db.init_engine(str(tmp_path / "t.db"))
    models_db.create_all_tables()
    yield models_db
    models_db._engine = None


def test_stored_bank_flats_are_refiled_once(db):
    from types import SimpleNamespace
    from scheduler.jobs import PipelineOrchestrator

    db.upsert_listing({"portal": "solvia", "external_id": "105551",
                       "url": "http://www.solvia.es/es/propiedades/comprar/Piso-Barcelona-2-dormitorio-105551-134515",
                       "title": "Piso en C/ Clot - Barcelona -", "price": 340000.0,
                       "city": "clot", "district": "barcelona"})
    repair = PipelineOrchestrator.repair_bank_towns.__get__(SimpleNamespace())
    assert repair() == 1
    from sqlalchemy import select
    from models.schema import listings
    with db.get_engine().connect() as conn:
        row = conn.execute(select(listings)).fetchone()
    assert (row.city, row.district) == ("barcelona", None)
    assert repair() == 0                    # once only
