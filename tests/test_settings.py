"""Tests for settings load/save coercion and fallback behaviour.

The on-disk path is redirected into ``tmp_path`` so tests never touch the
developer's real ``~/.config/kubby/settings.json``.
"""
from __future__ import annotations

import json

import pytest

from kubby import settings as settings_mod


@pytest.fixture
def config(tmp_path, monkeypatch):
    """Point the module's config path at an isolated temp directory."""
    cfg_dir = tmp_path / "kubby"
    monkeypatch.setattr(settings_mod, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(settings_mod, "CONFIG_FILE", cfg_dir / "settings.json")
    return cfg_dir


def _write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")


class TestLoad:
    def test_missing_file_returns_defaults(self, config):
        assert settings_mod.load() == {"minikube": dict(settings_mod.DEFAULT_MINIKUBE)}

    def test_corrupt_json_returns_defaults(self, config):
        _write(settings_mod.CONFIG_FILE, "{not json")
        assert settings_mod.load() == {"minikube": dict(settings_mod.DEFAULT_MINIKUBE)}

    def test_non_object_json_returns_defaults(self, config):
        _write(settings_mod.CONFIG_FILE, '["minikube"]')
        assert settings_mod.load() == {"minikube": dict(settings_mod.DEFAULT_MINIKUBE)}

    def test_empty_file_returns_defaults(self, config):
        _write(settings_mod.CONFIG_FILE, "")
        assert settings_mod.load() == {"minikube": dict(settings_mod.DEFAULT_MINIKUBE)}

    def test_partial_file_is_merged_over_defaults(self, config):
        _write(settings_mod.CONFIG_FILE, json.dumps({"minikube": {"cpus": "8"}}))
        loaded = settings_mod.load()
        assert loaded["minikube"]["cpus"] == "8"
        assert loaded["minikube"]["memory"] == settings_mod.DEFAULT_MINIKUBE["memory"]

    def test_missing_keys_are_backfilled(self, config):
        _write(settings_mod.CONFIG_FILE, json.dumps({"minikube": {"driver": "podman"}}))
        assert set(settings_mod.load()["minikube"]) == set(
            settings_mod.DEFAULT_MINIKUBE
        )

    def test_unknown_top_level_keys_are_preserved(self, config):
        _write(settings_mod.CONFIG_FILE, json.dumps({"future_section": {"a": 1}}))
        assert settings_mod.load()["future_section"] == {"a": 1}

    def test_wrong_typed_section_is_replaced_with_defaults(self, config):
        _write(settings_mod.CONFIG_FILE, json.dumps({"minikube": "nope"}))
        assert settings_mod.load()["minikube"] == settings_mod.DEFAULT_MINIKUBE

    def test_values_are_coerced_on_read(self, config):
        _write(
            settings_mod.CONFIG_FILE,
            json.dumps({"minikube": {"cpus": 4, "rootless": 1}}),
        )
        mk = settings_mod.load()["minikube"]
        assert mk["cpus"] == "4"
        # bools are never auto-coerced from int → falls back to the default
        assert mk["rootless"] is False

    def test_string_addons_are_split_on_load(self, config):
        _write(
            settings_mod.CONFIG_FILE,
            json.dumps({"minikube": {"addons": "default, ingress"}}),
        )
        assert settings_mod.load()["minikube"]["addons"] == ["default", "ingress"]

    def test_int_value_is_coerced_to_string(self, config):
        _write(settings_mod.CONFIG_FILE, json.dumps({"minikube": {"driver": 42}}))
        assert settings_mod.load()["minikube"]["driver"] == "42"

    def test_uncoercible_value_falls_back_to_default(self, config):
        _write(
            settings_mod.CONFIG_FILE,
            json.dumps({"minikube": {"driver": 4.5, "rootless": "yes"}}),
        )
        mk = settings_mod.load()["minikube"]
        assert mk["driver"] == ""
        assert mk["rootless"] is False

    def test_returned_settings_are_independent_of_defaults(self, config):
        loaded = settings_mod.load()
        loaded["minikube"]["addons"].append("ingress")
        loaded["minikube"]["cpus"] = "99"
        fresh = settings_mod.load()
        assert fresh["minikube"]["addons"] == ["default"]
        assert fresh["minikube"]["cpus"] == settings_mod.DEFAULT_MINIKUBE["cpus"]


class TestSave:
    def test_save_creates_file_and_round_trips(self, config):
        payload = {"minikube": {"cpus": "4", "driver": "podman"}}
        settings_mod.save(payload)
        assert settings_mod.CONFIG_FILE.exists()
        assert settings_mod.load()["minikube"]["driver"] == "podman"
        assert settings_mod.load()["minikube"]["cpus"] == "4"

    def test_save_backfills_missing_keys_with_defaults(self, config):
        settings_mod.save({"minikube": {"driver": "docker"}})
        stored = json.loads(settings_mod.CONFIG_FILE.read_text(encoding="utf-8"))
        assert set(stored["minikube"]) == set(settings_mod.DEFAULT_MINIKUBE)
        assert stored["minikube"]["driver"] == "docker"
        assert stored["minikube"]["memory"] == settings_mod.DEFAULT_MINIKUBE["memory"]

    def test_non_dict_settings_raise(self, config):
        with pytest.raises(ValueError, match="must be a JSON object"):
            settings_mod.save(["nope"])  # type: ignore[arg-type]

    def test_corrupt_payload_is_coerced_before_write(self, config):
        settings_mod.save({"minikube": {"cpus": 8, "addons": "ingress"}})
        stored = json.loads(settings_mod.CONFIG_FILE.read_text(encoding="utf-8"))
        assert stored["minikube"]["cpus"] == "8"
        assert stored["minikube"]["addons"] == ["ingress"]

    def test_caller_payload_section_is_replaced_not_mutated(self, config):
        original = {"driver": "podman", "cpus": 8}
        payload = {"minikube": original}
        settings_mod.save(payload)
        # save() swaps in its own coerced copy, so the caller's dict object
        # keeps its original (uncoerced) contents verbatim.
        assert payload["minikube"] is not original
        assert original == {"driver": "podman", "cpus": 8}

    def test_non_dict_section_is_replaced(self, config):
        settings_mod.save({"minikube": 42})
        stored = json.loads(settings_mod.CONFIG_FILE.read_text(encoding="utf-8"))
        assert stored["minikube"] == settings_mod.DEFAULT_MINIKUBE

    def test_no_temp_files_are_left_behind(self, config):
        settings_mod.save({"minikube": {"cpus": "2"}})
        assert [p for p in config.iterdir() if p.suffix == ".tmp"] == []
