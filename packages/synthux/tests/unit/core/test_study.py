import pytest

from synthux.core.study import Study, load_study
from synthux.safety.allowlist import SafetyError


def _write(tmp_path, url):
    p = tmp_path / "study.yaml"
    p.write_text(f"""
name: t
target:
  url: {url}
  credentials: {{password: "${{SX_PW:-pw123}}"}}
tasks:
  - id: a
    goal: do a thing
    start: /a
    inputs: {{password: "{{{{cred:password}}}}"}}
""", encoding="utf-8")
    return p


def test_study_loads_and_resolves_credentials(tmp_path, settings):
    study = load_study(_write(tmp_path, "http://127.0.0.1:8765"), settings)
    assert study.task_url(study.tasks[0]) == "http://127.0.0.1:8765/a"
    vault = study.vault()
    assert vault.resolve(study.tasks[0].inputs["password"]) == "pw123"
    assert "pw123" not in repr(study)  # secrets are masked in reprs


def test_study_off_allowlist_is_refused(tmp_path, settings):
    with pytest.raises(SafetyError):
        load_study(_write(tmp_path, "https://www.example.com"), settings)


def test_duplicate_task_ids_rejected():
    with pytest.raises(ValueError):
        Study.model_validate({"name": "x", "target": {"url": "http://a"},
                              "tasks": [{"id": "a", "goal": "g"}, {"id": "a", "goal": "h"}]})
