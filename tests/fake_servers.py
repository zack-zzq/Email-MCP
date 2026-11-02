"""Small real TCP protocol fixtures. They bind only to 127.0.0.1."""

import re
import shlex
import socketserver
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from email import policy
from email.message import EmailMessage


def example_message(subject: str = "中文主题：项目进展", attachment: bool = True) -> bytes:
    msg = EmailMessage(policy=policy.SMTP)
    msg["From"] = "张三 <sender@example.com>"
    msg["To"] = "user@example.com"
    msg["Cc"] = "copy@example.com"
    msg["Subject"] = subject
    msg["Message-ID"] = "<fixture@example.com>"
    msg["Date"] = "Sun, 01 Nov 2026 10:00:00 +0800"
    msg.set_content("你好，项目进展正常。\n第一行\n.以点开头\n")
    msg.add_alternative("<p>你好，项目进展正常。</p><script>hidden</script>", subtype="html")
    if attachment:
        msg.add_attachment(
            b"\x00\x01attachment-data\xff",
            maintype="application",
            subtype="octet-stream",
            filename="../../中文报告.bin",
        )
    return msg.as_bytes()


@dataclass
class State:
    messages: dict[int, bytes] = field(
        default_factory=lambda: {
            101: example_message("Older email"),
            102: example_message(),
        }
    )
    flags: dict[int, set[str]] = field(default_factory=dict)
    commands: list[str] = field(default_factory=list)
    sent: list[dict] = field(default_factory=list)
    capabilities: str = "IMAP4rev1 UIDPLUS MOVE"
    preauth_capabilities: str | None = None
    validity: int = 42
    top_supported: bool = True
    fail_pop_quit: bool = False


class Handler(socketserver.StreamRequestHandler):
    def line(self, value: str | bytes) -> None:
        self.wfile.write((value.encode("ascii") if isinstance(value, str) else value) + b"\r\n")

    def multiline(self, raw: bytes) -> None:
        for row in raw.split(b"\r\n"):
            self.line(b"." + row if row.startswith(b".") else row)
        self.line(".")

    def handle(self) -> None:
        state, protocol = self.server.state, self.server.protocol
        if protocol == "imap":
            self.line("* OK local test IMAP")
            self.imap(state)
        elif protocol == "pop3":
            self.line("+OK local test POP3")
            self.pop3(state)
        else:
            self.line("220 local test SMTP")
            self.smtp(state)

    def imap(self, state: State) -> None:
        authenticated = False
        while raw := self.rfile.readline():
            line = raw.decode("ascii").strip()
            state.commands.append(line)
            tag, command, *args = shlex.split(line)
            command = command.upper()
            if command == "CAPABILITY":
                capabilities = (
                    state.capabilities
                    if authenticated
                    else (state.preauth_capabilities or state.capabilities)
                )
                self.line("* CAPABILITY " + capabilities)
            elif command == "LOGIN":
                authenticated = True
            elif command == "AUTHENTICATE":
                self.line("+ ")
                state.commands.append(self.rfile.readline().decode("ascii").strip())
                authenticated = True
            elif command in {"SELECT", "EXAMINE"}:
                self.line(f"* {len(state.messages)} EXISTS")
                self.line(f"* OK [UIDVALIDITY {state.validity}] UIDs valid")
            elif command == "LIST":
                self.line('* LIST (\\HasNoChildren) "/" "INBOX"')
                self.line('* LIST () "/" "&XfJP9mWHTvZZOQ-"')
            elif command == "LOGOUT":
                self.line("* BYE logging out")
                self.line(f"{tag} OK LOGOUT done")
                return
            elif command == "UID":
                subcommand = args[0].upper()
                if subcommand == "SEARCH":
                    self.line("* SEARCH " + " ".join(str(uid) for uid in sorted(state.messages)))
                elif subcommand == "FETCH":
                    uid = int(args[1])
                    if uid in state.messages:
                        message = state.messages[uid]
                        flags = " ".join(sorted(state.flags.get(uid, set())))
                        if "BODY.PEEK" in line:
                            if "HEADER" in line:
                                message = message.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n"
                            bound = int(re.search(r"<0\.(\d+)>", line).group(1))
                            message = message[:bound]
                            self.line(f"* 1 FETCH (UID {uid} BODY[]<0> {{{len(message)}}}")
                            self.wfile.write(message)
                            self.line(")")
                        else:
                            self.line(
                                f"* 1 FETCH (UID {uid} RFC822.SIZE {len(message)} FLAGS ({flags}))"
                            )
                elif subcommand == "STORE":
                    uid = int(args[1])
                    flag = re.search(r"\((\\[^ )]+)\)", line).group(1)
                    if args[2].startswith("+"):
                        state.flags.setdefault(uid, set()).add(flag)
                    else:
                        state.flags.setdefault(uid, set()).discard(flag)
                elif subcommand in {"MOVE", "EXPUNGE"}:
                    state.messages.pop(int(args[1]), None)
            elif command not in {"LOGIN", "NOOP"}:
                self.line(f"{tag} BAD unsupported")
                continue
            self.line(f"{tag} OK {command} done")

    def pop3(self, state: State) -> None:
        deleted = set()
        ids = sorted(state.messages)
        while raw := self.rfile.readline():
            line = raw.decode("ascii").strip()
            state.commands.append(line)
            command, *args = line.split()
            command = command.upper()
            if command == "UIDL":
                self.line("+OK identifiers")
                for index, uid in enumerate(ids, 1):
                    self.line(f"{index} UID-{uid}")
                self.line(".")
            elif command == "LIST":
                if args:
                    number = int(args[0])
                    self.line(f"+OK {number} {len(state.messages[ids[number - 1]])}")
                else:
                    self.line("+OK sizes")
                    for index, uid in enumerate(ids, 1):
                        self.line(f"{index} {len(state.messages[uid])}")
                    self.line(".")
            elif command in {"RETR", "TOP"}:
                if command == "TOP" and not state.top_supported:
                    self.line("-ERR TOP not supported")
                    continue
                msg = state.messages[ids[int(args[0]) - 1]]
                if command == "TOP":
                    msg = msg.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n"
                self.line("+OK message follows")
                self.multiline(msg)
            elif command == "DELE":
                deleted.add(ids[int(args[0]) - 1])
                self.line("+OK marked")
            elif command == "RSET":
                deleted.clear()
                self.line("+OK reset")
            elif command == "QUIT":
                if state.fail_pop_quit:
                    self.line("-ERR deletion could not be committed")
                else:
                    for uid in deleted:
                        state.messages.pop(uid, None)
                    self.line("+OK bye")
                return
            elif command == "STAT":
                self.line(f"+OK {len(ids)} {sum(len(state.messages[uid]) for uid in ids)}")
            else:
                self.line("+OK")

    def smtp(self, state: State) -> None:
        envelope = {"recipients": []}
        while raw := self.rfile.readline():
            line = raw.decode("utf-8").strip()
            state.commands.append(line)
            command = line.split()[0].upper()
            if command in {"EHLO", "HELO"}:
                self.line("250-localhost")
                self.line("250-AUTH PLAIN LOGIN")
                self.line("250-SMTPUTF8")
                self.line("250 8BITMIME")
            elif command == "AUTH":
                self.line("235 authenticated")
            elif command == "MAIL":
                envelope = {"sender": line, "recipients": []}
                self.line("250 sender accepted")
            elif command == "RCPT":
                if "rejected@example.com" in line:
                    self.line("550 recipient rejected")
                else:
                    envelope["recipients"].append(line)
                    self.line("250 recipient accepted")
            elif command == "DATA":
                self.line("354 send data")
                content = []
                while row := self.rfile.readline():
                    if row == b".\r\n":
                        break
                    content.append(row[1:] if row.startswith(b"..") else row)
                state.sent.append({**envelope, "raw": b"".join(content)})
                self.line("250 message accepted")
            elif command == "QUIT":
                self.line("221 bye")
                return
            else:
                self.line("250 ok")


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


@contextmanager
def mail_server(protocol: str, state: State | None = None):
    state = state or State()
    with Server(("127.0.0.1", 0), Handler) as server:
        server.state, server.protocol = state, protocol
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        thread.start()
        try:
            yield server.server_address[1], state
        finally:
            server.shutdown()
            thread.join(timeout=2)
