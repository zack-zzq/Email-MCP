import base64
import smtplib
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

from email_mcp.connections import imap_connection, pop3_connection, smtp_connection
from email_mcp.errors import MailError
from email_mcp.service import MailService

from .fake_servers import State, mail_server


@pytest.mark.parametrize("protocol", ["imap", "pop3", "smtp"])
def test_oauth2_authenticates_on_real_protocol_wire(configured, protocol):
    with mail_server(protocol) as (port, state):
        settings = configured(
            protocol, port, EMAIL_AUTH_TYPE="oauth2", EMAIL_ACCESS_TOKEN="test-token"
        )
        result = MailService(settings).check_connection(protocol)
        assert result["ok"]
        payloads = []
        for command in state.commands:
            if command.startswith("AUTH XOAUTH2 "):
                payloads.append(command.removeprefix("AUTH XOAUTH2 "))
            elif len(command) > 20 and " " not in command:
                payloads.append(command)
        assert any(b"auth=Bearer test-token" in base64.b64decode(value) for value in payloads)


def test_post_auth_capabilities_allow_move(configured):
    with mail_server("imap", State(preauth_capabilities="IMAP4rev1 AUTH=PLAIN")) as (port, _):
        assert MailService(configured(port=port)).move_message("42:102", "Trash")["moved"]


@pytest.mark.parametrize("protocol", ["imap", "pop3", "smtp"])
def test_implicit_tls_uses_verified_context(configured, protocol, monkeypatch):
    import email_mcp.connections as connections

    client = MagicMock()
    client.capability.return_value = ("OK", [b"IMAP4rev1"])
    module, constructor, context, key = {
        "imap": (connections.imaplib, "IMAP4_SSL", imap_connection, "ssl_context"),
        "pop3": (connections.poplib, "POP3_SSL", pop3_connection, "context"),
        "smtp": (connections.smtplib, "SMTP_SSL", smtp_connection, "context"),
    }[protocol]
    factory = MagicMock(return_value=client)
    monkeypatch.setattr(module, constructor, factory)
    settings = configured(protocol, **{f"EMAIL_{protocol.upper()}_TLS": "ssl"})
    with context(settings):
        pass
    import ssl

    tls = factory.call_args.kwargs[key]
    assert tls.verify_mode == ssl.CERT_REQUIRED and tls.check_hostname
    assert not client.starttls.called and not client.stls.called


def test_uncertain_send_is_safe_and_never_retried(configured, monkeypatch):
    import email_mcp.service as service_module

    client = MagicMock()
    client.send_message.side_effect = smtplib.SMTPServerDisconnected("secret server payload")

    @contextmanager
    def connection(settings):
        yield client

    monkeypatch.setattr(service_module, "smtp_connection", connection)
    with pytest.raises(MailError, match="uncertain") as error:
        MailService(configured("smtp")).send_message(["ok@example.com"], "hi", "body")
    assert "secret server payload" not in str(error.value)
    assert client.send_message.call_count == 1
