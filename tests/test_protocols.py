import imaplib
from dataclasses import replace
from unittest.mock import MagicMock

import pytest

from email_mcp.connections import imap_connection, pop3_connection, smtp_connection
from email_mcp.errors import MailError
from email_mcp.mime import parse
from email_mcp.service import MailService

from .fake_servers import State, mail_server


def test_imap_list_read_search_and_no_implicit_seen(configured):
    with mail_server("imap") as (port, state):
        service = MailService(configured(port=port))
        folders = service.list_folders()["folders"]
        assert folders[0]["name"] == "INBOX"
        page = service.list_messages(limit=1)
        assert page["total"] == 2 and page["next_offset"] == 1
        assert page["messages"][0]["id"] == "42:102"
        assert service.list_messages(limit=1, offset=1)["messages"][0]["id"] == "42:101"
        message = service.read_message("42:102")
        assert message["subject"] == "中文主题：项目进展"
        assert "你好" in message["text"]
        assert "\\Seen" not in message["flags"]
        assert any("EXAMINE" in command for command in state.commands)
        assert not any("STORE" in command or "CLOSE" in command for command in state.commands)
        assert any("BODY.PEEK[]" in command for command in state.commands)
        result = service.search_messages(
            subject="项目",
            text="进展",
            recipient="copy@example.com",
            since="2026-11-01",
            before="2026-11-02",
            unread=True,
        )
        assert [item["id"] for item in result["messages"]] == ["42:102"]
        assert not service.search_messages(subject="absent")["messages"]
        assert not service.search_messages(since="2026-11-02")["messages"]


def test_search_discloses_bounded_scan(configured):
    with mail_server("imap") as (port, _):
        service = MailService(replace(configured(port=port), search_scan_limit=1))
        result = service.search_messages(subject="Older")
        assert result["scan_truncated"] and result["scanned"] == 1
        assert result["matches_in_scan"] == 0
        assert result["total_in_folder"] == 2


def test_uidvalidity_change_rejected_before_modifying(configured):
    with mail_server("imap", State(validity=43)) as (port, state):
        service = MailService(configured(port=port))
        with pytest.raises(MailError, match="UIDVALIDITY changed"):
            service.delete_message("42:102", permanent=True)
        assert len(state.messages) == 2
        assert not any("STORE" in line or "EXPUNGE" in line for line in state.commands)


def test_imap_single_uid_delete_does_not_expunge_other_mail(configured):
    with mail_server("imap") as (port, state):
        state.flags[101] = {"\\Deleted"}
        service = MailService(configured(port=port))
        result = service.delete_message("42:102", permanent=True)
        assert result["status"] == "deleted"
        assert set(state.messages) == {101}
        assert any("UID EXPUNGE 102" in line for line in state.commands)
        assert not any(line.endswith(" EXPUNGE") or "CLOSE" in line for line in state.commands)


def test_imap_soft_delete_and_flags(configured):
    with mail_server("imap") as (port, state):
        service = MailService(configured(port=port))
        service.set_flags("42:102", seen=True, flagged=True)
        assert state.flags[102] == {"\\Seen", "\\Flagged"}
        service.set_flags("42:102", seen=False)
        assert "\\Seen" not in state.flags[102]
        assert service.delete_message("42:102")["status"] == "marked_deleted"
        assert 102 in state.messages and "\\Deleted" in state.flags[102]
        assert not any("EXPUNGE" in line for line in state.commands)


def test_imap_move_capability_and_absent_uid(configured):
    with mail_server("imap") as (port, state):
        service = MailService(configured(port=port))
        with pytest.raises(MailError, match="no longer exists"):
            service.set_flags("42:999", seen=True)
        assert service.move_message("42:102", "已发送邮件")["moved"]
        assert 102 not in state.messages
    with mail_server("imap", State(capabilities="IMAP4rev1")) as (port, state):
        service = MailService(configured(port=port))
        with pytest.raises(MailError, match="MOVE"):
            service.move_message("42:102", "Trash")
        with pytest.raises(MailError, match="UIDPLUS"):
            service.delete_message("42:102", permanent=True)
        assert len(state.messages) == 2 and not state.flags


def test_pop3_list_read_and_top_fallback(configured):
    for supported in (True, False):
        with mail_server("pop3", State(top_supported=supported)) as (port, state):
            service = MailService(configured("pop3", port))
            page = service.list_messages()
            assert page["messages"][0]["id"] == "UID-102"
            assert service.list_folders()["folders"][0]["name"] == "INBOX"
            body = service.read_message("UID-102")
            assert "你好" in body["text"] and ".以点开头" in body["text"]
            assert service.search_messages(subject="项目")["matches_in_scan"] == 1
            assert any(command.startswith("TOP") for command in state.commands)
            assert not any(command.startswith("DELE") for command in state.commands)
            with pytest.raises(MailError, match="read/unread"):
                service.search_messages(unread=True)
            with pytest.raises(MailError, match="INBOX"):
                service.list_messages(folder="Archive")


def test_pop3_uidl_survives_renumbering_and_requires_explicit_delete(configured):
    with mail_server("pop3") as (port, state):
        service = MailService(configured("pop3", port))
        service.list_messages()
        state.messages.pop(101)
        assert service.read_message("UID-102")["subject"] == "中文主题：项目进展"
        with pytest.raises(MailError, match="permanent=true"):
            service.delete_message("UID-102")
        assert service.delete_message("UID-102", permanent=True)["status"] == "deleted"
        assert not state.messages
        assert "DELE 1" in state.commands


def test_pop3_quit_commit_error_does_not_report_success(configured):
    import poplib

    with mail_server("pop3", State(fail_pop_quit=True)) as (port, state):
        with pytest.raises(poplib.error_proto):
            MailService(configured("pop3", port)).delete_message("UID-102", permanent=True)
        assert 102 in state.messages


def test_attachment_download_is_confined_unique_and_binary_exact(configured):
    with mail_server("imap") as (port, _):
        settings = configured(port=port)
        service = MailService(settings)
        result = service.download_attachment("42:102", "1")
        from pathlib import Path

        path = Path(result["path"])
        assert path.is_relative_to(settings.attachment_dir)
        assert "/" not in result["filename"] and "\\" not in result["filename"]
        assert path.read_bytes() == b"\x00\x01attachment-data\xff"
        assert service.download_attachment("42:102", "1")["path"] != result["path"]
        with pytest.raises(MailError, match="attachment_id"):
            service.download_attachment("42:102", "100")
        small = MailService(replace(settings, max_attachment_bytes=2))
        with pytest.raises(MailError, match="MAX_ATTACHMENT"):
            small.download_attachment("42:102", "1")


def test_attachment_long_unicode_filename_fits_filesystem_limit(configured):
    from email import policy
    from email.message import EmailMessage
    from pathlib import Path

    message = EmailMessage(policy=policy.SMTP)
    message.set_content("body")
    message.add_attachment(
        b"binary", maintype="application", subtype="octet-stream", filename="中文" * 100 + ".bin"
    )
    with mail_server("imap", State(messages={102: message.as_bytes()})) as (port, _):
        result = MailService(configured(port=port)).download_attachment("42:102", "1")
        path = Path(result["path"])
        assert len(path.name.encode("utf-8")) < 255 and path.read_bytes() == b"binary"


def test_oversized_body_not_downloaded_and_headers_still_available(configured):
    with mail_server("imap") as (port, state):
        service = MailService(replace(configured(port=port), max_message_bytes=1024))
        assert len(service.list_messages()["messages"]) == 2
        state.commands.clear()
        with pytest.raises(MailError, match="MAX_MESSAGE_BYTES"):
            service.read_message("42:102")
        assert not any("BODY.PEEK[]" in command for command in state.commands)


def test_smtp_bcc_mime_partial_acceptance_and_attachment(configured):
    with mail_server("smtp") as (port, state):
        settings = configured("smtp", port)
        settings.attachment_dir.mkdir()
        (settings.attachment_dir / "报告.txt").write_text("附件中文", encoding="utf-8")
        service = MailService(settings)
        result = service.send_message(
            ["ok@example.com", "rejected@example.com"],
            "中文标题",
            "纯文本正文",
            cc=["copy@example.com"],
            bcc=["hidden@example.com"],
            html_body="<p>正文</p>",
            attachments=["报告.txt"],
            in_reply_to="<parent@example.com>",
            references="<root@example.com>",
        )
        assert result["status"] == "partially_accepted"
        assert result["refused_recipients"] == [{"address": "rejected@example.com", "code": 550}]
        wire = state.sent[0]
        parsed = parse(wire["raw"])
        assert parsed["Subject"] == "中文标题" and parsed["Bcc"] is None
        assert b"hidden@example.com" not in wire["raw"]
        assert any("hidden@example.com" in address for address in wire["recipients"])
        assert parsed["In-Reply-To"] == "<parent@example.com>"
        assert list(parsed.iter_attachments())[0].get_payload(decode=True) == "附件中文".encode()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"to": ["safe@example.com\r\nBcc: victim@example.com"]},
        {"subject": "hi\r\nBcc: victim@example.com"},
        {"to": ["a@example.com,b@example.com"]},
        {"to": ["invalid"]},
        {"attachments": ["../outside-secret.txt"]},
        {"to": []},
    ],
)
def test_smtp_rejects_header_and_path_injection_before_connecting(configured, kwargs):
    service = MailService(configured("smtp"))
    args = {"to": ["ok@example.com"], "subject": "hi", "body": "text", **kwargs}
    with pytest.raises(MailError):
        service.send_message(**args)


def test_attachment_symlink_escape_rejected(configured, tmp_path):
    settings = configured("smtp")
    settings.attachment_dir.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("secret")
    link = settings.attachment_dir / "linked.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("Host does not allow creating symlinks")
    with pytest.raises(MailError, match="inside"):
        MailService(settings).send_message(
            ["ok@example.com"], "hi", "text", attachments=["linked.txt"]
        )


def test_readonly_rejects_all_mail_writes(configured):
    service = MailService(replace(configured(), read_only=True))
    calls = [
        lambda: service.send_message(["a@example.com"], "hi", "text"),
        lambda: service.set_flags("42:102", seen=True),
        lambda: service.move_message("42:102", "Trash"),
        lambda: service.delete_message("42:102", permanent=True),
    ]
    for call in calls:
        with pytest.raises(MailError, match="read-only"):
            call()


@pytest.mark.parametrize("protocol", ["imap", "pop3", "smtp"])
def test_starttls_is_required_before_authentication(configured, monkeypatch, protocol):
    import email_mcp.connections as connections

    client = MagicMock()
    module, constructor, context, tls_method = {
        "imap": (connections.imaplib, "IMAP4", imap_connection, "starttls"),
        "pop3": (connections.poplib, "POP3", pop3_connection, "stls"),
        "smtp": (connections.smtplib, "SMTP", smtp_connection, "starttls"),
    }[protocol]
    monkeypatch.setattr(module, constructor, MagicMock(return_value=client))
    client.starttls.side_effect = RuntimeError("TLS failed with secret")
    client.stls.side_effect = RuntimeError("TLS failed with secret")
    settings = configured(protocol, **{f"EMAIL_{protocol.upper()}_TLS": "starttls"})
    with pytest.raises(RuntimeError):
        with context(settings):
            pytest.fail("TLS failure must stop connection")
    assert not client.login.called and not client.user.called and not client.pass_.called
    assert getattr(client, tls_method).called


def test_imap_login_failure_still_disconnects(configured, monkeypatch):
    import email_mcp.connections as connections

    client = MagicMock()
    auth_error = imaplib.IMAP4.error
    client.login.side_effect = auth_error("bad password")
    monkeypatch.setattr(connections.imaplib, "IMAP4", MagicMock(return_value=client))
    with pytest.raises(auth_error):
        with imap_connection(configured()):
            pass
    assert client.logout.called
