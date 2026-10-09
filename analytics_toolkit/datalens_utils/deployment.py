"""SDK-free BI deployment identities."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlsplit

from .errors import DataLensConfigurationError


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DataLensConfigurationError(label + " must be a nonempty string.")
    return value


@dataclass(frozen=True)
class TargetLocation:
    """Folder path or exactly one workbook reference; contains no SDK objects."""

    kind: str
    value: str
    reference: str = "id"

    def __post_init__(self) -> None:
        _text(self.value, "target")
        if self.kind not in {"path", "workbook"} or self.reference not in {"id", "key"}:
            msg = "Invalid target location."
            raise DataLensConfigurationError(msg)
        if self.kind == "path" and self.reference != "id":
            msg = "Folder targets require a path."
            raise DataLensConfigurationError(msg)

    @classmethod
    def path(cls, path: str) -> TargetLocation:
        return cls("path", _text(path, "path").rstrip("/") or "/")

    @classmethod
    def workbook(cls, *, by_id: str | None = None, key: str | None = None) -> TargetLocation:
        if (by_id is None) == (key is None):
            msg = "Supply exactly one workbook by_id or key."
            raise DataLensConfigurationError(msg)
        return cls(
            "workbook",
            _text(by_id if by_id is not None else key, "workbook"),
            "id" if by_id is not None else "key",
        )


@dataclass(frozen=True, init=False)
class BIProjectDeployment:
    """Yandex Cloud or Enterprise identity and external authentication references."""

    dashboard_name: str
    target: TargetLocation
    installation: str = "yc"
    organization_id: str | None = None
    yc_profile: str | None = None
    yc_binary: str = "yc"
    base_url: str | None = None
    token_env: str | None = None

    def __init__(  # noqa: PLR0913 - Explicit public deployment contract.
        self,
        dashboard_name: str,
        target: TargetLocation,
        *,
        installation: str = "yc",
        organization_id: str | None = None,
        yc_profile: str | None = None,
        yc_binary: str = "yc",
        base_url: str | None = None,
        token_env: str | None = None,
    ) -> None:
        values = locals()
        for name in (
            "dashboard_name",
            "target",
            "installation",
            "organization_id",
            "yc_profile",
            "yc_binary",
            "base_url",
            "token_env",
        ):
            object.__setattr__(self, name, values[name])
        self.__post_init__()

    def __post_init__(self) -> None:
        _text(self.dashboard_name, "dashboard_name")
        if not isinstance(self.target, TargetLocation):
            msg = "target must be a TargetLocation."
            raise DataLensConfigurationError(msg)
        if self.installation not in {"yc", "enterprise"}:
            msg = "installation must be yc or enterprise."
            raise DataLensConfigurationError(msg)
        if self.installation == "enterprise":
            parsed = urlsplit(self.base_url or "")
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.netloc
                or parsed.username
                or parsed.password
            ):
                msg = "Enterprise requires an HTTP(S) base_url without credentials."
                raise DataLensConfigurationError(msg)
        if self.token_env is not None:
            _text(self.token_env, "token_env")

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)
