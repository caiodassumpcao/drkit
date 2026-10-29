import copy
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from drkit import config
from drkit.config import LoadedConfig, Manifest
from drkit.errors import ConfigError, InsecurePermissionsError

MINIMAL: dict[str, Any] = {
    "version": 1,
    "host": "prod-01",
    "repository": {
        "url": "s3:https://s3.example.com/bucket/prod-01",
        "password_file": "/etc/drkit/restic.pass",
    },
    "retention": {"daily": 7},
    "projects": [
        {
            "name": "shop",
            "path": "/srv/shop",
            "git": {"remote": "git@github.com:acme/shop.git"},
            "compose": {"files": ["docker-compose.yml"]},
            "secrets": [".env"],
            "databases": [{"engine": "postgres", "service": "db", "database": "shop"}],
            "volumes": [{"name": "uploads"}],
            "healthchecks": [{"url": "http://127.0.0.1:8080/health"}],
        }
    ],
}


def manifest_with(mutate: Callable[[dict[str, Any]], object]) -> dict[str, Any]:
    data = copy.deepcopy(MINIMAL)
    mutate(data)
    return data


def validation_message(data: dict[str, Any]) -> str:
    with pytest.raises(ValidationError) as excinfo:
        Manifest.model_validate(data)
    return config.format_validation_error(excinfo.value)


def project(data: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = data["projects"][0]
    return result


# --------------------------------------------------------------------------- models


class TestModels:
    def test_minimal_manifest_and_defaults(self) -> None:
        manifest = Manifest.model_validate(MINIMAL)
        shop = manifest.projects[0]
        assert shop.compose_project == "shop"
        assert shop.compose_files() == [shop.path / file for file in shop.compose.files]
        assert shop.compose_files(Path("/restore")) == [
            Path("/restore") / file for file in shop.compose.files
        ]
        assert shop.databases[0].user_env_candidates == ("POSTGRES_USER",)
        assert shop.volumes[0].consistency == "live"
        assert shop.healthchecks[0].expect_status == 200
        assert shop.healthchecks[0].timeout == 60
        assert shop.hooks.post_restore == []
        assert manifest.system.paths == []
        assert manifest.notifications is None
        assert manifest.project("shop") is shop

    def test_mysql_default_user_env_and_override(self) -> None:
        manifest = Manifest.model_validate(
            manifest_with(
                lambda d: project(d)["databases"].extend(
                    [
                        {"engine": "mysql", "service": "mysql", "database": "a"},
                        {"engine": "mysql", "service": "m", "database": "b", "user_env": "U"},
                    ]
                )
            )
        )
        dbs = manifest.projects[0].databases
        assert dbs[1].user_env_candidates == ("MYSQL_USER", "MARIADB_USER")
        assert dbs[2].user_env_candidates == ("U",)

    def test_password_env_defaults_and_override(self) -> None:
        manifest = Manifest.model_validate(
            manifest_with(
                lambda d: project(d)["databases"].extend(
                    [
                        {"engine": "mysql", "service": "m", "database": "a"},
                        {"engine": "mysql", "service": "m", "database": "b", "password_env": "P"},
                    ]
                )
            )
        )
        postgres, mysql_default, mysql_custom = manifest.projects[0].databases
        assert postgres.password_env_candidates == ()
        assert mysql_default.password_env_candidates == ("MYSQL_PASSWORD", "MARIADB_PASSWORD")
        assert mysql_custom.password_env_candidates == ("P",)

    def test_unknown_project_lookup(self) -> None:
        manifest = Manifest.model_validate(MINIMAL)
        with pytest.raises(ConfigError, match="unknown project 'nope'") as excinfo:
            manifest.project("nope")
        assert excinfo.value.hint == "projects in the manifest: shop"

    def test_models_are_immutable(self) -> None:
        manifest = Manifest.model_validate(MINIMAL)
        with pytest.raises(ValidationError, match="frozen"):
            manifest.host = "other"  # type: ignore[misc]

    @pytest.mark.parametrize(
        ("mutate", "expected"),
        [
            (lambda d: d.update(extra=1), "extra: Extra inputs are not permitted"),
            (
                lambda d: project(d)["git"].update(branch="main"),
                "projects[0].git.branch: Extra inputs are not permitted",
            ),
            (lambda d: d.update(version=2), "version: Input should be 1"),
            (lambda d: d.update(host="Prod_01"), "host: must be 2-41"),
            (lambda d: d.update(host="a"), "host: must be 2-41"),
            (lambda d: project(d).update(name="-shop"), "projects[0].name:"),
            (lambda d: project(d).update(name="x" * 42), "projects[0].name:"),
            (lambda d: d.pop("retention"), "retention: Field required"),
            (
                lambda d: d.update(retention={}),
                "retention: set at least one of daily, weekly or monthly",
            ),
            (
                lambda d: d.update(retention={"daily": 0}),
                "retention.daily: Input should be greater than 0",
            ),
            (
                lambda d: d["repository"].update(password_file="restic.pass"),
                "repository.password_file: must be an absolute path",
            ),
            (
                lambda d: project(d).update(path="srv/shop"),
                "projects[0].path: must be an absolute path",
            ),
            (
                lambda d: project(d).update(secrets=["/etc/shadow"]),
                "projects[0].secrets[0]: must be a path relative",
            ),
            (
                lambda d: project(d).update(secrets=["config/../../.env"]),
                "projects[0].secrets[0]: must not escape the project directory",
            ),
            (
                lambda d: project(d).update(secrets=[""]),
                "projects[0].secrets[0]: must not be empty",
            ),
            (
                lambda d: project(d)["compose"].update(files=["../compose.yml"]),
                "projects[0].compose.files[0]: must not escape",
            ),
            (
                lambda d: project(d)["compose"].update(files=[]),
                "projects[0].compose.files: List should have at least 1 item",
            ),
            (
                lambda d: project(d)["compose"].update(project_name="Shop"),
                "projects[0].compose.project_name: must be lowercase",
            ),
            (
                lambda d: project(d)["databases"][0].update(engine="mongo"),
                "projects[0].databases[0].engine: Input should be 'postgres' or 'mysql'",
            ),
            (
                lambda d: project(d)["databases"][0].update(database="a;drop"),
                "projects[0].databases[0].database: must be a database name",
            ),
            (
                lambda d: project(d)["databases"][0].update(user_env="1BAD"),
                "projects[0].databases[0].user_env:",
            ),
            (
                lambda d: project(d)["databases"][0].update(password_env="PGPASSWORD"),
                "projects[0].databases[0]: password_env is only used by mysql",
            ),
            (
                lambda d: project(d)["databases"][0].update(engine="mysql", password_env="1X"),
                "projects[0].databases[0].password_env: must be an environment variable name",
            ),
            (
                lambda d: project(d)["volumes"][0].update(consistency="pause"),
                "projects[0].volumes[0].consistency: Input should be 'live' or 'stop'",
            ),
            (
                lambda d: project(d)["healthchecks"][0].update(url="ftp://x"),
                "projects[0].healthchecks[0].url: URL scheme should be 'http' or 'https'",
            ),
            (
                lambda d: project(d)["healthchecks"][0].update(expect_status=99),
                "projects[0].healthchecks[0].expect_status: Input should be greater than",
            ),
            (
                lambda d: project(d).update(hooks={"pre_backup": [""]}),
                "projects[0].hooks.pre_backup[0]: String should have at least 1 character",
            ),
            (
                lambda d: d.update(system={"tls": {"strategy": "reissue"}}),
                "system.tls: email is required when strategy is 'reissue'",
            ),
            (
                lambda d: d.update(system={"tls": {"strategy": "restore", "email": "nope"}}),
                "system.tls.email: must be an email address",
            ),
            (
                lambda d: d.update(system={"ansible": {"source": "x", "playbook": "/abs.yml"}}),
                "system.ansible.playbook:",
            ),
            (
                lambda d: d.update(notifications={"on": ["failure"]}),
                "notifications: configure at least one channel",
            ),
            (
                lambda d: d.update(
                    notifications={"on": ["always"], "webhook": {"url": "https://x"}}
                ),
                "notifications.on[0]: Input should be 'failure' or 'success'",
            ),
            (
                lambda d: d.update(notifications={"webhook": {"url": "hooks.slack.com/x"}}),
                "notifications.webhook.url: must be an http(s) URL",
            ),
        ],
    )
    def test_invalid_manifests_produce_precise_errors(
        self, mutate: Callable[[dict[str, Any]], object], expected: str
    ) -> None:
        assert expected in validation_message(manifest_with(mutate))

    def test_project_names_must_be_unique(self) -> None:
        message = validation_message(
            manifest_with(lambda d: d["projects"].append(copy.deepcopy(project(d))))
        )
        assert "projects[].name must be unique, duplicated: shop" in message

    def test_database_and_volume_names_unique_per_project(self) -> None:
        def mutate(d: dict[str, Any]) -> None:
            project(d)["databases"].append({"engine": "mysql", "service": "m", "database": "shop"})
            project(d)["volumes"].append({"name": "uploads", "consistency": "stop"})

        message = validation_message(manifest_with(mutate))
        assert "databases[].database must be unique, duplicated: shop" in message

        message = validation_message(
            manifest_with(lambda d: project(d)["volumes"].append({"name": "uploads"}))
        )
        assert "volumes[].name must be unique, duplicated: uploads" in message

    def test_all_errors_are_reported_together(self) -> None:
        message = validation_message(
            manifest_with(lambda d: (d.update(host="X"), project(d).update(path="rel")))
        )
        assert "host:" in message
        assert "projects[0].path:" in message

    def test_errors_never_echo_input_values(self) -> None:
        message = validation_message(
            manifest_with(lambda d: d.update(notifications={"webhook": {"url": "sekrit-url"}}))
        )
        assert "sekrit-url" not in message

    def test_chat_id_accepts_numbers(self) -> None:
        manifest = Manifest.model_validate(
            manifest_with(
                lambda d: d.update(
                    notifications={"telegram": {"bot_token": "123:abc", "chat_id": 123456789}}
                )
            )
        )
        assert manifest.notifications is not None
        assert manifest.notifications.telegram is not None
        assert manifest.notifications.telegram.chat_id == "123456789"
        assert manifest.notifications.on == ["failure"]

    def test_secret_values_and_private_files(self) -> None:
        def mutate(d: dict[str, Any]) -> None:
            d["repository"]["env_file"] = "/etc/drkit/env"
            project(d)["git"]["deploy_key"] = "/etc/drkit/keys/shop"
            d["notifications"] = {
                "telegram": {"bot_token": "tg-token", "chat_id": "1"},
                "webhook": {"url": "https://hooks.example.com/abc"},
            }

        manifest = Manifest.model_validate(manifest_with(mutate))
        assert manifest.secret_values() == ["tg-token", "https://hooks.example.com/abc"]
        assert manifest.private_files() == {
            Path("/etc/drkit/restic.pass"): "password file",
            Path("/etc/drkit/env"): "env file",
            Path("/etc/drkit/keys/shop"): "deploy key (shop)",
        }
        assert "tg-token" not in repr(manifest)


# --------------------------------------------------------------------------- parsing


class TestYaml:
    def test_on_key_is_not_a_boolean(self) -> None:
        data = config.parse_yaml("on: [failure]\nenabled: yes\nflag: true\n", source=Path("x"))
        assert data == {"on": ["failure"], "enabled": "yes", "flag": True}

    def test_duplicate_keys_are_rejected(self) -> None:
        with pytest.raises(ConfigError, match=r"x:2:1: duplicate key 'host'"):
            config.parse_yaml("host: a\nhost: b\n", source=Path("x"))

    def test_syntax_error_reports_position_without_content(self) -> None:
        with pytest.raises(ConfigError, match=r"invalid YAML at x:2:") as excinfo:
            config.parse_yaml("a: 1\n  token: [unclosed-secret-value\n", source=Path("x"))
        assert "unclosed-secret-value" not in str(excinfo.value)

    @pytest.mark.parametrize("text", ["", "- a\n- b\n", "just a string"])
    def test_top_level_must_be_a_mapping(self, text: str) -> None:
        with pytest.raises(ConfigError, match="top level must be a mapping"):
            config.parse_yaml(text, source=Path("x"))

    def test_non_marked_yaml_errors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(*_: object, **__: object) -> None:
            raise yaml.YAMLError("raw")

        monkeypatch.setattr(yaml, "load", boom)
        with pytest.raises(ConfigError, match="invalid YAML in x: YAMLError"):
            config.parse_yaml("a: 1", source=Path("x"))


class TestEnvFile:
    def test_parses_comments_export_and_quotes(self) -> None:
        text = (
            "# credentials\n"
            "\n"
            "AWS_ACCESS_KEY_ID=AKIA123\n"
            "export AWS_SECRET_ACCESS_KEY = 'abc def'\n"
            'DRKIT_TG_TOKEN="1:x#y"\n'
            "EMPTY=\n"
            "WITH_EQUALS=a=b\n"
        )
        assert config.parse_env_file(text, source=Path("env")) == {
            "AWS_ACCESS_KEY_ID": "AKIA123",
            "AWS_SECRET_ACCESS_KEY": "abc def",
            "DRKIT_TG_TOKEN": "1:x#y",
            "EMPTY": "",
            "WITH_EQUALS": "a=b",
        }

    @pytest.mark.parametrize("line", ["just-a-secret-value", "1BAD=x", "=x"])
    def test_invalid_line_reports_line_number_only(self, line: str) -> None:
        with pytest.raises(ConfigError, match=r"invalid env file env:2: expected KEY=VALUE") as exc:
            config.parse_env_file(f"OK=1\n{line}\n", source=Path("env"))
        assert line not in str(exc.value)


class TestInterpolation:
    def test_replaces_nested_values_only(self) -> None:
        data = {"a": "${X}", "b": ["x-${X}-${Y}", 3, None], "c": {"${X}": "${Y}"}}
        assert config.interpolate(data, {"X": "1", "Y": "2"}) == {
            "a": "1",
            "b": ["x-1-2", 3, None],
            "c": {"${X}": "2"},
        }

    def test_dollar_escape_and_unbraced_variables(self) -> None:
        assert config.interpolate("$${X} $$ $HOME", {"X": "1"}) == "${X} $ $HOME"

    def test_missing_variables_are_all_reported_with_locations(self) -> None:
        data = {"n": {"t": "${TOKEN}", "u": ["${URL}", "${TOKEN}"]}}
        with pytest.raises(ConfigError) as excinfo:
            config.interpolate(data, {"OTHER": "do-not-print-me"})
        message = str(excinfo.value)
        assert "TOKEN (used at n.t, n.u[1])" in message
        assert "URL (used at n.u[0])" in message
        assert "do-not-print-me" not in message

    @pytest.mark.parametrize("ref", ["${}", "${1X}", "${A-B}"])
    def test_invalid_references(self, ref: str) -> None:
        with pytest.raises(ConfigError, match="invalid variable reference"):
            config.interpolate({"k": ref}, {})


def test_format_loc() -> None:
    assert config.format_loc(()) == "<root>"
    assert config.format_loc(("projects", 0, "databases", 1, "engine")) == (
        "projects[0].databases[1].engine"
    )


# --------------------------------------------------------------------------- loading


def write_private(path: Path, content: str, mode: int = 0o600) -> Path:
    path.write_text(content)
    path.chmod(mode)
    return path


@pytest.fixture
def files(tmp_path: Path) -> dict[str, Path]:
    password = write_private(tmp_path / "restic.pass", "pw\n")
    env = write_private(
        tmp_path / "env", "AWS_SECRET_ACCESS_KEY=aws-secret\nDRKIT_TG_TOKEN=from-file\n"
    )
    data = manifest_with(
        lambda d: (
            d["repository"].update(password_file=str(password), env_file=str(env)),
            d.update(
                notifications={
                    "on": ["failure", "success"],
                    "telegram": {"bot_token": "${DRKIT_TG_TOKEN}", "chat_id": "1"},
                }
            ),
        )
    )
    manifest = write_private(tmp_path / "dr.yaml", yaml.safe_dump(data, sort_keys=False))
    return {"manifest": manifest, "password": password, "env": env}


def load(files: dict[str, Path], **environ: str) -> LoadedConfig:
    return config.load_config(files["manifest"], environ=environ)


class TestLoadConfig:
    def test_loads_and_interpolates_from_env_file(self, files: dict[str, Path]) -> None:
        loaded = load(files)
        assert loaded.path == files["manifest"]
        assert loaded.env["AWS_SECRET_ACCESS_KEY"] == "aws-secret"
        notifications = loaded.manifest.notifications
        assert notifications is not None
        assert notifications.on == ["failure", "success"]
        assert notifications.telegram is not None
        assert notifications.telegram.bot_token.get_secret_value() == "from-file"
        assert set(loaded.secret_values()) == {"aws-secret", "from-file"}

    def test_process_environment_wins_over_env_file(self, files: dict[str, Path]) -> None:
        loaded = load(files, DRKIT_TG_TOKEN="from-process")
        assert loaded.manifest.notifications is not None
        assert loaded.manifest.notifications.telegram is not None
        assert loaded.manifest.notifications.telegram.bot_token.get_secret_value() == (
            "from-process"
        )

    def test_env_file_path_can_use_process_variables(
        self, files: dict[str, Path], tmp_path: Path
    ) -> None:
        text = files["manifest"].read_text().replace(str(files["env"]), "${DRKIT_DIR}/env")
        files["manifest"].write_text(text)
        loaded = load(files, DRKIT_DIR=str(tmp_path))
        assert loaded.env["DRKIT_TG_TOKEN"] == "from-file"

    def test_without_env_file(self, files: dict[str, Path]) -> None:
        data = yaml.safe_load(files["manifest"].read_text())
        del data["repository"]["env_file"]
        del data["notifications"]
        files["manifest"].write_text(yaml.safe_dump(data))
        assert load(files).env == {}

    def test_missing_variable(self, files: dict[str, Path]) -> None:
        files["env"].write_text("OTHER=1\n")
        with pytest.raises(ConfigError, match=r"DRKIT_TG_TOKEN \(used at notifications"):
            load(files)

    def test_validation_errors_name_the_file(self, files: dict[str, Path]) -> None:
        files["manifest"].write_text(files["manifest"].read_text().replace("prod-01", "PROD"))
        with pytest.raises(ConfigError) as excinfo:
            load(files)
        assert f"invalid manifest {files['manifest']}:\n  host: must be 2-41" in str(excinfo.value)
        assert excinfo.value.exit_code == 2

    @pytest.mark.parametrize("which", ["manifest", "env", "password"])
    def test_group_or_world_accessible_files_are_refused(
        self, files: dict[str, Path], which: str
    ) -> None:
        files[which].chmod(0o640)
        with pytest.raises(InsecurePermissionsError, match=r"mode 0640") as excinfo:
            load(files)
        assert excinfo.value.hint == f"chmod 600 {files[which]}"

    def test_permission_checks_can_be_skipped(self, files: dict[str, Path]) -> None:
        for path in files.values():
            path.chmod(0o644)
        loaded = config.load_config(files["manifest"], environ={}, check_permissions=False)
        assert loaded.manifest.host == "prod-01"

    def test_missing_files(self, files: dict[str, Path], tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="manifest not found"):
            config.load_config(tmp_path / "nope.yaml", check_permissions=False)
        with pytest.raises(ConfigError, match="manifest not found"):
            config.load_config(tmp_path / "nope.yaml")
        files["password"].unlink()
        with pytest.raises(ConfigError, match="password file not found"):
            load(files)
        files["env"].unlink()
        with pytest.raises(ConfigError, match="env file not found"):
            config.load_config(files["manifest"], environ={}, check_permissions=False)

    def test_unreadable_file(self, files: dict[str, Path]) -> None:
        if os.geteuid() == 0:
            pytest.skip("root can read any file")
        files["env"].chmod(0o000)
        with pytest.raises(ConfigError, match=r"cannot read env file .*permission denied"):
            config.load_config(files["manifest"], environ={}, check_permissions=False)

    def test_non_utf8_file(self, files: dict[str, Path]) -> None:
        files["manifest"].write_bytes(b"\xff\xfe")
        with pytest.raises(ConfigError, match="cannot read manifest"):
            load(files)


class TestCheckPrivateFile:
    def test_ok(self, tmp_path: Path) -> None:
        config.check_private_file(write_private(tmp_path / "f", "x"))

    def test_owner_must_be_current_user(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = write_private(tmp_path / "f", "x")
        monkeypatch.setattr(os, "geteuid", lambda: path.stat().st_uid + 1)
        with pytest.raises(InsecurePermissionsError, match="is owned by uid"):
            config.check_private_file(path, label="password file")

    def test_directory_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="is not a regular file"):
            config.check_private_file(tmp_path)

    def test_inaccessible_parent(self, tmp_path: Path) -> None:
        if os.geteuid() == 0:
            pytest.skip("root can traverse any directory")
        locked = tmp_path / "locked"
        locked.mkdir()
        write_private(locked / "f", "x")
        locked.chmod(0o000)
        try:
            with pytest.raises(ConfigError, match="permission denied"):
                config.check_private_file(locked / "f")
        finally:
            locked.chmod(0o700)


class TestResolveConfigPath:
    def test_precedence(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(config.CONFIG_ENV_VAR, raising=False)
        assert config.resolve_config_path() == config.DEFAULT_CONFIG_PATH
        monkeypatch.setenv(config.CONFIG_ENV_VAR, "/tmp/from-env.yaml")
        assert config.resolve_config_path() == Path("/tmp/from-env.yaml")
        assert config.resolve_config_path(Path("/x.yaml")) == Path("/x.yaml")
