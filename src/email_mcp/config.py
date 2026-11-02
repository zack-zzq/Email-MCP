"""Environment-only configuration; no dotenv loading or credential persistence."""

import os
import ssl
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ConfigError


def _integer(env: Mapping[str, str], key: str, default: int, low: int, high: int) -> int:
    try:
        value = int(env.get(key, str(default)))
    except ValueError:
        raise ConfigError(f"{key} must be an integer.") from None
    if not low <= value <= high:
        raise ConfigError(f"{key} must be between {low} and {high}.")
    return value


def _boolean(env: Mapping[str, str], key: str, default: bool = False) -> bool:
    value = env.get(key, str(default)).strip().lower()
    if value not in {"true", "false", "1", "0"}:
        raise ConfigError(f"{key} must be true or false.")
    return value in {"true", "1"}


@dataclass(frozen=True)
class Endpoint:
    protocol: str
    host: str
    port: int
    tls: str
    username: str = field(repr=False)
    password: str = field(repr=False)
    auth_type: str = "password"
    access_token: str = field(default="", repr=False)


@dataclass(frozen=True)
class Settings:
    endpoints: dict[str, Endpoint]
    from_address: str
    timeout: int = 30
    read_protocol: str = "imap"
    read_only: bool = False
    attachment_dir: Path = field(default_factory=lambda: Path.home() / ".email-mcp" / "attachments")
    ca_file: str | None = None
    max_message_bytes: int = 25 * 1024 * 1024
    max_attachment_bytes: int = 20 * 1024 * 1024
    max_body_chars: int = 50_000
    search_scan_limit: int = 200

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if environ is None else environ
        endpoints = {}
        defaults = {"imap": (993, 143), "pop3": (995, 110), "smtp": (465, 587)}
        for protocol, ports in defaults.items():
            prefix = f"EMAIL_{protocol.upper()}_"
            host = env.get(prefix + "HOST", "").strip()
            if not host:
                continue
            if any(c.isspace() or ord(c) < 32 for c in host) or "/" in host:
                raise ConfigError(f"{prefix}HOST must be a host name or IP address, without a URL.")
            tls = env.get(prefix + "TLS", "starttls" if protocol == "smtp" else "ssl").lower()
            if tls not in {"ssl", "starttls", "plain"}:
                raise ConfigError(f"{prefix}TLS must be ssl, starttls or plain.")
            auth = env.get(prefix + "AUTH_TYPE", env.get("EMAIL_AUTH_TYPE", "password")).lower()
            if auth not in {"password", "oauth2", "none"} or (
                auth == "none" and protocol != "smtp"
            ):
                raise ConfigError(
                    f"{prefix}AUTH_TYPE must be password or oauth2 (SMTP also: none)."
                )
            user = env.get(prefix + "USERNAME", env.get("EMAIL_USERNAME", ""))
            password = env.get(prefix + "PASSWORD", env.get("EMAIL_PASSWORD", ""))
            token = env.get(prefix + "ACCESS_TOKEN", env.get("EMAIL_ACCESS_TOKEN", ""))
            if auth != "none" and (not user or any(c in user for c in "\r\n\x00\x01")):
                raise ConfigError(f"Set EMAIL_USERNAME or {prefix}USERNAME to a valid login name.")
            if auth == "password" and not password:
                raise ConfigError(f"Set EMAIL_PASSWORD or {prefix}PASSWORD.")
            if auth == "oauth2" and (not token or any(c in token for c in "\r\n\x00\x01")):
                raise ConfigError(f"Set EMAIL_ACCESS_TOKEN or {prefix}ACCESS_TOKEN.")
            port = _integer(env, prefix + "PORT", ports[0 if tls == "ssl" else 1], 1, 65535)
            if protocol == "smtp" and tls == "plain" and prefix + "PORT" not in env:
                port = 25
            endpoints[protocol] = Endpoint(protocol, host, port, tls, user, password, auth, token)
        if not endpoints:
            raise ConfigError(
                "Set at least one EMAIL_IMAP_HOST, EMAIL_POP3_HOST or EMAIL_SMTP_HOST."
            )
        read_protocol = env.get("EMAIL_READ_PROTOCOL", "imap" if "imap" in endpoints else "pop3")
        if read_protocol not in {"imap", "pop3"}:
            raise ConfigError("EMAIL_READ_PROTOCOL must be imap or pop3.")
        if any(p in endpoints for p in ("imap", "pop3")) and read_protocol not in endpoints:
            raise ConfigError("EMAIL_READ_PROTOCOL must select a configured receiving protocol.")
        from_address = env.get(
            "EMAIL_FROM", endpoints.get("smtp", next(iter(endpoints.values()))).username
        )
        return cls(
            endpoints=endpoints,
            from_address=from_address,
            read_protocol=read_protocol,
            timeout=_integer(env, "EMAIL_TIMEOUT", 30, 1, 300),
            read_only=_boolean(env, "EMAIL_READ_ONLY"),
            attachment_dir=Path(
                env.get("EMAIL_ATTACHMENT_DIR", str(Path.home() / ".email-mcp" / "attachments"))
            )
            .expanduser()
            .resolve(),
            ca_file=env.get("EMAIL_TLS_CA_FILE") or None,
            max_message_bytes=_integer(
                env, "EMAIL_MAX_MESSAGE_BYTES", 25 * 1024 * 1024, 1024, 10**9
            ),
            max_attachment_bytes=_integer(
                env, "EMAIL_MAX_ATTACHMENT_BYTES", 20 * 1024 * 1024, 1, 10**9
            ),
            max_body_chars=_integer(env, "EMAIL_MAX_BODY_CHARS", 50_000, 100, 10**7),
            search_scan_limit=_integer(env, "EMAIL_SEARCH_SCAN_LIMIT", 200, 1, 2000),
        )

    def endpoint(self, protocol: str) -> Endpoint:
        if protocol not in self.endpoints:
            raise ConfigError(
                f"{protocol.upper()} is not configured; set EMAIL_{protocol.upper()}_HOST."
            )
        return self.endpoints[protocol]

    def ssl_context(self) -> ssl.SSLContext:
        return ssl.create_default_context(cafile=self.ca_file)

    def require_write(self) -> None:
        if self.read_only:
            raise ConfigError("This server is read-only (EMAIL_READ_ONLY=true).")
