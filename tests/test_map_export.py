"""The map's data: one point per flat, placed exactly or near its barri."""
import json

from scheduler.map_export import to_points, write


def _flat(uid, lat=None, lng=None, **kw):
    base = {"id": uid, "portal": "habitaclia", "price": 250000.0, "area_m2": 75.0, "rooms": 3,
            "floor": "2ª planta", "url": f"https://h/{uid}/d", "city": "barcelona",
            "district": "navas", "latitude": lat, "longitude": lng, "_sent": False,
            "first_seen_at": "2026-11-03 09:00:00"}
    base.update(kw)
    return base


def test_a_flat_with_a_position_keeps_it():
    pts = to_points([_flat("a", 41.4161, 2.1866)])
    assert pts[0]["lat"] == 41.4161 and pts[0]["lng"] == 2.1866 and not pts[0]["ap"]
    assert pts[0]["t"] == "Barcelona" and pts[0]["b"] == "Navas"


def test_a_flat_without_one_sits_in_its_barri_marked_approximate():
    pts = to_points([_flat("a", 41.4161, 2.1866), _flat("b", price=230000.0)])
    approx = next(p for p in pts if p["ap"])
    assert abs(approx["lat"] - 41.4161) < 0.01 and abs(approx["lng"] - 2.1866) < 0.01


def test_a_flat_nowhere_to_place_or_with_a_plot_for_area_is_left_off():
    assert to_points([_flat("x", city="tordera", district="centre")]) == []
    assert to_points([_flat("y", 41.4, 2.1, area_m2=640.0)]) == []


def test_the_file_says_when_it_was_written(tmp_path):
    path = tmp_path / "docs" / "pisos.json"
    write(to_points([_flat("a", 41.4161, 2.1866)]), str(path))
    data = json.loads(path.read_text())
    assert data["updated"] and len(data["flats"]) == 1
