"""Short-lived mail sessions with strict TLS and deterministic cleanup."""

import base64
import imaplib
import poplib
import smtplib
from collections.abc import Iterator
from contextlib import contextmanager, suppress

from .config import Endpoint, Settings
from .errors import MailError


def _oauth_payload(endpoint: Endpoint) -> str:
    return f"user={endpoint.username}\x01auth=Bearer {endpoint.access_token}\x01\x01"


@contextmanager
def imap_connection(settings: Settings) -> Iterator[imaplib.IMAP4]:
    endpoint = settings.endpoint("imap")
    client = None
    try:
        if endpoint.tls == "ssl":
            client = imaplib.IMAP4_SSL(
                endpoint.host,
                endpoint.port,
                ssl_context=settings.ssl_context(),
                timeout=settings.timeout,
            )
        else:
            client = imaplib.IMAP4(endpoint.host, endpoint.port, timeout=settings.timeout)
            if endpoint.tls == "starttls":
                client.starttls(ssl_context=settings.ssl_context())
        if endpoint.auth_type == "oauth2":
            sent = False

            def respond(challenge: bytes) -> bytes:
                nonlocal sent
                if sent:
                    return b""
                sent = True
                return _oauth_payload(endpoint).encode("utf-8")

            client.authenticate("XOAUTH2", respond)
        else:
            client.login(endpoint.username, endpoint.password)
        # Several servers only advertise mailbox extensions after authentication.
        data = imap_ok(client.capability(), "CAPABILITY")
        client.capabilities = tuple(b" ".join(data).decode("ascii").upper().split())
        yield client
    finally:
        if client is not None:
            # CLOSE would expunge other clients' deleted messages. LOGOUT does not.
            try:
                client.logout()
            except Exception:
                with suppress(Exception):
                    client.shutdown()


@contextmanager
def pop3_connection(settings: Settings) -> Iterator[poplib.POP3]:
    endpoint = settings.endpoint("pop3")
    client = None
    success = False
    try:
        if endpoint.tls == "ssl":
            client = poplib.POP3_SSL(
                endpoint.host,
                endpoint.port,
                context=settings.ssl_context(),
                timeout=settings.timeout,
            )
        else:
            client = poplib.POP3(endpoint.host, endpoint.port, timeout=settings.timeout)
            if endpoint.tls == "starttls":
                client.stls(context=settings.ssl_context())
        if endpoint.auth_type == "oauth2":
            token = base64.b64encode(_oauth_payload(endpoint).encode("utf-8")).decode("ascii")
            client._shortcmd("AUTH XOAUTH2 " + token)
        else:
            client.user(endpoint.username)
            client.pass_(endpoint.password)
        yield client
        success = True
    finally:
        if client is not None:
            if success:
                try:
                    client.quit()
                finally:
                    with suppress(Exception):
                        client.close()
            else:
                # An abnormal disconnect rolls back all DELE marks; don't risk a QUIT commit.
                with suppress(Exception):
                    client.close()


@contextmanager
def smtp_connection(settings: Settings) -> Iterator[smtplib.SMTP]:
    endpoint = settings.endpoint("smtp")
    client = None
    try:
        if endpoint.tls == "ssl":
            client = smtplib.SMTP_SSL(
                endpoint.host,
                endpoint.port,
                context=settings.ssl_context(),
                timeout=settings.timeout,
            )
        else:
            client = smtplib.SMTP(endpoint.host, endpoint.port, timeout=settings.timeout)
        client.ehlo_or_helo_if_needed()
        if endpoint.tls == "starttls":
            client.starttls(context=settings.ssl_context())
            client.ehlo()
        if endpoint.auth_type == "oauth2":
            client.auth(
                "XOAUTH2",
                lambda challenge=None: _oauth_payload(endpoint) if challenge is None else "",
            )
        elif endpoint.auth_type == "password":
            client.login(endpoint.username, endpoint.password)
        yield client
    finally:
        if client is not None:
            # A failed QUIT after DATA acceptance must never turn success into a retry.
            try:
                client.quit()
            except Exception:
                with suppress(Exception):
                    client.close()


def imap_ok(result: tuple, operation: str) -> list:
    status, data = result
    if status != "OK":
        raise MailError(f"IMAP {operation} was rejected by the server.")
    return data
