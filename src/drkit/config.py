"""Manifest (``dr.yaml``) models, loading, ``${VAR}`` interpolation and file permission checks.

Loading order: permission checks → YAML → env file → interpolation → validation. Error messages
name files, keys and variables, but never echo values: the manifest and env file hold secrets.
"""

from __future__ import annotations

import os
import re
import stat
from collections import Counter
from collections.abc import Hashable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal, Self

import yaml
from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    HttpUrl,
    PositiveInt,
    SecretStr,
    ValidationError,
    model_validator,
)

from drkit.errors import ConfigError, InsecurePermissionsError

DEFAULT_CONFIG_PATH = Path("/etc/drkit/dr.yaml")
CONFIG_ENV_VAR = "DRKIT_CONFIG"

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")
_COMPOSE_PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_DB_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]{0,62}$")
_VOLUME_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# `$$` escapes a literal `$` (as in Docker Compose); only the braced `${VAR}` form is
# interpolated, so shell variables like `$HOME` in hooks pass through untouched.
_INTERPOLATION_RE = re.compile(r"\$\$|\$\{([^}]*)\}")


# --------------------------------------------------------------------------- field types


def _name(value: str) -> str:
    if not _NAME_RE.match(value):
        raise ValueError(
            "must be 2-41 characters of lowercase letters, digits or '-', "
            "starting with a letter or digit"
        )
    return value


def _matches(pattern: re.Pattern[str], description: str) -> AfterValidator:
    def check(value: str) -> str:
        if not pattern.match(value):
            raise ValueError(f"must be {description}")
        return value

    return AfterValidator(check)


def _absolute(value: Path) -> Path:
    if not value.is_absolute():
        raise ValueError("must be an absolute path")
    return value


def _contained_relative(value: Path) -> Path:
    posix = PurePosixPath(value.as_posix())
    if posix.is_absolute():
        raise ValueError("must be a path relative to the project directory")
    if ".." in posix.parts:
        raise ValueError("must not escape the project directory ('..')")
    if not posix.parts:
        raise ValueError("must not be empty")
    return value


def _to_str(value: Any) -> Any:
    # YAML reads `chat_id: 123` as an int; it is an identifier, not a number.
    return str(value) if isinstance(value, int) and not isinstance(value, bool) else value


def _http_url(value: SecretStr) -> SecretStr:
    if not value.get_secret_value().startswith(("https://", "http://")):
        raise ValueError("must be an http(s) URL")
    return value


Name = Annotated[str, AfterValidator(_name)]
NonEmptyStr = Annotated[str, Field(min_length=1)]
AbsolutePath = Annotated[Path, AfterValidator(_absolute)]
RelativePath = Annotated[Path, AfterValidator(_contained_relative)]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --------------------------------------------------------------------------- models


class Repository(_Model):
    url: NonEmptyStr
    password_file: AbsolutePath
    env_file: AbsolutePath | None = None


class Retention(_Model):
    daily: PositiveInt | None = None
    weekly: PositiveInt | None = None
    monthly: PositiveInt | None = None

    @model_validator(mode="after")
    def _at_least_one(self) -> Self:
        if self.daily is None and self.weekly is None and self.monthly is None:
            raise ValueError("set at least one of daily, weekly or monthly")
        return self


class Ansible(_Model):
    source: NonEmptyStr
    ref: NonEmptyStr | None = None
    playbook: RelativePath = Path("site.yml")
    extra_vars: dict[str, Any] = Field(default_factory=dict)
    skip_tags: list[NonEmptyStr] = Field(default_factory=list)


class SystemPath(_Model):
    path: AbsolutePath
    include: list[NonEmptyStr] = Field(default_factory=list)
    restore: bool = False


class Tls(_Model):
    strategy: Literal["reissue", "restore"]
    email: Annotated[str, _matches(re.compile(r"^[^@\s]+@[^@\s]+$"), "an email address")] | None = (
        None
    )

    @model_validator(mode="after")
    def _email_for_reissue(self) -> Self:
        if self.strategy == "reissue" and self.email is None:
            raise ValueError("email is required when strategy is 'reissue' (used by certbot)")
        return self


class System(_Model):
    ansible: Ansible | None = None
    paths: list[SystemPath] = Field(default_factory=list)
    tls: Tls | None = None


class Git(_Model):
    remote: NonEmptyStr
    deploy_key: AbsolutePath | None = None


class Compose(_Model):
    project_name: (
        Annotated[
            str,
            _matches(_COMPOSE_PROJECT_RE, "lowercase letters, digits, '-' or '_'"),
        ]
        | None
    ) = None
    files: list[RelativePath] = Field(min_length=1)


DEFAULT_USER_ENV: Mapping[str, str] = {"postgres": "POSTGRES_USER", "mysql": "MYSQL_USER"}
DEFAULT_PASSWORD_ENV = "MYSQL_PASSWORD"  # noqa: S105 - a variable name, not a password

EnvVarName = Annotated[str, _matches(_ENV_KEY_RE, "an environment variable name")]


class Database(_Model):
    engine: Literal["postgres", "mysql"]
    service: NonEmptyStr
    database: Annotated[str, _matches(_DB_NAME_RE, "a database name ([A-Za-z0-9_-], max 63)")]
    user_env: EnvVarName | None = None
    password_env: EnvVarName | None = None
    verify_query: NonEmptyStr | None = None

    @model_validator(mode="after")
    def _password_env_for_mysql(self) -> Self:
        if self.password_env is not None and self.engine != "mysql":
            raise ValueError(
                "password_env is only used by mysql (postgres dumps authenticate over the "
                "container's local socket)"
            )
        return self

    @property
    def effective_user_env(self) -> str:
        """Container env var holding the database user (engine default when unset)."""
        return self.user_env or DEFAULT_USER_ENV[self.engine]

    @property
    def effective_password_env(self) -> str | None:
        """Container env var holding the password (mysql only; default MYSQL_PASSWORD)."""
        if self.engine != "mysql":
            return None
        return self.password_env or DEFAULT_PASSWORD_ENV


class Volume(_Model):
    name: Annotated[str, _matches(_VOLUME_NAME_RE, "a Docker volume name")]
    consistency: Literal["live", "stop"] = "live"


class Hooks(_Model):
    pre_backup: list[NonEmptyStr] = Field(default_factory=list)
    post_backup: list[NonEmptyStr] = Field(default_factory=list)
    post_restore: list[NonEmptyStr] = Field(default_factory=list)


class Healthcheck(_Model):
    url: HttpUrl
    expect_status: int = Field(default=200, ge=100, le=599)
    timeout: PositiveInt = 60


class Project(_Model):
    name: Name
    path: AbsolutePath
    git: Git
    compose: Compose
    secrets: list[RelativePath] = Field(default_factory=list)
    databases: list[Database] = Field(default_factory=list)
    volumes: list[Volume] = Field(default_factory=list)
    hooks: Hooks = Field(default_factory=Hooks)
    healthchecks: list[Healthcheck] = Field(default_factory=list)

    @property
    def compose_project(self) -> str:
        return self.compose.project_name or self.name

    @model_validator(mode="after")
    def _unique_items(self) -> Self:
        _require_unique((d.database for d in self.databases), "databases[].database")
        _require_unique((v.name for v in self.volumes), "volumes[].name")
        return self


class Telegram(_Model):
    bot_token: SecretStr
    chat_id: Annotated[NonEmptyStr, BeforeValidator(_to_str)]


class Webhook(_Model):
    url: Annotated[SecretStr, AfterValidator(_http_url)]


NotifyEvent = Literal["failure", "success"]


class Notifications(_Model):
    on: list[NotifyEvent] = Field(default_factory=lambda: list[NotifyEvent](["failure"]))
    telegram: Telegram | None = None
    webhook: Webhook | None = None

    @model_validator(mode="after")
    def _at_least_one_channel(self) -> Self:
        if self.telegram is None and self.webhook is None:
            raise ValueError("configure at least one channel: telegram or webhook")
        return self


class Manifest(_Model):
    version: Literal[1]
    host: Name
    repository: Repository
    retention: Retention
    system: System = Field(default_factory=System)
    projects: list[Project] = Field(default_factory=list)
    notifications: Notifications | None = None

    @model_validator(mode="after")
    def _unique_projects(self) -> Self:
        _require_unique((p.name for p in self.projects), "projects[].name")
        return self

    def project(self, name: str) -> Project:
        for project in self.projects:
            if project.name == name:
                return project
        known = ", ".join(p.name for p in self.projects) or "none"
        raise ConfigError(f"unknown project {name!r}", hint=f"projects in the manifest: {known}")

    def secret_values(self) -> list[str]:
        """Secret values held by the manifest itself (to register with the redactor)."""
        values: list[str] = []
        if self.notifications is not None:
            if self.notifications.telegram is not None:
                values.append(self.notifications.telegram.bot_token.get_secret_value())
            if self.notifications.webhook is not None:
                values.append(self.notifications.webhook.url.get_secret_value())
        return values

    def private_files(self) -> dict[Path, str]:
        """Files that must only be accessible by their owner, with a label for messages."""
        files = {self.repository.password_file: "password file"}
        if self.repository.env_file is not None:
            files[self.repository.env_file] = "env file"
        for project in self.projects:
            if project.git.deploy_key is not None:
                files[project.git.deploy_key] = f"deploy key ({project.name})"
        return files


def _require_unique(values: Iterable[Hashable], what: str) -> None:
    duplicates = sorted(str(value) for value, count in Counter(values).items() if count > 1)
    if duplicates:
        raise ValueError(f"{what} must be unique, duplicated: {', '.join(duplicates)}")


# --------------------------------------------------------------------------- loading


@dataclass(frozen=True, slots=True)
class LoadedConfig:
    path: Path
    manifest: Manifest
    env: Mapping[str, str] = field(default_factory=dict)
    """Variables read from ``repository.env_file`` (credentials passed to restic)."""

    def secret_values(self) -> list[str]:
        return [*self.env.values(), *self.manifest.secret_values()]


def resolve_config_path(explicit: Path | None = None) -> Path:
    """``--config`` wins, then ``$DRKIT_CONFIG``, then the default location."""
    if explicit is not None:
        return explicit
    from_env = os.environ.get(CONFIG_ENV_VAR)
    return Path(from_env) if from_env else DEFAULT_CONFIG_PATH


def load_config(
    path: Path,
    *,
    environ: Mapping[str, str] | None = None,
    check_permissions: bool = True,
) -> LoadedConfig:
    """Load, interpolate and validate a manifest.

    Variables come from ``repository.env_file`` and the process environment; the process
    environment wins on conflicts. With ``check_permissions`` (the default), the manifest,
    password file and env file must be regular files accessible only by their owner.
    """
    process_env = dict(os.environ if environ is None else environ)
    if check_permissions:
        check_private_file(path, label="manifest")
    raw = parse_yaml(_read_text(path, "manifest"), source=path)

    env: dict[str, str] = {}
    env_file = _raw_env_file(raw, process_env)
    if env_file is not None:
        if check_permissions:
            check_private_file(env_file, label="env file")
        env = parse_env_file(_read_text(env_file, "env file"), source=env_file)

    interpolated = interpolate(raw, {**env, **process_env})
    try:
        manifest = Manifest.model_validate(interpolated)
    except ValidationError as exc:
        raise ConfigError(
            f"invalid manifest {path}:\n{format_validation_error(exc)}",
            hint="see examples/dr.yaml for an annotated reference",
        ) from None

    if check_permissions:
        check_private_file(manifest.repository.password_file, label="password file")
    return LoadedConfig(path=path, manifest=manifest, env=env)


class _YamlLoader(yaml.SafeLoader):
    """SafeLoader with YAML 1.2 booleans and duplicate-key detection.

    YAML 1.1 turns `on`, `off`, `yes` and `no` into booleans, which would make the
    `notifications.on` key silently become `True`.
    """

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        seen: set[Any] = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in seen:
                raise yaml.constructor.ConstructorError(
                    None, None, f"duplicate key {key!r}", key_node.start_mark
                )
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


_YamlLoader.yaml_implicit_resolvers = {
    first: [r for r in resolvers if r[0] != "tag:yaml.org,2002:bool"]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_YamlLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool", re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"), list("tTfF")
)


def parse_yaml(text: str, *, source: Path) -> dict[str, Any]:
    try:
        data = yaml.load(text, Loader=_YamlLoader)  # noqa: S506 - _YamlLoader is a SafeLoader
    except yaml.MarkedYAMLError as exc:
        # Only the problem and position: the default message quotes the offending line,
        # which may contain a secret.
        mark = exc.problem_mark or exc.context_mark
        where = f"{source}:{mark.line + 1}:{mark.column + 1}" if mark else str(source)
        raise ConfigError(f"invalid YAML at {where}: {exc.problem or exc.context}") from None
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {source}: {type(exc).__name__}") from None
    if not isinstance(data, dict):
        raise ConfigError(f"invalid manifest {source}: top level must be a mapping")
    return data


def parse_env_file(text: str, *, source: Path) -> dict[str, str]:
    """Parse ``KEY=VALUE`` lines (``#`` comments, optional ``export`` and surrounding quotes).

    There are no inline comments: everything after ``=`` is the value.
    """
    values: dict[str, str] = {}
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not _ENV_KEY_RE.match(key):
            # Never echo the line: it is most likely a malformed secret.
            raise ConfigError(f"invalid env file {source}:{lineno}: expected KEY=VALUE")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key] = value
    return values


def interpolate(data: Any, variables: Mapping[str, str]) -> Any:
    """Replace ``${VAR}`` in every string value; report every undefined variable at once."""
    missing: dict[str, list[str]] = {}
    result = _interpolate(data, variables, (), missing)
    if missing:
        details = "\n".join(
            f"  {name} (used at {', '.join(locations)})" for name, locations in missing.items()
        )
        raise ConfigError(
            f"undefined variables in manifest:\n{details}",
            hint="define them in repository.env_file or in the environment",
        )
    return result


def _interpolate(
    data: Any,
    variables: Mapping[str, str],
    loc: tuple[str | int, ...],
    missing: dict[str, list[str]],
) -> Any:
    if isinstance(data, str):

        def substitute(match: re.Match[str]) -> str:
            if match.group(0) == "$$":
                return "$"
            name = match.group(1)
            if not _ENV_KEY_RE.match(name):
                raise ConfigError(
                    f"invalid variable reference '${{{name}}}' at {format_loc(loc)}",
                    hint="use ${NAME} with letters, digits and '_', or '$$' for a literal '$'",
                )
            if name not in variables:
                missing.setdefault(name, []).append(format_loc(loc))
                return ""
            return variables[name]

        return _INTERPOLATION_RE.sub(substitute, data)
    if isinstance(data, dict):
        return {k: _interpolate(v, variables, (*loc, str(k)), missing) for k, v in data.items()}
    if isinstance(data, list):
        return [_interpolate(v, variables, (*loc, i), missing) for i, v in enumerate(data)]
    return data


def _raw_env_file(raw: Mapping[str, Any], environ: Mapping[str, str]) -> Path | None:
    # Read before validation: the env file provides variables the rest of the manifest needs.
    repository = raw.get("repository")
    value = repository.get("env_file") if isinstance(repository, dict) else None
    if not isinstance(value, str):
        return None  # absent, or invalid: reported by validation
    return Path(interpolate(value, environ))


def _read_text(path: Path, label: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError(
            f"{label} not found: {path}",
            hint=f"create it, or point --config / ${CONFIG_ENV_VAR} to the right manifest"
            if label == "manifest"
            else "create it, or fix the path in the manifest",
        ) from None
    except PermissionError:
        raise ConfigError(
            f"cannot read {label} {path}: permission denied", hint="run drkit as root"
        ) from None
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"cannot read {label} {path}: {exc}") from None


def check_private_file(path: Path, *, label: str = "file") -> None:
    """Require a regular file, owned by the current user, with no group/other permissions."""
    try:
        st = path.stat()
    except FileNotFoundError:
        raise ConfigError(
            f"{label} not found: {path}", hint="create it, or fix its path in the manifest"
        ) from None
    except PermissionError:
        raise ConfigError(
            f"cannot access {label} {path}: permission denied", hint="run drkit as root"
        ) from None
    if not stat.S_ISREG(st.st_mode):
        raise ConfigError(f"{label} {path} is not a regular file")
    mode = stat.S_IMODE(st.st_mode)
    if mode & 0o077:
        raise InsecurePermissionsError(
            f"{label} {path} is accessible by other users (mode {mode:04o})",
            hint=f"chmod 600 {path}",
        )
    euid = os.geteuid()
    if st.st_uid != euid:
        raise InsecurePermissionsError(
            f"{label} {path} is owned by uid {st.st_uid}, not by the current user (uid {euid})",
            hint=f"chown {euid} {path}",
        )


def format_loc(loc: Iterable[str | int]) -> str:
    out = ""
    for part in loc:
        out += f"[{part}]" if isinstance(part, int) else (f".{part}" if out else str(part))
    return out or "<root>"


def format_validation_error(exc: ValidationError) -> str:
    lines = []
    for error in exc.errors(include_url=False, include_input=False, include_context=False):
        message = error["msg"].removeprefix("Value error, ")
        lines.append(f"  {format_loc(error['loc'])}: {message}")
    return "\n".join(lines)
