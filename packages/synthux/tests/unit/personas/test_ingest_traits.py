from pathlib import Path

from synthux.personas.ingest import load_source, load_sources
from synthux.personas.traits import derive_evidence

EX = Path(__file__).resolve().parents[3] / "examples"


def test_survey_csv_is_anonymized_and_traits_derived():
    records = load_source(EX / "survey.csv")
    assert len(records) == 6
    blob = " ".join(r.text + str(r.fields) for r in records)
    for leaked in ("Maria", "Lopez", "maria.lopez@example.com", "555-123-4567", "42 Elm Street"):
        assert leaked not in blob
    ev = derive_evidence(records)
    assert ev.distribution("device")["mobile"] >= 3
    assert ev.distribution("age_range")["65+"] == 1
    assert ev.distribution("eyesight")["low_vision"] >= 1
    assert any("compare plans" in g for g in ev.goals)


def test_text_and_markdown_sources():
    records, report = load_sources([EX / "reviews.txt", EX / "interview_notes.md"])
    assert len(records) == 7
    assert report.replaced.get("email") == 1 and report.replaced.get("name", 0) >= 1
    ev = derive_evidence(records)
    assert ev.distribution("motor")["limited_dexterity"] >= 1
    assert ev.distribution("mood")["distracted"] >= 1


def test_analytics_export_weights(tmp_path):
    f = tmp_path / "ga.csv"
    f.write_text("deviceCategory,sessions\nmobile,700\ndesktop,250\ntablet,50\n")
    records = load_source(f)
    assert [r.kind for r in records] == ["analytics"] * 3
    ev = derive_evidence(records)
    assert ev.distribution("device").most_common(1)[0] == ("mobile", 700.0)
