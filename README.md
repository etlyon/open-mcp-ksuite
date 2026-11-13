This code was written with the help of generative AI tools and reviewed by ETLYON. It is intended for professional use; test it in your environment before production. Provided as is, without warranty.

# open-mcp-ksuite

Connect any assistant to **your own kSuite**. You bring your server or account and your own key; nothing is set up in advance.

Works with Claude, ChatGPT, Le Chat, eBot and any assistant that can add a connector by address. Your assistant acts as you, with exactly the rights you have.

> "mcp" appears only in this repository's name. People and product screens say **Apps** / **Connector** (D386).

## Install in 5 minutes

**1. Run one Docker container** (TLS in front of it; set the three names below — never put secrets in the image):

```sh
mkdir -p /etc/open-mcp-ksuite /var/lib/open-mcp-ksuite
cat >/etc/open-mcp-ksuite/connector.toml <<'EOF'
public_url = "https://ksuite.example.org"
app = "ksuite"
state_dir = "/var/lib/open-mcp-ksuite"
source_url = "https://github.com/etlyon/open-mcp-ksuite"
maker_label = "open source"
EOF
docker build -t open-mcp-ksuite:0.1.0 .
docker run --name open-mcp-ksuite --restart=unless-stopped \
  -p 127.0.0.1:8130:8000 \
  -v /etc/open-mcp-ksuite/connector.toml:/config/connector.toml:ro \
  -v /var/lib/open-mcp-ksuite:/var/lib/open-mcp-ksuite \
  open-mcp-ksuite:0.1.0
```

Settings (names only — put your own values):
| Setting | Meaning |
|---|---|
| `public_url` | Public https host of **this** connector (no path) |
| `source_url` | Public source of the code you run (AGPL §13) |
| `state_dir` | Where the SQLite file lives (0700) |
| `allowed_mail_domains` / `send_recipient_domains` | Who may connect / who mail may go to — see `config.example.toml`. `allowed_servers` does not apply (no server to pick). |

Put a reverse proxy with TLS in front (`X-Forwarded-For` / `X-Forwarded-Proto`) and pass `--forwarded-allow-ips` if you change the entrypoint.

**2. Connect it in eBot (or any AI app)**

1. In the assistant, add a custom connector / MCP server and paste `https://<your-connector-host>/mcp`.
2. Complete the on-screen window (address + key, or Grant access, or Infomaniak token — this app's steps are below).
3. Allow the assistant when asked. Done.

Same paste works in Claude, ChatGPT, Le Chat and eBot. Menu names per assistant: [docs/assistants.md](docs/assistants.md).

## kSuite (Infomaniak)

Same window; one process with `app = "ksuite"` (see `config.example.toml`). There is **no step B**: kSuite works by account, on Infomaniak's own servers, so there is no server to pick and nothing to change.

**C. Your token.** The page shows *1. Open Infomaniak ↗ 2. Create a token with user_info, workspace:mail and workspace:calendar 3. Paste it below*. "Open Infomaniak" is Infomaniak's own API token page (Manager › API tokens). Choose those three scopes, copy the token (Infomaniak shows it only once) and paste it. The page checks it with three read-only requests (your profile, your mailboxes, your calendars): a refused token, or a token missing a part, is named in one sentence, and nothing is kept.

**D. Done.** Your assistant can list, search and read your mail (attachments as text, or as the file itself), save drafts, mark and star messages, and read and change your calendar. **Sending mail, inviting people to an event and deleting an event always ask you first**, and the question shows what goes out: sender, every recipient, subject, attachments and the start of the text; for an event, its title, time and everyone Infomaniak e-mails. Moving mail asks too. Creating or updating an event never e-mails anyone. Mail goes out only from your own mailbox: the one with your account's address (else the only one, else the one marked primary); other mailboxes the token opens are not used in version 1. Attachments travel only as bytes in the call (at most 10 files, 3 MiB in total).

**Who mail may go to.** Mail, invitations and cancellations go only to the mail domains the operator lists (`send_recipient_domains`, exact match, at most 20), checked on the final message just before Infomaniak is asked to send it. With no list (the default), nothing is sent: the message is kept as a draft you can send yourself from Infomaniak Mail, and nobody is invited. Because Infomaniak may e-mail everyone on an event when one person is added, an invitation must name every person already on the event as well as the new ones.

To stop: remove the connector in your assistant, **then delete the token in Infomaniak** (Manager › API tokens: select it, revoke). No Infomaniak API lets a token delete itself, so the connector cannot do it for you.

For operators: this process calls only `api.infomaniak.com` and `mail.infomaniak.com` (https, 443); Mail uses `mail.infomaniak.com/api`, the API of Infomaniak's own Mail MCP server, which Infomaniak does not document publicly. `allowed_servers` does not apply; `allowed_mail_domains` limits who may connect by the domain of their mailbox (checked when they connect and on every call); `send_recipient_domains` lists the domains mail may go to (empty = drafts only). Infomaniak allows 60 requests a minute; the connector keeps each connection under that, counting every request it makes.

## What it keeps

- Your key (Odoo), app password (Hub) or API token (kSuite, with the id and address of your own mailbox), encrypted with a key that only your assistant's live sign-in can unlock. A copy of the connector's disk or backups opens nothing.
- Your name and email as Odoo, Nextcloud or Infomaniak gives them, to show on the "Allow?" and "connected" pages.
- For a change made with an idempotency key (so a repeat does not write twice): the key, a hash of the arguments and the answer. For kSuite only the ids, yes/no answers and counts of that answer are kept: no address, subject, title or text.
- Nothing else: no chat content.

## Licence

AGPL-3.0-or-later ([LICENSE](LICENSE)). If you run a modified copy for others, offer them its source: set `source_url`; every page links to it. Third-party notices: [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). Not affiliated with Odoo S.A., Nextcloud GmbH or Infomaniak.
