import json
from pathlib import Path

from modules.player_stress.core.root_cause_analyzer import RootCauseAnalyzer


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "player_stress_accuracy"


def _load_fixture(path: Path):
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _replay_fixture(case_data):
    analyzer = RootCauseAnalyzer(case_data.get("package_name", ""))
    for snapshot in case_data.get("baseline", []) or []:
        analyzer.record_baseline(dict(snapshot))
    for item in case_data.get("events", []) or []:
        analyzer.record_stutter_event(
            dict(item.get("snapshot", {}) or {}),
            str(item.get("top_consumers_raw", "") or ""),
        )
    return analyzer.get_summary()


def test_accuracy_golden_fixtures_are_well_formed():
    fixture_paths = sorted(FIXTURE_DIR.glob("*.json"))
    assert fixture_paths, "expected at least one player_stress accuracy fixture"
    for path in fixture_paths:
        data = _load_fixture(path)
        assert data.get("name"), f"{path.name} missing name"
        assert isinstance(data.get("baseline", []), list), f"{path.name} baseline must be a list"
        assert isinstance(data.get("events", []), list), f"{path.name} events must be a list"
        assert isinstance(data.get("expected", {}), dict), f"{path.name} expected must be an object"


def test_accuracy_replay_matches_golden_expectations():
    fixture_paths = sorted(FIXTURE_DIR.glob("*.json"))
    assert fixture_paths, "expected at least one player_stress accuracy fixture"

    for path in fixture_paths:
        case_data = _load_fixture(path)
        summary = _replay_fixture(case_data)
        expected = case_data["expected"]
        most_confident = summary.get("most_confident_cause") or {}
        diagnosis = summary.get("final_diagnosis") or {}
        evidence = most_confident.get("evidence") or {}

        assert most_confident.get("root_cause_type") == expected["most_confident_cause"], path.name
        assert diagnosis.get("evidence_level") == expected["evidence_level"], path.name
        assert (diagnosis.get("evidence_strength") or {}).get("level") == expected["evidence_strength_level"], path.name
        assert str(diagnosis.get("suspect_process", "") or "") == str(expected.get("suspect_process", "") or ""), path.name

        if "confidence_min" in expected:
            assert float(most_confident.get("confidence", 0) or 0.0) >= float(expected["confidence_min"]), path.name
        if "confidence_max" in expected:
            assert float(most_confident.get("confidence", 0) or 0.0) <= float(expected["confidence_max"]), path.name
        if "resource_only" in expected:
            assert bool(evidence.get("resource_only", False)) == bool(expected["resource_only"]), path.name


def test_accuracy_replay_covers_confirmed_risk_and_insufficient():
    levels = set()
    causes = set()
    for path in sorted(FIXTURE_DIR.glob("*.json")):
        summary = _replay_fixture(_load_fixture(path))
        diagnosis = summary.get("final_diagnosis") or {}
        levels.add(str(diagnosis.get("evidence_level", "") or ""))
        most_confident = summary.get("most_confident_cause") or {}
        causes.add(str(most_confident.get("root_cause_type", "") or ""))

    assert {"confirmed", "risk", "insufficient"}.issubset(levels)
    assert {"CPU_CONTENTION", "DECODER_STUCK", "UNKNOWN"}.issubset(causes)
