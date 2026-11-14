import shutil

import pytest
import yaml
from pydantic import ValidationError

from src.common.config import CONFIG_DIR, load_settings


def test_real_config_loads():
    s = load_settings()
    assert s.rubric.rubric_version == "v1"
    assert s.pipeline.hunter.mode == "mock"
    assert s.pipeline.email.mode == "dry_run"
    assert s.pipeline.qualification.already_certified_policy == "research"
    assert s.pipeline.qualification.max_extra_rounds == 2
    assert {r.id for r in s.rubric.evidence_map} == {"already_certified", "b2b_saas", "stage", "trigger"}


def test_rubric_buckets_match_spec():
    r = load_settings().rubric
    assert r.bucket_points("compliance_trigger", "explicit_audit_target") == 40
    assert r.bucket_points("compliance_trigger", "grc_leader_hire") == 25
    assert r.bucket_points("saas_fit", "it_services") == 0
    assert r.bucket_points("stage", "series_d_plus") == 8
    assert r.decision["qualified"]["min_score"] == 75


def test_secrets_never_in_repr(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "super-secret-value-123")
    s = load_settings()
    assert "super-secret-value-123" not in repr(s.secrets)
    assert s.secrets.present()["GEMINI_API_KEY"] is True


def _copy_cfg(tmp_path):
    d = tmp_path / "config"
    shutil.copytree(CONFIG_DIR, d)
    return d


def _edit(path, fn):
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    fn(data)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


def test_rubric_must_sum_to_100(tmp_path):
    d = _copy_cfg(tmp_path)
    _edit(d / "icp_rubric.yaml", lambda r: r["components"]["stage"].__setitem__("max", 20))
    with pytest.raises(ValidationError, match="sum to 100"):
        load_settings(config_dir=d)


def test_robots_cannot_be_disabled(tmp_path):
    d = _copy_cfg(tmp_path)
    _edit(d / "pipeline.yaml", lambda p: p["scrape"].__setitem__("respect_robots", False))
    with pytest.raises(ValidationError, match="respect_robots"):
        load_settings(config_dir=d)


def test_hunter_hard_stop_not_above_budget(tmp_path):
    d = _copy_cfg(tmp_path)
    _edit(d / "pipeline.yaml", lambda p: p["hunter"].__setitem__("hard_stop", 30))
    with pytest.raises(ValidationError, match="hard_stop"):
        load_settings(config_dir=d)


def test_loop_rounds_capped_at_two(tmp_path):
    d = _copy_cfg(tmp_path)
    _edit(d / "pipeline.yaml", lambda p: p["qualification"].__setitem__("max_extra_rounds", 5))
    with pytest.raises(ValidationError):
        load_settings(config_dir=d)
