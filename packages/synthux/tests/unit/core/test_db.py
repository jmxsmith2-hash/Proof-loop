from synthux.core.db import Database, new_id, utcnow


def test_migrations_create_tables_once(tmp_path):
    db = Database(tmp_path / "a.sqlite")
    assert {"personas", "runs", "sessions", "steps", "issues", "issue_clusters", "llm_cache",
            "calibration_profiles", "real_user_issues", "sensor_findings"} <= db.tables()
    assert db.migrate() == []  # nothing new on second call
    db.close()
    again = Database(tmp_path / "a.sqlite")
    assert again.migrate() == []
    again.close()


def test_json_columns_round_trip(db):
    pid = new_id("p")
    db.insert("personas", {"id": pid, "created_at": utcnow(), "label": "x",
                           "traits_json": {"tech_skill": 2, "tags": ["a"]}, "narrative": "n"})
    row = db.one("SELECT * FROM personas WHERE id = ?", [pid])
    assert row["traits_json"] == {"tech_skill": 2, "tags": ["a"]}
    db.update("personas", "id", pid, {"label": "y"})
    assert db.one("SELECT label FROM personas WHERE id = ?", [pid])["label"] == "y"
