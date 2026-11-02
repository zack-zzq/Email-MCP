# Email-MCP-zack

纯本地运行的通用邮件 MCP 服务，使用 Python 3.11+ 和官方 MCP Python SDK，通过 **stdio** 接入 MCP 客户端。支持 **IMAP / POP3 收信、SMTP 发信**，可以单独启用任一协议。

服务不启动 HTTP 端口，不依赖邮件平台的云 API。邮箱账号、密码、授权码和 OAuth2 access token 均通过启动进程的环境变量传入；代码不读取 `.env`，不保存凭据。邮件操作只连接你配置的邮件服务器；安装依赖时需要访问 Python 软件包源。MCP 客户端及其所使用的模型如何处理邮件内容，由客户端自身配置决定。

## 直接从 PyPI 运行（无需克隆项目）

安装 [uv](https://docs.astral.sh/uv/getting-started/installation/) 后，MCP 客户端可以通过下面的命令自动从 PyPI 获取软件包并启动服务：

```text
uvx --from Email-MCP-zack email-mcp
```

PyPI 分发包名为 **`Email-MCP-zack`**，启动命令为 **`email-mcp`**，Python 模块名为 **`email_mcp`**。`uvx` 会在独立环境中安装并缓存软件包，不需要下载源码或预先创建本项目的虚拟环境。运行时仍通过 MCP 配置的 `env` 传入邮箱信息。

也可以先安装，再把 MCP 客户端的 command 配为 `email-mcp`、args 配为 `[]`：

```powershell
python -m pip install Email-MCP-zack
```

如果客户端找不到 `uvx` 或 `email-mcp`，把 command 改成该程序的完整路径。服务等待 MCP 客户端通过标准输入输出通信，启动后没有交互式界面。

## MCP 客户端配置

把下面的条目加入支持 `mcpServers` 配置格式的客户端；替换账号、授权码和邮件服务器。其他配置格式的客户端使用相同的 command、args 和 env 值。

```json
{
  "mcpServers": {
    "local-email": {
      "command": "uvx",
      "args": ["--from", "Email-MCP-zack", "email-mcp"],
      "env": {
        "EMAIL_USERNAME": "you@example.com",
        "EMAIL_PASSWORD": "replace-with-password-or-app-password",
        "EMAIL_IMAP_HOST": "imap.example.com",
        "EMAIL_IMAP_PORT": "993",
        "EMAIL_IMAP_TLS": "ssl",
        "EMAIL_SMTP_HOST": "smtp.example.com",
        "EMAIL_SMTP_PORT": "587",
        "EMAIL_SMTP_TLS": "starttls"
      }
    }
  }
}
```

完整三协议 PyPI 示例见 [examples/mcp-config.json](examples/mcp-config.json)。如需固定版本，将 args 中的包名改为 `Email-MCP-zack==0.1.0`；如需更新 uvx 缓存的版本，可在命令中加 `--upgrade`。附件默认保存到用户主目录下 `.email-mcp/attachments`，可通过 `EMAIL_ATTACHMENT_DIR` 指定其他路径。

只使用 POP3 收信时，去掉 IMAP 配置，加入：

```json
{
  "EMAIL_POP3_HOST": "pop.example.com",
  "EMAIL_POP3_PORT": "995",
  "EMAIL_POP3_TLS": "ssl",
  "EMAIL_READ_PROTOCOL": "pop3"
}
```

客户端从同一个进程环境继承已设置的变量时，也可省略 env 中相应的敏感值。配置文件中填入的密码仍应按凭据保管。支持多个邮箱的方法是配置多个 MCP 服务条目，每个条目传入不同的环境变量；每个进程对应一个邮箱。

## 环境变量

| 变量 | 说明 / 默认值 |
| --- | --- |
| `EMAIL_USERNAME` | 共用登录账号，通常为完整邮箱地址 |
| `EMAIL_PASSWORD` | 共用密码或客户端授权码；密码认证必填 |
| `EMAIL_AUTH_TYPE` | `password`（默认）、`oauth2`；SMTP 可用 `none` 连接无需认证的中继 |
| `EMAIL_ACCESS_TOKEN` | OAuth2 模式的 access token，使用 XOAUTH2；不自动刷新 |
| `EMAIL_IMAP_HOST` / `EMAIL_POP3_HOST` / `EMAIL_SMTP_HOST` | 按需启用协议，至少配置一个；只填写主机名或 IP |
| `EMAIL_<协议>_PORT` | IMAP：SSL 993、其他 143；POP3：SSL 995、其他 110；SMTP：SSL 465、STARTTLS 587、plain 25 |
| `EMAIL_<协议>_TLS` | `ssl`、`starttls`、`plain`；IMAP/POP3 默认 ssl，SMTP 默认 starttls |
| `EMAIL_<协议>_USERNAME` / `EMAIL_<协议>_PASSWORD` | 覆盖共用账号或密码，协议为 `IMAP` / `POP3` / `SMTP` |
| `EMAIL_<协议>_AUTH_TYPE` / `EMAIL_<协议>_ACCESS_TOKEN` | 覆盖对应协议的认证方式或 token |
| `EMAIL_FROM` | 发信人地址，可带显示名；默认 SMTP 登录账号 |
| `EMAIL_READ_PROTOCOL` | `imap` / `pop3`；配置 IMAP 时优先 IMAP，否则 POP3 |
| `EMAIL_READ_ONLY` | `true` 禁止发送、修改标记、移动和删除；默认 false；仍可下载附件 |
| `EMAIL_ATTACHMENT_DIR` | 附件下载及发信附件的唯一允许目录；默认用户主目录下 `.email-mcp/attachments` |
| `EMAIL_TIMEOUT` | 每次网络阻塞操作的超时秒数，默认 30；不是整个搜索的总时限 |
| `EMAIL_TLS_CA_FILE` | 可选 PEM CA 证书文件，供私有邮件服务器使用 |
| `EMAIL_MAX_MESSAGE_BYTES` | 默认 26214400（25 MiB），限制整封邮件及发信编码后的大小 |
| `EMAIL_MAX_ATTACHMENT_BYTES` | 默认 20971520（20 MiB），限制单附件大小 |
| `EMAIL_MAX_BODY_CHARS` | 默认 50000，返回正文的字符上限 |
| `EMAIL_SEARCH_SCAN_LIMIT` | 默认 200，最多 2000；每次搜索扫描最新的这些邮件 |

TLS 默认校验证书和主机名。`starttls` 升级失败会直接报错，不降级传输密码。`plain` 需要明确配置，适用于本机邮件桥或可信内网中的明文邮件服务。OAuth2 只接受外部提供的现成 access token；过期后需更新进程环境并重启。服务不会自行申请授权或刷新 token。

## 工具

| 工具 | 功能 |
| --- | --- |
| `email_account_info` | 查看启用的协议、主机、端口和限制，不返回登录账号或凭据 |
| `email_check_connection` | 检查连接与认证，不发送测试邮件 |
| `email_list_folders` | 列出 IMAP 文件夹；POP3 只有 INBOX |
| `email_list_messages` | 分页列出邮件头，按服务器接收顺序从新到旧 |
| `email_search_messages` | 按发件人、收件人、主题、正文、日期、未读状态搜索 |
| `email_read_message` | 解码正文和附件信息；默认不返回 HTML |
| `email_download_attachment` | 下载指定附件，返回本地路径、大小及 SHA-256 |
| `email_send_message` | 发送纯文本 / HTML 邮件，支持 To/Cc/Bcc、本地附件和回复头 |
| `email_set_flags` | IMAP 设置已读 / 未读、星标 |
| `email_move_message` | IMAP 移动邮件到已有文件夹，要求服务器支持 MOVE |
| `email_delete_message` | IMAP 标记删除或指定 UID 永久删除；POP3 显式永久删除 |

收信工具的 `protocol` 可选 `auto` / `imap` / `pop3`。`auto` 使用 `EMAIL_READ_PROTOCOL`；认证失败时不会自动切换协议。修改标记和移动工具固定使用 IMAP。

### 使用细节

- **编号**：工具参数 `message_id` 使用列表返回的 `id`，不是邮件头中的 `Message-ID`。IMAP id 为 `UIDVALIDITY:UID`，POP3 id 为 UIDL。必须与同一次列表返回的协议、文件夹一起使用；切换协议或移动文件夹后重新列出邮件。
- **读取**：IMAP 使用只读 SELECT 与 `BODY.PEEK`，不会自动设为已读。POP3 优先用 TOP 读取邮件头，服务器不支持时回退 RETR；POP3 服务端的下载后处理行为由服务端决定。
- **搜索**：所有条件取交集，中文匹配在本地解码后执行，不向服务端拼接原始查询语句。`recipient` 匹配 To/Cc，`text` 只匹配正文；`since` 含当天，`before` 不含当天，使用邮件 Date 头中的日期。`unread` 仅适用于 IMAP。返回 `scanned`、`scan_truncated`、`skipped`，明确扫描范围；`offset` 在本次扫描得到的匹配结果中分页。需要查找更早的邮件时，可调整扫描上限或使用列表分页定位。没有完整邮箱索引。
- **附件**：先调用读取工具取得 `attachment_id`，再下载。发信附件必须事先放在 `EMAIL_ATTACHMENT_DIR` 内；支持相对该目录的路径或目录内绝对路径，拒绝路径穿越及指向目录外的符号链接。下载使用唯一文件名，不覆盖旧文件，不自动打开文件。
- **发信**：SMTP 接受邮件后返回 `accepted`，部分收件人被拒绝时返回 `partially_accepted` 及逐项结果。不要重复发送给已接受的收件人。SMTP 断线时接受状态可能不确定，应先核实再重试。发送不自动追加 IMAP“已发送”副本，是否保存取决于邮件服务器。
- **删除**：IMAP 默认只加 `\\Deleted` 标记，其他客户端之后可能清除该邮件；如要放入回收站，请调用移动工具。`permanent=true` 仅在服务器支持 UIDPLUS 时执行指定 UID 的 EXPUNGE，避免清除其他邮件。POP3 不提供软删除，必须明确设置 `permanent=true`，成功 QUIT 后提交。任何协议的删除均不会自动重试。
- **本地存储**：服务不建立邮件数据库。只有请求下载的附件会落地；邮件服务器返回的原始错误不会直接暴露给模型，日志不主动记录凭据或邮件内容。邮件正文视为外部不可信数据。

## 验证与开发

从源码开发时再克隆仓库，并在项目目录安装开发依赖：

```powershell
git clone https://github.com/zack-zzq/Email-MCP.git
cd Email-MCP
uv --cache-dir .uv-cache sync --locked --group dev
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\ruff.exe check src tests
```

源码运行的 MCP 配置见 [examples/mcp-config.source.json](examples/mcp-config.source.json)，将其中的解释器和附件目录替换成自己的路径。Linux/macOS 对应解释器为 `.venv/bin/python`。

测试使用本地模拟邮箱及回环地址，不需要真实账号，不向外部收件人发邮件。覆盖三种协议、MCP stdio 握手与调用、中文 MIME、附件边界、TLS 配置、UID 变化、删除范围和凭据错误脱敏。

连接真实邮箱后，先调用 `email_check_connection`，再列出邮件。不同服务商可能需要在邮箱设置中开启 IMAP/POP3/SMTP，并使用客户端授权码；仅提供 OAuth2 的邮箱需要配置 token。实际服务商的登录策略、限制和扩展支持，需要在你的邮箱环境中验证。

## 自动发布到 PyPI

[publish.yml](.github/workflows/publish.yml) 使用 PyPI Trusted Publishing，通过 GitHub OIDC 发布，不需要设置 `PYPI_API_TOKEN`。对应的 PyPI 发布者配置为：

| 项目 | 值 |
| --- | --- |
| PyPI Project Name | `Email-MCP-zack` |
| Owner | `zack-zzq` |
| Repository | `Email-MCP` |
| Workflow name | `publish.yml` |
| Environment | `pypi` |

发布步骤：

1. 同步修改 `pyproject.toml` 的 `version` 和 `src/email_mcp/__init__.py` 的 `__version__`，每次使用新的版本号，更新并提交 `uv.lock`。
2. 将代码合入 `main`，等待测试和打包验证通过。
3. 在 GitHub 发布一个 Release，标签必须为 `v` 加版本号，例如 `v0.1.0`。Release 发布后，Action 自动测试、构建 wheel 和源码包、检查 PyPI 元数据、验证安装后的 MCP 通信，再将同一批安装包上传 PyPI。

普通 push 和 pull request 只执行验证。也可在 Actions → Publish to PyPI → Run workflow 中选择 `main`，将 `publish` 勾选为 true，手动发布当前版本；默认 false 只验证。PyPI 已存在的版本不会被覆盖，同一版本再次发布会失败。仓库中的 `pypi` Environment 如设置了必需审批人，发布会等待该环境的审批。

PyPI 页面：[Email-MCP-zack](https://pypi.org/project/Email-MCP-zack/)。如果发布者设置里仍填写了旧名 `Email-MCP`，需要将它改为 `Email-MCP-zack`，其他配置保持与上表一致。

## 参考

- [官方 MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)
- [Python imaplib](https://docs.python.org/3.13/library/imaplib.html)
- [Python poplib](https://docs.python.org/3.13/library/poplib.html)
- [Python smtplib](https://docs.python.org/3.13/library/smtplib.html)
