import pytest

from quad_controller.config import ConfigError, load_config


def test_defaults_load_sitl():
    cfg = load_config(env={})
    assert cfg.target.name == "sitl"
    assert cfg.target.connection.startswith("udpin:")
    assert cfg.safety.allow_real_aircraft_arming is False


def test_hardware_target_uses_uart():
    cfg = load_config(target="hardware", env={})
    assert cfg.target.connection == "/dev/ttyAMA0"
    assert cfg.target.baud == 921600


def test_override_file_and_env(tmp_path):
    f = tmp_path / "local.yaml"
    f.write_text("target: hardware\nmavlink:\n  heartbeat_timeout_s: 9\n")
    cfg = load_config(f, env={"QC_CONNECTION": "tcp:10.0.0.5:5760"})
    assert cfg.target.name == "hardware"
    assert cfg.target.connection == "tcp:10.0.0.5:5760"
    assert cfg.target.baud == 921600
    assert cfg.mavlink.heartbeat_timeout_s == 9


def test_cli_flags_win(tmp_path):
    cfg = load_config(target="sitl", connection="udpin:0.0.0.0:1", env={"QC_TARGET": "hardware"})
    assert cfg.target.name == "sitl"
    assert cfg.target.connection == "udpin:0.0.0.0:1"


def test_bad_target_rejected():
    with pytest.raises(ConfigError):
        load_config(target="moon", env={})


def test_unknown_key_rejected(tmp_path):
    f = tmp_path / "bad.yaml"
    f.write_text("mavlink:\n  heartbeat_timout_s: 1\n")
    with pytest.raises(ConfigError, match="heartbeat_timout_s"):
        load_config(f, env={})
