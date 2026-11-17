"""web/world.json: the land outline behind the map, and its third-party notice."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_world_json_is_a_single_path_in_the_map_projection():
    data = json.loads((ROOT / "web" / "world.json").read_text(encoding="utf-8"))
    assert set(data) == {"viewBox", "d"}
    assert data["viewBox"] == "0 0 720 360"
    d = data["d"]
    assert isinstance(d, str) and d.startswith("M") and len(d) > 1000
    assert re.fullmatch(r"[MLZ0-9. -]+", d), "only move, line, and close commands"
    numbers = [float(n) for n in re.findall(r"-?\d+(?:\.\d+)?", d)]
    xs, ys = numbers[0::2], numbers[1::2]
    assert len(xs) == len(ys)
    assert 0 <= min(xs) and max(xs) <= 720 and 0 <= min(ys) and max(ys) <= 360
    assert (ROOT / "web" / "world.json").stat().st_size < 64 * 1024


def test_third_party_notice_names_natural_earth():
    text = (ROOT / "THIRD-PARTY-LICENSES").read_text(encoding="utf-8")
    assert "Natural Earth" in text and "https://www.naturalearthdata.com/" in text and "public domain" in text
