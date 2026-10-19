from pathlib import Path

import pytest
import yaml

from honk.actions import ActionType

ROOT = Path(__file__).resolve().parent.parent / "scenarios"
FILES = sorted((ROOT / "real").glob("*.yaml")) + sorted((ROOT / "demo").glob("*.yaml"))
REQUIRED = {"id", "title", "source", "reproduce", "tempting_action", "helps", "real_fix",
            "reviewed_by"}


def test_there_are_scenarios() -> None:
    assert len(FILES) >= 20


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.stem)
def test_scenario_is_well_formed(path: Path) -> None:
    s = yaml.safe_load(path.read_text())
    assert set(s) >= REQUIRED, REQUIRED - set(s)
    assert s["id"] == path.stem
    assert s["tempting_action"] in {a.value for a in ActionType}
    assert s["helps"] in (True, False, "temporarily", "partly")
    if s["source"]["kind"] != "demo":
        assert s["source"]["url"].startswith("https://")
    if s["reproduce"]:
        manifest = s["reproduce"].split("#")[0]
        assert (path.parent / manifest).exists(), manifest
