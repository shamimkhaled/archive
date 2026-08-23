import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def test_env_file_is_loaded(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("TEST_ENV_FLAG=from-env-file\n", encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TEST_ENV_FLAG", raising=False)

    import bcp_project.config as config_module

    config_module.ENV_FILE = env_file
    config_module.load_environment()

    assert os.getenv("TEST_ENV_FLAG") == "from-env-file"
