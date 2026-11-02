"""Client-safe errors. Never expose raw server replies or authentication payloads."""

import imaplib
import poplib
import smtplib
import ssl


class MailError(Exception):
    """A safe, actionable error suitable for an MCP tool result."""


class ConfigError(MailError):
    pass


def safe_error(exc: Exception) -> str:
    if isinstance(exc, MailError):
        return str(exc)
    if isinstance(exc, ssl.SSLCertVerificationError):
        return "TLS certificate verification failed; check the server name and EMAIL_TLS_CA_FILE."
    if isinstance(exc, ssl.SSLError):
        return "TLS negotiation failed; check the protocol, port and TLS mode."
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)) and not isinstance(
        exc, smtplib.SMTPException
    ):
        return "Network or local file operation failed; check connectivity, paths and permissions."
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return "SMTP authentication failed; check credentials, authorization code or access token."
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return "SMTP rejected all recipients; the message was not accepted."
    if isinstance(exc, smtplib.SMTPException):
        return (
            "SMTP operation failed. If sending, acceptance may be uncertain; "
            "check the mailbox before retrying to avoid duplicates."
        )
    if isinstance(exc, imaplib.IMAP4.error):
        return "IMAP operation failed; check authentication, folder and server permissions."
    if isinstance(exc, poplib.error_proto):
        return (
            "POP3 operation failed; check authentication and server capabilities (UIDL/TOP). "
            "If deleting, commit may be uncertain; list messages again before retrying."
        )
    return "Email operation failed; no server response or credentials were included in this error."
