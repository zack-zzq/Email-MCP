import imaplib
import ssl
from dataclasses import replace
from email import policy
from email.message import EmailMessage

import pytest

from email_mcp.config import Settings
from email_mcp.errors import ConfigError, safe_error
from email_mcp.mime import (
    attachment_parts,
    decode_folder,
    encode_folder,
    full_message,
    parse,
    parse_folder_list,
)

from .fake_servers import example_message


def test_config_defaults_and_protocol_overrides():
    settings = Settings.from_env(
        {
            "EMAIL_USERNAME": "common@example.com",
            "EMAIL_PASSWORD": "secret",
            "EMAIL_IMAP_HOST": "imap.example.com",
            "EMAIL_SMTP_HOST": "smtp.example.com",
            "EMAIL_SMTP_USERNAME": "sender@example.com",
            "EMAIL_SMTP_PASSWORD": "smtp-secret",
        }
    )
    assert settings.read_protocol == "imap"
    assert settings.endpoint("imap").port == 993
    assert settings.endpoint("smtp").port == 587
    assert settings.endpoint("smtp").username == "sender@example.com"
    assert settings.endpoint("smtp").password == "smtp-secret"
    assert "secret" not in repr(settings)
    context = settings.ssl_context()
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname


@pytest.mark.parametrize(
    "override",
    [
        {"EMAIL_IMAP_PORT": "oops"},
        {"EMAIL_IMAP_PORT": "70000"},
        {"EMAIL_IMAP_TLS": "auto"},
        {"EMAIL_READ_ONLY": "maybe"},
        {"EMAIL_IMAP_HOST": "https://mail.example.com"},
        {"EMAIL_TIMEOUT": "0"},
        {"EMAIL_READ_PROTOCOL": "smtp"},
        {"EMAIL_READ_PROTOCOL": "pop3"},
        {"EMAIL_IMAP_AUTH_TYPE": "none"},
        {"EMAIL_SEARCH_SCAN_LIMIT": "2001"},
    ],
)
def test_invalid_config_fails_without_echoing_values(override):
    env = {"EMAIL_USERNAME": "user", "EMAIL_PASSWORD": "secret", "EMAIL_IMAP_HOST": "localhost"}
    with pytest.raises(ConfigError) as error:
        Settings.from_env({**env, **override})
    assert "secret" not in str(error.value)


def test_no_implicit_credentials_or_dotenv(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("EMAIL_PASSWORD=secret\nEMAIL_IMAP_HOST=localhost")
    with pytest.raises(ConfigError, match="at least one"):
        Settings.from_env({})
    with pytest.raises(ConfigError, match="PASSWORD"):
        Settings.from_env({"EMAIL_IMAP_HOST": "localhost", "EMAIL_USERNAME": "user"})


def test_oauth2_and_unauthenticated_relay():
    oauth = Settings.from_env(
        {
            "EMAIL_IMAP_HOST": "localhost",
            "EMAIL_USERNAME": "user",
            "EMAIL_AUTH_TYPE": "oauth2",
            "EMAIL_ACCESS_TOKEN": "token-secret",
        }
    )
    assert oauth.endpoint("imap").auth_type == "oauth2"
    assert "token-secret" not in repr(oauth)
    relay = Settings.from_env(
        {
            "EMAIL_SMTP_HOST": "localhost",
            "EMAIL_SMTP_AUTH_TYPE": "none",
            "EMAIL_SMTP_TLS": "plain",
            "EMAIL_FROM": "test@example.com",
        }
    )
    assert relay.endpoint("smtp").port == 25


def test_cjk_mime_and_unsafe_attachment_name():
    decoded = full_message(parse(example_message()), 10_000, True)
    assert decoded["subject"] == "中文主题：项目进展"
    assert "你好" in decoded["text"]
    assert ".以点开头" in decoded["text"]
    assert decoded["attachments"][0]["filename"] == "../../中文报告.bin"
    assert decoded["attachments"][0]["attachment_id"] == "1"
    assert "<p>你好" in decoded["html"]
    truncated = full_message(parse(example_message()), 2, False)
    assert truncated["text_truncated"] and len(truncated["text"]) == 2
    assert "html" not in truncated


def test_html_only_and_bad_charset():
    raw = (
        b"Content-Type: text/html; charset=nonexistent\r\n\r\n"
        b"<style>secret</style><p>Hello &amp; hi</p><script>secret</script>"
    )
    assert full_message(parse(raw), 1000, False)["text"] == "Hello & hi"


def test_single_part_binary_message_is_downloadable_attachment():
    raw = b"Content-Type: application/octet-stream\r\n\r\nbinary content"
    result = full_message(parse(raw), 1000, False)
    assert result["text"] == ""
    assert result["attachments"][0]["size"] == len(b"binary content")


def test_attached_email_body_not_mixed_into_parent():
    parent, child = EmailMessage(policy=policy.SMTP), EmailMessage(policy=policy.SMTP)
    parent.set_content("outer")
    child.set_content("inner secret")
    parent.add_attachment(child, filename="forwarded.eml")
    parsed = parse(parent.as_bytes())
    assert len(attachment_parts(parsed)) == 1
    result = full_message(parsed, 1000, False)
    assert "inner secret" not in result["text"]
    assert result["attachments"][0]["size"] > 0


@pytest.mark.parametrize(
    "name", ["INBOX", "已发送邮件", "A&B", 'A "quoted" folder', "test\\folder"]
)
def test_imap_folder_round_trip(name):
    encoded = encode_folder(name)[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    assert decode_folder(encoded.encode("ascii")) == name


def test_literal_list_response():
    result = parse_folder_list([(b'(\\HasNoChildren) "/" {5}', b"INBOX"), b")"])
    assert result[0]["name"] == "INBOX"


def test_raw_server_errors_are_redacted():
    for exc in (imaplib.IMAP4.error("password=top-secret"), RuntimeError("token=top-secret")):
        assert "top-secret" not in safe_error(exc)


def test_account_info_does_not_return_secrets(configured):
    from email_mcp.service import MailService

    settings = replace(configured(), read_only=True)
    info = str(MailService(settings).account_info())
    assert "fixture-password" not in info and "user@example.com" not in info
