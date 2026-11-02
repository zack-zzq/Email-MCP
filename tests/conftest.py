import pytest

from email_mcp.config import Settings


@pytest.fixture
def configured(tmp_path):
    def create(protocol="imap", port=12345, **overrides):
        env = {
            "EMAIL_USERNAME": "user@example.com",
            "EMAIL_PASSWORD": "fixture-password",
            f"EMAIL_{protocol.upper()}_HOST": "127.0.0.1",
            f"EMAIL_{protocol.upper()}_PORT": str(port),
            f"EMAIL_{protocol.upper()}_TLS": "plain",
            "EMAIL_ATTACHMENT_DIR": str(tmp_path / "attachments"),
            "EMAIL_TIMEOUT": "3",
            **overrides,
        }
        return Settings.from_env(env)

    return create
