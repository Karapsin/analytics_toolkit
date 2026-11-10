"""Preserve native instance identity through legacy hostname factory callbacks."""

from __future__ import annotations


class RecoveryEndpoint(str):
    """A hostname compatible with existing callbacks, carrying its discovered TCP port."""

    __slots__ = ("tcp_port",)

    tcp_port: int

    def __new__(cls, host: str, tcp_port: int) -> RecoveryEndpoint:  # noqa: PYI034 - Python 3.8 lacks typing.Self
        endpoint = super().__new__(cls, host)
        endpoint.tcp_port = int(tcp_port)
        return endpoint

    @property
    def identity(self) -> str:
        return f"{self}:{self.tcp_port}"


def endpoint_identity(host: str) -> str:
    return host.identity if isinstance(host, RecoveryEndpoint) else host
