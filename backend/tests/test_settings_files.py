from pathlib import Path

import pytest

from app.settings import Settings


def config(tmp_path, text):
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_toml_groups_and_paths(tmp_path):
    path = config(tmp_path, '[worker]\nenabled=true\n[runtime]\nmax_loops=3\n'
                  '[storage]\ndatabase_path="data/example.db"\n'
                  '[knowledge.query_planning]\nmax_queries=2\n')
    settings = Settings(_env_file=None, config_file=path)
    assert settings.effective_task_worker_enabled is True
    assert settings.runtime_max_loops == 3
    assert settings.knowledge_query_planning_max_queries == 2
    assert settings.database_path == Path(__file__).resolve().parents[1] / "data/example.db"


def test_source_priority(tmp_path, monkeypatch):
    path = config(tmp_path, '[runtime]\nmax_loops=3\n')
    dotenv = tmp_path / ".env"
    dotenv.write_text("RUNTIME_MAX_LOOPS=4\n", encoding="utf-8")
    assert Settings(_env_file=dotenv, config_file=path).runtime_max_loops == 4
    monkeypatch.setenv("RUNTIME_MAX_LOOPS", "5")
    assert Settings(_env_file=dotenv, config_file=path).runtime_max_loops == 5
    assert Settings(_env_file=dotenv, config_file=path, runtime_max_loops=6).runtime_max_loops == 6


@pytest.mark.parametrize("value", ["true", "false"])
def test_legacy_worker_env_overrides_toml(tmp_path, monkeypatch, value):
    path = config(tmp_path, '[worker]\nenabled=true\n')
    monkeypatch.setenv("CODE_TASK_WORKER_ENABLED", value)
    assert Settings(_env_file=None, config_file=path).effective_task_worker_enabled == (value == "true")
    monkeypatch.setenv("TASK_WORKER_ENABLED", "false")
    assert Settings(_env_file=None, config_file=path).effective_task_worker_enabled is False


def test_legacy_worker_dotenv_overrides_toml(tmp_path):
    path = config(tmp_path, '[worker]\nenabled=true\n')
    dotenv = tmp_path / ".env"
    dotenv.write_text("CODE_TASK_WORKER_ENABLED=false\n", encoding="utf-8")
    assert Settings(_env_file=dotenv, config_file=path).effective_task_worker_enabled is False


@pytest.mark.parametrize("text", [
    '[runtime]\nmax_loop=3\n', '[llm]\napi_key="private-marker"\n',
    '[code]\nallow_network=true\n', '[worker]\nmax_concurrency=2\n',
    '[knowledge.rerank]\napi_key="private-marker"\n', '[runtime]\nmax_loops=[\n',
])
def test_invalid_toml_rejected_without_values(tmp_path, text):
    with pytest.raises(ValueError) as exc:
        Settings(_env_file=None, config_file=config(tmp_path, text))
    assert "private-marker" not in str(exc.value)


def test_explicit_missing_file_rejected(tmp_path):
    with pytest.raises(ValueError, match="configuration file"):
        Settings(_env_file=None, config_file=tmp_path / "missing.toml")


def test_disabled_file_sources_use_defaults(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.local.toml").write_text('[worker]\nenabled=true\n')
    settings = Settings(_env_file=None, config_file=None)
    assert settings.effective_task_worker_enabled is False
    assert settings.runtime_max_loops == 12


def test_doc_limit_is_wired(tmp_path):
    from app.dependencies import build_app_dependencies

    deps = build_app_dependencies(Settings(_env_file=None, config_file=None,
                                         docs_root=tmp_path, max_doc_bytes=7))
    tool = deps.tool_registry.get("read_docs")
    assert tool.max_bytes == 7


def test_config_file_environment_selection(tmp_path, monkeypatch):
    path = config(tmp_path, '[runtime]\nmax_loops=2\n')
    monkeypatch.setenv("CONFIG_FILE", str(path))
    assert Settings(_env_file=None).runtime_max_loops == 2
    monkeypatch.setenv("CONFIG_FILE", "")
    assert Settings(_env_file=None).runtime_max_loops == 12


def test_invalid_field_type_hides_input(tmp_path):
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as exc:
        Settings(_env_file=None, config_file=config(tmp_path, '[runtime]\nmax_loops="private-marker"\n'))
    assert "private-marker" not in str(exc.value)


def test_default_files_are_independent_of_cwd(tmp_path, monkeypatch):
    import app.settings as module

    monkeypatch.delenv("CONFIG_FILE")
    monkeypatch.setattr(module, "BACKEND_ROOT", tmp_path)
    (tmp_path / "config.local.toml").write_text('[runtime]\nmax_loops=2\n')
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert Settings(_env_file=tmp_path / ".env").runtime_max_loops == 2
    assert Settings(_env_file=None).runtime_max_loops == 12
    (tmp_path / "config.local.toml").unlink()
    assert Settings(_env_file=tmp_path / ".env").runtime_max_loops == 12


def test_example_covers_all_supported_options():
    import re
    from app.config_file import BACKEND_ROOT, GROUPS, read_config

    text = (BACKEND_ROOT / "config.example.toml").read_text()
    section = ""
    seen = set()
    for line in text.splitlines():
        if line.startswith("["):
            section = line.strip("[]")
        match = re.match(r"(?:# )?([a-z_]+)\s*=", line)
        if match:
            seen.add((section, match.group(1)))
    assert seen == {(group, key) for group, fields in GROUPS.items() for key in fields}
    data = read_config(BACKEND_ROOT / "config.example.toml", required=True)
    assert data["task_worker_enabled"] is True
    assert Settings(_env_file=None, config_file=BACKEND_ROOT / "config.example.toml").effective_task_worker_enabled