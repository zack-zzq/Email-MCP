"""MIME decoding, safe headers, IMAP mailbox encoding and attachment metadata."""

import base64
import re
from email import policy
from email.message import EmailMessage, Message
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any

from .errors import MailError


def clean_header(value: str, name: str = "Header") -> str:
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise MailError(f"{name} contains a forbidden control character.")
    return value


def addresses(values: list[str], name: str) -> list[str]:
    """One mailbox per item; reject ambiguous/injected envelope addresses."""
    result = []
    if len(values) > 100:
        raise MailError(f"{name} allows at most 100 addresses.")
    for value in values:
        clean_header(value, name)
        parsed = getaddresses([value])
        if len(parsed) != 1 or not parsed[0][1]:
            raise MailError(f"{name} must contain one email address per item.")
        addr = parsed[0][1]
        if not re.fullmatch(r"[^\s<>@,;:]+@[^\s<>@,;:]+", addr):
            raise MailError(f"{name} contains an invalid email address.")
        result.append(addr)
    return result


def encode_folder(name: str) -> str:
    """RFC 3501 modified UTF-7, with explicit IMAP quoting."""
    clean_header(name, "Folder")
    if not name or len(name) > 1000:
        raise MailError("Folder name must contain 1 to 1000 characters.")
    output, pending = [], []

    def flush() -> None:
        if pending:
            data = "".join(pending).encode("utf-16-be")
            output.append("&" + base64.b64encode(data).decode().rstrip("=").replace("/", ",") + "-")
            pending.clear()

    for char in name:
        if " " <= char <= "~":
            flush()
            output.append("&-" if char == "&" else char)
        else:
            pending.append(char)
    flush()
    return '"' + "".join(output).replace("\\", "\\\\").replace('"', '\\"') + '"'


def decode_folder(value: bytes) -> str:
    text = value.decode("ascii", "replace")

    def decode(match: re.Match[str]) -> str:
        encoded = match.group(1)
        if not encoded:
            return "&"
        try:
            encoded = encoded.replace(",", "/")
            return base64.b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-16-be")
        except (ValueError, UnicodeError):
            return match.group(0)

    return re.sub(r"&([^-]*)-", decode, text)


def parse_folder_list(items: list[Any]) -> list[dict[str, Any]]:
    result = []
    pattern = rb'^\(([^)]*)\)\s+(NIL|"(?:[^"\\]|\\.)*")\s+(.+)$'
    for item in items:
        # imaplib returns a tuple when the mailbox name is a literal.
        prefix, literal = item if isinstance(item, tuple) else (item, None)
        if not isinstance(prefix, bytes):
            continue
        match = re.match(pattern, prefix)
        if not match:
            continue
        flags, delimiter, mailbox = match.groups()
        if literal is not None:
            mailbox = literal
        elif mailbox.startswith(b'"') and mailbox.endswith(b'"'):
            mailbox = re.sub(rb"\\(.)", rb"\1", mailbox[1:-1])
        result.append(
            {
                "name": decode_folder(mailbox),
                "delimiter": None
                if delimiter == b"NIL"
                else delimiter[1:-1].decode("ascii", "replace"),
                "flags": flags.decode("ascii", "replace").split(),
            }
        )
    return result


class _HTMLText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self.hidden += 1
        elif tag in {"p", "div", "br", "li", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def html_text(html: str) -> str:
    parser = _HTMLText()
    parser.feed(html)
    return "".join(parser.parts).strip()


def parse(raw: bytes) -> EmailMessage:
    return BytesParser(policy=policy.default).parsebytes(raw)


def decoded_text(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        return str(part.get_payload()) if not part.is_multipart() else ""
    try:
        return payload.decode(part.get_content_charset() or "utf-8", "replace")
    except LookupError:
        return payload.decode("utf-8", "replace")


def attachment_parts(message: Message) -> list[Message]:
    """Treat attached messages as one attachment; don't leak their bodies into the parent."""
    result = []

    def visit(part: Message) -> None:
        if (
            part.get_filename()
            or part.get_content_disposition() == "attachment"
            or part.get_content_maintype() != "multipart"
            and part.get_content_type() not in {"text/plain", "text/html"}
        ):
            result.append(part)
        elif part.is_multipart():
            for child in part.get_payload():
                visit(child)

    visit(message)
    return result


def attachment_bytes(part: Message) -> bytes:
    payload = part.get_payload(decode=True)
    if payload is not None:
        return payload
    if part.is_multipart():
        return b"\r\n".join(child.as_bytes(policy=policy.SMTP) for child in part.get_payload())
    return str(part.get_payload()).encode("utf-8")


def bodies(message: Message) -> tuple[str, str]:
    plain, html = [], []
    excluded = {id(p) for p in attachment_parts(message)}

    def visit(part: Message) -> None:
        if id(part) in excluded:
            return
        if part.is_multipart():
            for child in part.get_payload():
                visit(child)
        elif part.get_content_type() == "text/plain":
            plain.append(decoded_text(part))
        elif part.get_content_type() == "text/html":
            html.append(decoded_text(part))

    visit(message)
    return "\n".join(plain), "\n".join(html)


def summary(message: Message) -> dict[str, Any]:
    raw_date = str(message.get("Date", ""))
    try:
        date = parsedate_to_datetime(raw_date).isoformat()
    except (TypeError, ValueError, OverflowError):
        date = None
    return {
        "message_id": str(message.get("Message-ID", "")),
        "subject": str(message.get("Subject", "")),
        "from": str(message.get("From", "")),
        "to": str(message.get("To", "")),
        "cc": str(message.get("Cc", "")),
        "date": date,
        "date_header": raw_date,
    }


def full_message(message: Message, max_chars: int, include_html: bool) -> dict[str, Any]:
    plain, html = bodies(message)
    text = plain or html_text(html)
    result = summary(message)
    result.update(
        {
            "text": text[:max_chars],
            "text_truncated": len(text) > max_chars,
            "in_reply_to": str(message.get("In-Reply-To", "")),
            "references": str(message.get("References", "")),
            "attachments": [
                {
                    "attachment_id": str(index),
                    "filename": part.get_filename() or f"attachment-{index}",
                    "content_type": part.get_content_type(),
                    "size": len(attachment_bytes(part)),
                    "content_id": str(part.get("Content-ID", "")),
                }
                for index, part in enumerate(attachment_parts(message), 1)
            ],
        }
    )
    if include_html:
        result.update({"html": html[:max_chars], "html_truncated": len(html) > max_chars})
    return result
