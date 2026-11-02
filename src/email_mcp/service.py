"""Protocol-independent email operations. No credentials are accepted as tool arguments."""

import hashlib
import imaplib
import mimetypes
import poplib
import re
import smtplib
from datetime import UTC, date, datetime
from email import policy
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from pathlib import Path
from typing import Any
from uuid import uuid4

from .config import Settings
from .connections import imap_connection, imap_ok, pop3_connection, smtp_connection
from .errors import MailError, safe_error
from .mime import (
    addresses,
    attachment_bytes,
    attachment_parts,
    bodies,
    clean_header,
    encode_folder,
    full_message,
    html_text,
    parse,
    parse_folder_list,
    summary,
)


def _page(limit: int, offset: int) -> None:
    if not 1 <= limit <= 100 or offset < 0:
        raise MailError("limit must be 1..100 and offset must be non-negative.")


def _imap_ref(message_id: str) -> tuple[str, str]:
    match = re.fullmatch(r"([1-9][0-9]*):([1-9][0-9]*)", message_id)
    if not match:
        raise MailError("IMAP id must be the uidvalidity:uid value returned by list/search.")
    return match.group(1), match.group(2)


def _select(client: Any, folder: str, readonly: bool = True) -> str:
    imap_ok(client.select(encode_folder(folder), readonly=readonly), "SELECT")
    _, values = client.response("UIDVALIDITY")
    value = values[0] if values else None
    if not isinstance(value, bytes) or not re.fullmatch(rb"[1-9][0-9]*", value):
        raise MailError("The IMAP server did not provide a valid UIDVALIDITY.")
    return value.decode("ascii")


def _validate_ref(validity: str, message_id: str) -> str:
    expected, uid = _imap_ref(message_id)
    if expected != validity:
        raise MailError("The mailbox UIDVALIDITY changed. List messages again before continuing.")
    return uid


def _imap_fetch(client: Any, uid: str, max_bytes: int, headers_only: bool = False) -> tuple:
    metadata = imap_ok(client.uid("FETCH", uid, "(UID RFC822.SIZE FLAGS)"), "FETCH metadata")
    chunks = [item for item in metadata if isinstance(item, bytes)]
    joined = b" ".join(chunks)
    size_match = re.search(rb"RFC822.SIZE\s+(\d+)", joined)
    uid_match = re.search(rb"\bUID\s+(\d+)\b", joined)
    if not size_match or not uid_match or uid_match.group(1).decode() != uid:
        raise MailError("Message no longer exists or IMAP returned incomplete metadata.")
    size = int(size_match.group(1))
    if not headers_only and size > max_bytes:
        raise MailError("Message exceeds EMAIL_MAX_MESSAGE_BYTES.")
    section = "HEADER" if headers_only else ""
    # Bound literals even if the server reports an incorrect RFC822.SIZE.
    bound = min(max_bytes, 256 * 1024) if headers_only else max_bytes
    data = imap_ok(client.uid("FETCH", uid, f"(BODY.PEEK[{section}]<0.{bound + 1}>)"), "FETCH body")
    raw = next((item[1] for item in data if isinstance(item, tuple)), None)
    if not isinstance(raw, bytes):
        raise MailError("Message no longer exists or its body could not be read.")
    if len(raw) > bound:
        raise MailError("Message or header exceeds the configured size limit.")
    return parse(raw), size, [f.decode("ascii", "replace") for f in imaplib.ParseFlags(joined)]


def _uid_map(client: Any) -> list[tuple[int, str]]:
    rows = client.uidl()[1]
    result, seen = [], set()
    for row in rows:
        number, uid = row.split(maxsplit=1)
        value = uid.decode("ascii")
        if value in seen or not re.fullmatch(r"[!-~]{1,70}", value) or not number.isdigit():
            raise MailError("POP3 server returned invalid or duplicate UIDL identifiers.")
        seen.add(value)
        result.append((int(number), value))
    return sorted(result, reverse=True)


def _pop_fetch(client: Any, number: int, max_bytes: int, headers_only: bool = False) -> tuple:
    listing = client.list(number).split()
    if len(listing) < 3:
        raise MailError("POP3 server did not return a message size.")
    size = int(listing[2])
    if headers_only:
        try:
            _, lines, _ = client.top(number, 0)
        except poplib.error_proto:
            if size > max_bytes:
                raise MailError(
                    "POP3 TOP is unavailable and the message is too large for RETR."
                ) from None
            _, lines, _ = client.retr(number)
    else:
        if size > max_bytes:
            raise MailError("Message exceeds EMAIL_MAX_MESSAGE_BYTES.")
        _, lines, _ = client.retr(number)
    raw = b"\r\n".join(lines) + b"\r\n"
    if len(raw) > max_bytes:
        raise MailError("Message exceeds EMAIL_MAX_MESSAGE_BYTES.")
    return parse(raw), size, []


class MailService:
    def __init__(self, settings: Settings):
        self.settings = settings

    def _protocol(self, protocol: str = "auto", folder: str = "INBOX") -> str:
        if protocol not in {"auto", "imap", "pop3"}:
            raise MailError("Receiving protocol must be auto, imap or pop3.")
        selected = self.settings.read_protocol if protocol == "auto" else protocol
        self.settings.endpoint(selected)
        if selected == "pop3" and folder.upper() != "INBOX":
            raise MailError("POP3 only supports the INBOX folder.")
        return selected

    def account_info(self) -> dict[str, Any]:
        return {
            "transport": "stdio",
            "read_protocol": self.settings.read_protocol,
            "read_only": self.settings.read_only,
            "protocols": {
                name: {
                    "host": item.host,
                    "port": item.port,
                    "tls": item.tls,
                    "auth_type": item.auth_type,
                }
                for name, item in self.settings.endpoints.items()
            },
            "attachment_dir": str(self.settings.attachment_dir),
            "max_message_bytes": self.settings.max_message_bytes,
            "max_attachment_bytes": self.settings.max_attachment_bytes,
            "search_scan_limit": self.settings.search_scan_limit,
        }

    def check_connection(self, protocol: str = "all") -> dict[str, Any]:
        if protocol not in {"all", "imap", "pop3", "smtp"}:
            raise MailError("Protocol must be all, imap, pop3 or smtp.")
        selected = self.settings.endpoints if protocol == "all" else {protocol: None}
        result = {}
        for name in selected:
            try:
                if name == "imap":
                    with imap_connection(self.settings) as client:
                        imap_ok(client.noop(), "NOOP")
                elif name == "pop3":
                    with pop3_connection(self.settings) as client:
                        client.noop()
                else:
                    with smtp_connection(self.settings) as client:
                        code, _ = client.noop()
                        if code != 250:
                            raise MailError("SMTP NOOP was rejected.")
                result[name] = {"ok": True}
            except Exception as exc:
                result[name] = {"ok": False, "error": safe_error(exc)}
        return {"ok": all(item["ok"] for item in result.values()), "protocols": result}

    def list_folders(self, protocol: str = "auto") -> dict[str, Any]:
        selected = self._protocol(protocol)
        if selected == "pop3":
            return {"protocol": selected, "folders": [{"name": "INBOX", "flags": []}]}
        with imap_connection(self.settings) as client:
            folders = parse_folder_list(imap_ok(client.list(), "LIST"))
        return {"protocol": selected, "folders": folders}

    def list_messages(
        self, folder: str = "INBOX", protocol: str = "auto", limit: int = 20, offset: int = 0
    ) -> dict[str, Any]:
        _page(limit, offset)
        selected = self._protocol(protocol, folder)
        items, skipped = [], []
        if selected == "imap":
            with imap_connection(self.settings) as client:
                validity = _select(client, folder)
                data = imap_ok(client.uid("SEARCH", None, "ALL"), "SEARCH")
                ids = sorted((data[0] or b"").split(), key=int, reverse=True)
                total = len(ids)
                for raw_uid in ids[offset : offset + limit]:
                    uid = raw_uid.decode("ascii")
                    message_id = f"{validity}:{uid}"
                    try:
                        msg, size, flags = _imap_fetch(
                            client, uid, self.settings.max_message_bytes, True
                        )
                        items.append(
                            {"id": message_id, **summary(msg), "size": size, "flags": flags}
                        )
                    except MailError as exc:
                        skipped.append({"id": message_id, "reason": str(exc)})
        else:
            with pop3_connection(self.settings) as client:
                ids = _uid_map(client)
                total = len(ids)
                for number, message_id in ids[offset : offset + limit]:
                    try:
                        msg, size, flags = _pop_fetch(
                            client, number, self.settings.max_message_bytes, True
                        )
                        items.append(
                            {"id": message_id, **summary(msg), "size": size, "flags": flags}
                        )
                    except MailError as exc:
                        skipped.append({"id": message_id, "reason": str(exc)})
        return {
            "protocol": selected,
            "folder": folder,
            "total": total,
            "offset": offset,
            "messages": items,
            "skipped": skipped,
            "next_offset": offset + limit if offset + limit < total else None,
        }

    def search_messages(
        self,
        folder: str = "INBOX",
        protocol: str = "auto",
        sender: str = "",
        recipient: str = "",
        subject: str = "",
        text: str = "",
        since: str = "",
        before: str = "",
        unread: bool | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        _page(limit, offset)
        selected = self._protocol(protocol, folder)
        if selected == "pop3" and unread is not None:
            raise MailError("POP3 does not expose read/unread flags; omit unread.")
        try:
            lower, upper = (
                date.fromisoformat(since) if since else None,
                (date.fromisoformat(before) if before else None),
            )
        except ValueError:
            raise MailError("since and before must be ISO dates (YYYY-MM-DD).") from None
        if lower and upper and lower >= upper:
            raise MailError("since must be earlier than before.")
        matches, skipped = [], []
        scanned = 0

        def inspect_message(msg: EmailMessage, size: int, flags: list, message_id: str) -> None:
            entry = summary(msg)
            if any(
                needle.casefold() not in entry[key].casefold()
                for key, needle in (("from", sender), ("subject", subject))
                if needle
            ):
                return
            if recipient and not any(
                recipient.casefold() in entry[key].casefold() for key in ("to", "cc")
            ):
                return
            if unread is not None and unread != ("\\Seen" not in flags):
                return
            if lower or upper:
                if not entry["date"]:
                    return
                day = datetime.fromisoformat(entry["date"]).date()
                if (lower and day < lower) or (upper and day >= upper):
                    return
            if text:
                plain, html = bodies(msg)
                if text.casefold() not in (plain + "\n" + html_text(html)).casefold():
                    return
            matches.append({"id": message_id, **entry, "size": size, "flags": flags})

        if selected == "imap":
            with imap_connection(self.settings) as client:
                validity = _select(client, folder)
                data = imap_ok(client.uid("SEARCH", None, "ALL"), "SEARCH")
                ids = sorted((data[0] or b"").split(), key=int, reverse=True)
                total = len(ids)
                for raw_uid in ids[: self.settings.search_scan_limit]:
                    uid = raw_uid.decode("ascii")
                    message_id = f"{validity}:{uid}"
                    scanned += 1
                    try:
                        msg, size, flags = _imap_fetch(
                            client, uid, self.settings.max_message_bytes, not bool(text)
                        )
                        inspect_message(msg, size, flags, message_id)
                    except MailError as exc:
                        skipped.append({"id": message_id, "reason": str(exc)})
        else:
            with pop3_connection(self.settings) as client:
                ids = _uid_map(client)
                total = len(ids)
                for number, message_id in ids[: self.settings.search_scan_limit]:
                    scanned += 1
                    try:
                        msg, size, flags = _pop_fetch(
                            client, number, self.settings.max_message_bytes, not bool(text)
                        )
                        inspect_message(msg, size, flags, message_id)
                    except MailError as exc:
                        skipped.append({"id": message_id, "reason": str(exc)})
        return {
            "protocol": selected,
            "folder": folder,
            "total_in_folder": total,
            "scanned": scanned,
            "scan_truncated": scanned < total,
            "matches_in_scan": len(matches),
            "messages": matches[offset : offset + limit],
            "offset": offset,
            "skipped": skipped,
            "next_offset": offset + limit if offset + limit < len(matches) else None,
        }

    def _read(self, message_id: str, folder: str, protocol: str) -> tuple:
        selected = self._protocol(protocol, folder)
        if selected == "imap":
            with imap_connection(self.settings) as client:
                uid = _validate_ref(_select(client, folder), message_id)
                msg, size, flags = _imap_fetch(client, uid, self.settings.max_message_bytes)
        else:
            with pop3_connection(self.settings) as client:
                number = next((n for n, uid in _uid_map(client) if uid == message_id), None)
                if number is None:
                    raise MailError("POP3 message not found; list messages again.")
                msg, size, flags = _pop_fetch(client, number, self.settings.max_message_bytes)
        return selected, msg, size, flags

    def read_message(
        self,
        message_id: str,
        folder: str = "INBOX",
        protocol: str = "auto",
        include_html: bool = False,
    ) -> dict[str, Any]:
        selected, msg, size, flags = self._read(message_id, folder, protocol)
        return {
            "id": message_id,
            "protocol": selected,
            "folder": folder,
            "size": size,
            "flags": flags,
            **full_message(msg, self.settings.max_body_chars, include_html),
        }

    def _attachment_path(self, value: str, must_exist: bool = True) -> Path:
        root = self.settings.attachment_dir.resolve()
        path = Path(value).expanduser()
        path = (path if path.is_absolute() else root / path).resolve()
        if path == root or not path.is_relative_to(root):
            raise MailError("Attachment paths must stay inside EMAIL_ATTACHMENT_DIR.")
        if must_exist and not path.is_file():
            raise MailError("Attachment file does not exist in EMAIL_ATTACHMENT_DIR.")
        return path

    def download_attachment(
        self, message_id: str, attachment_id: str, folder: str = "INBOX", protocol: str = "auto"
    ) -> dict[str, Any]:
        selected, msg, _, _ = self._read(message_id, folder, protocol)
        parts = attachment_parts(msg)
        if not re.fullmatch(r"[1-9][0-9]*", attachment_id) or not 1 <= int(attachment_id) <= len(
            parts
        ):
            raise MailError("Use an attachment_id returned by email_read_message.")
        part = parts[int(attachment_id) - 1]
        content = attachment_bytes(part)
        if len(content) > self.settings.max_attachment_bytes:
            raise MailError("Attachment exceeds EMAIL_MAX_ATTACHMENT_BYTES.")
        # The server's filename is never interpreted as a path, including on Windows.
        filename = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", part.get_filename() or "attachment")
        filename = (
            filename.encode("utf-8")[:120].decode("utf-8", "ignore").strip(" .") or "attachment"
        )
        path = self._attachment_path(f"{uuid4().hex}-{filename}", must_exist=False)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as stream:
            stream.write(content)
        return {
            "protocol": selected,
            "path": str(path),
            "filename": filename,
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }

    def send_message(
        self,
        to: list[str],
        subject: str,
        body: str,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        html_body: str = "",
        attachments: list[str] | None = None,
        in_reply_to: str = "",
        references: str = "",
    ) -> dict[str, Any]:
        self.settings.require_write()
        self.settings.endpoint("smtp")
        cc, bcc = cc or [], bcc or []
        envelope = list(
            dict.fromkeys(addresses(to, "To") + addresses(cc, "Cc") + addresses(bcc, "Bcc"))
        )
        if not envelope or len(envelope) > 100:
            raise MailError("Specify 1..100 total recipients.")
        sender = addresses([self.settings.from_address], "EMAIL_FROM")[0]
        msg = EmailMessage(policy=policy.SMTP)
        msg["From"] = clean_header(self.settings.from_address, "EMAIL_FROM")
        if to:
            msg["To"] = ", ".join(to)
        if cc:
            msg["Cc"] = ", ".join(cc)
        # Bcc is envelope-only and must never enter MIME headers.
        msg["Subject"] = clean_header(subject, "Subject")
        msg["Date"] = format_datetime(datetime.now(UTC))
        msg["Message-ID"] = make_msgid()
        if in_reply_to:
            msg["In-Reply-To"] = clean_header(in_reply_to, "In-Reply-To")
        if references:
            msg["References"] = clean_header(references, "References")
        msg.set_content(body)
        if html_body:
            msg.add_alternative(html_body, subtype="html")
        if len(attachments or []) > 20:
            raise MailError("At most 20 attachments are allowed per message.")
        total = len(body.encode("utf-8")) + len(html_body.encode("utf-8"))
        for value in attachments or []:
            path = self._attachment_path(value)
            size = path.stat().st_size
            if size > self.settings.max_attachment_bytes:
                raise MailError("Attachment exceeds EMAIL_MAX_ATTACHMENT_BYTES.")
            total += size
            if total > self.settings.max_message_bytes:
                raise MailError("Outgoing message exceeds EMAIL_MAX_MESSAGE_BYTES.")
            with path.open("rb") as stream:
                content = stream.read(self.settings.max_attachment_bytes + 1)
            if len(content) > self.settings.max_attachment_bytes:
                raise MailError("Attachment exceeds EMAIL_MAX_ATTACHMENT_BYTES.")
            maintype, subtype = (
                mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            ).split("/", 1)
            msg.add_attachment(content, maintype=maintype, subtype=subtype, filename=path.name)
        if len(msg.as_bytes()) > self.settings.max_message_bytes:
            raise MailError("Encoded outgoing message exceeds EMAIL_MAX_MESSAGE_BYTES.")
        with smtp_connection(self.settings) as client:
            try:
                refused = client.send_message(msg, from_addr=sender, to_addrs=envelope)
            except (OSError, smtplib.SMTPException) as exc:
                raise MailError(
                    safe_error(exc) + " Acceptance may be uncertain; verify before retrying."
                ) from None
        return {
            "status": "partially_accepted" if refused else "accepted",
            "message_id": str(msg["Message-ID"]),
            "accepted_recipients": [addr for addr in envelope if addr not in refused],
            "refused_recipients": [
                {"address": addr, "code": info[0]} for addr, info in refused.items()
            ],
            "note": "SMTP acceptance is not a delivery receipt. Do not resend accepted recipients.",
        }

    def set_flags(
        self,
        message_id: str,
        folder: str = "INBOX",
        seen: bool | None = None,
        flagged: bool | None = None,
    ) -> dict[str, Any]:
        self.settings.require_write()
        if seen is None and flagged is None:
            raise MailError("Specify seen or flagged.")
        with imap_connection(self.settings) as client:
            uid = _validate_ref(_select(client, folder, False), message_id)
            # Ensure a vanished UID is not reported as successfully modified.
            _imap_fetch(client, uid, self.settings.max_message_bytes, True)
            for flag, value in (("\\Seen", seen), ("\\Flagged", flagged)):
                if value is not None:
                    imap_ok(
                        client.uid(
                            "STORE", uid, "+FLAGS.SILENT" if value else "-FLAGS.SILENT", f"({flag})"
                        ),
                        "STORE",
                    )
        return {
            "id": message_id,
            "folder": folder,
            "seen": seen,
            "flagged": flagged,
            "updated": True,
        }

    def move_message(
        self, message_id: str, destination: str, folder: str = "INBOX"
    ) -> dict[str, Any]:
        self.settings.require_write()
        if folder == destination:
            raise MailError("Source and destination folders must differ.")
        target = encode_folder(destination)
        with imap_connection(self.settings) as client:
            uid = _validate_ref(_select(client, folder, False), message_id)
            capabilities = {item.upper() for item in client.capabilities}
            if "MOVE" not in capabilities:
                raise MailError("This IMAP server does not support MOVE; no message was changed.")
            _imap_fetch(client, uid, self.settings.max_message_bytes, True)
            imap_ok(client.uid("MOVE", uid, target), "MOVE")
        return {
            "moved": True,
            "source": folder,
            "destination": destination,
            "note": "List the destination folder to obtain the new message id.",
        }

    def delete_message(
        self,
        message_id: str,
        folder: str = "INBOX",
        protocol: str = "auto",
        permanent: bool = False,
    ) -> dict[str, Any]:
        self.settings.require_write()
        selected = self._protocol(protocol, folder)
        if selected == "imap":
            with imap_connection(self.settings) as client:
                uid = _validate_ref(_select(client, folder, False), message_id)
                if permanent and "UIDPLUS" not in {item.upper() for item in client.capabilities}:
                    raise MailError("Permanent deletion requires UIDPLUS; no message was changed.")
                _imap_fetch(client, uid, self.settings.max_message_bytes, True)
                imap_ok(client.uid("STORE", uid, "+FLAGS.SILENT", "(\\Deleted)"), "STORE")
                if permanent:
                    imap_ok(client.uid("EXPUNGE", uid), "UID EXPUNGE")
        else:
            if not permanent:
                raise MailError(
                    "POP3 only supports permanent deletion; set permanent=true explicitly."
                )
            with pop3_connection(self.settings) as client:
                number = next((n for n, uid in _uid_map(client) if uid == message_id), None)
                if number is None:
                    raise MailError("POP3 message not found; list messages again.")
                client.dele(number)
        return {
            "id": message_id,
            "protocol": selected,
            "status": "deleted" if permanent else "marked_deleted",
        }
