import json
import os
import subprocess
import sys
from pathlib import Path

from mcp import Client
from mcp.client.stdio import StdioServerParameters

from email_mcp.server import create_server

from .fake_servers import mail_server


async def test_mcp_discovery_validation_and_safe_errors(configured):
    async with Client(create_server(configured())) as client:
        discovery = await client.list_tools()
        assert len(discovery.tools) == 11
        tools = {tool.name: tool for tool in discovery.tools}
        assert tools["email_read_message"].annotations.read_only_hint
        assert tools["email_delete_message"].annotations.destructive_hint
        assert "PASSWORD" not in json.dumps([tool.input_schema for tool in discovery.tools])
        result = await client.call_tool("email_account_info", {})
        assert not result.is_error and result.structured_content["transport"] == "stdio"
        invalid = await client.call_tool("email_list_messages", {"limit": 101})
        assert invalid.is_error
        error = await client.call_tool(
            "email_read_message", {"message_id": "bad", "protocol": "pop3"}
        )
        assert error.is_error
        assert "fixture-password" not in str(error)


async def test_real_stdio_subprocess_lists_reads_and_sends(configured):
    with mail_server("imap") as (imap_port, _), mail_server("smtp") as (smtp_port, smtp_state):
        env = {key: value for key, value in os.environ.items() if not key.startswith("EMAIL_")}
        env.update(
            {
                "EMAIL_USERNAME": "user@example.com",
                "EMAIL_PASSWORD": "fixture-password",
                "EMAIL_IMAP_HOST": "127.0.0.1",
                "EMAIL_IMAP_PORT": str(imap_port),
                "EMAIL_IMAP_TLS": "plain",
                "EMAIL_SMTP_HOST": "127.0.0.1",
                "EMAIL_SMTP_PORT": str(smtp_port),
                "EMAIL_SMTP_TLS": "plain",
                "EMAIL_TIMEOUT": "3",
            }
        )
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "email_mcp"],
            env=env,
            cwd=str(Path(__file__).resolve().parents[1]),
        )
        async with Client(params) as client:
            tools = await client.list_tools()
            assert len(tools.tools) == 11
            health = await client.call_tool("email_check_connection", {})
            assert health.structured_content["ok"]
            listed = await client.call_tool("email_list_messages", {"limit": 1})
            message_id = listed.structured_content["messages"][0]["id"]
            read = await client.call_tool("email_read_message", {"message_id": message_id})
            assert "你好" in read.structured_content["text"]
            sent = await client.call_tool(
                "email_send_message",
                {
                    "to": ["ok@example.com"],
                    "subject": "stdio integration",
                    "body": "local only",
                },
            )
            assert sent.structured_content["status"] == "accepted"
        assert len(smtp_state.sent) == 1


def test_startup_config_error_only_uses_stderr():
    env = {key: value for key, value in os.environ.items() if not key.startswith("EMAIL_")}
    result = subprocess.run(
        [sys.executable, "-m", "email_mcp"], env=env, capture_output=True, timeout=15, text=True
    )
    assert result.returncode == 2 and result.stdout == ""
    assert "EMAIL_IMAP_HOST" in result.stderr and "Traceback" not in result.stderr
