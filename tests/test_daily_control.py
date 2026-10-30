"""The morning control: what became of yesterday's flats."""
from scheduler.jobs import PipelineOrchestrator

text = PipelineOrchestrator.daily_control_text


def test_a_clean_day_says_so_in_the_title():
    report = {"day": "2026-10-29", "escaped": [], "portals": {
        "habitaclia": {"nuevos": 301, "cumplen": 15, "enviados": 2, "repetidos": 13,
                       "descartados": 0, "escapados": 0}}}
    out = text(report).splitlines()
    assert out[0] == "Control del 29/10: todo en orden"
    assert out[1] == "Habitaclia: 301 nuevos · 15 cumplían → 2 enviados, 13 repetidos"
    assert "Idealista: no entró nada nuevo" in out


def test_a_miss_is_named_with_its_link():
    miss = {"price": 207000, "district": "can vidalet", "city": "esplugues de llobregat",
            "area_m2": 72.0, "url": "https://www.habitaclia.com/x/d"}
    report = {"day": "2026-10-29", "escaped": [miss], "portals": {
        "habitaclia": {"nuevos": 10, "cumplen": 1, "enviados": 0, "repetidos": 0,
                       "descartados": 0, "escapados": 1}}}
    out = text(report)
    assert out.startswith("Control del 29/10: se escaparon 1")
    assert "1 se escaparon" in out
    assert "⚠️ 207.000 € · Can Vidalet · 72.0 m² · https://www.habitaclia.com/x/d" in out
