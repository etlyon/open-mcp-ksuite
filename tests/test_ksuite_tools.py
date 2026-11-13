"""The kSuite tools on a fake Infomaniak: every tool, as the calling connection only.

Proven here: only Infomaniak's two kSuite hosts are reached; no tool has a path argument and a
path-like attachment is refused before any request; mail goes out only from the person's own pinned
mailbox; writes are keyed per connection; two connections never see each other's token; each
connection stays under 60 requests a minute; send, invite and delete always ask first.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sqlite3
import tempfile
import unittest
import zipfile
from io import BytesIO

import httpx
from fastmcp import Client

from open_connectors import net
from open_connectors.apps.ksuite import api, tools
from open_connectors.oauth.server import CURRENT
from open_connectors.store import Connection
from tests.fake_infomaniak import ALICE, ALICE_BOX, BOB, BOB_BOX, TEAM_BOX, FakeInfomaniak

FAKE = FakeInfomaniak()
ALICE_SECRET = {"api_token": ALICE, "mailbox_uuid": ALICE_BOX, "mailbox_email": "alice@example.org",
                "mailbox_name": "alice", "hosting_id": 1001}
BOB_SECRET = {"api_token": BOB, "mailbox_uuid": BOB_BOX, "mailbox_email": "bob@example.net",
              "mailbox_name": "bob", "hosting_id": 2002}


def connection(conn_id: str = "c-ksuite-alice", secret: dict | None = None, user: str = "4242",
               name: str = "Alice Example") -> Connection:
    return Connection(conn_id=conn_id, app="ksuite", server="https://api.infomaniak.com", remote_user=user,
                      client_id="t", resource="r", display={"name": name, "email": "", "label": name},
                      secret=dict(secret or ALICE_SECRET), scope="read write send")


def docx_bytes(text: str) -> bytes:
    from docx import Document
    document = Document()
    document.add_paragraph(text)
    out = BytesIO()
    document.save(out)
    return out.getvalue()


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class KsuiteTools(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["OPEN_CONNECTORS_JOURNAL"] = f"{cls.tmp.name}/journal.sqlite"

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        FAKE.reset()
        self.saved = (api.TRANSPORT, api.THROTTLE, tools.RECIPIENT_DOMAINS)
        api.TRANSPORT = FAKE.transport
        api.THROTTLE = api.Throttle()
        tools.RECIPIENT_DOMAINS = frozenset({"example.com"})     # the operator lets mail go to example.com
        self.token = CURRENT.set(connection())

    def tearDown(self):
        CURRENT.reset(self.token)
        api.TRANSPORT, api.THROTTLE, tools.RECIPIENT_DOMAINS = self.saved

    def as_connection(self, conn: Connection | None) -> None:
        CURRENT.reset(self.token)
        self.token = CURRENT.set(conn)

    async def call(self, name: str, arguments: dict | None = None, *, confirm: bool = False, **kw) -> dict | str:
        async def accept(message, response_type, params, ctx):
            return {"confirm": True}

        handler = accept if confirm else None
        async with Client(tools.mcp, elicitation_handler=handler) as client:
            result = await client.call_tool(name, arguments or {}, raise_on_error=False, **kw)
        text = result.content[0].text
        try:
            return json.loads(text)
        except ValueError:
            return text

    async def confirmed(self, name: str, arguments: dict) -> dict | str:
        """An ask tool runs only after the person's confirmation form, never a token the model can copy."""
        return await self.call(name, arguments, confirm=True)

    async def question(self, name: str, arguments: dict) -> str:
        """The words shown to the person. Declining runs nothing."""
        seen: list[str] = []

        async def decline(message, response_type, params, ctx):
            seen.append(str(message))
            return {"confirm": False}

        async with Client(tools.mcp, elicitation_handler=decline) as client:
            result = await client.call_tool(name, arguments, raise_on_error=False)
        self.assertEqual(len(seen), 1, getattr(result, "content", result))
        self.assertIn("nothing ran", result.content[0].text)
        return seen[0]

    def message(self, **extra) -> dict:
        return {"params": {"to": ["Carol Example <carol@example.com>"], "subject": "Plan", "body": "Line 1\nLine <2>",
                           **extra}}

    # ------------------------------------------------------------------ reads
    async def test_status_and_folders(self):
        status = json.loads(await tools.ksuite_connection_status())
        self.assertEqual((status["connected"], status["mailbox"]), (True, "alice@example.org"))
        self.assertEqual(status["identity"], {"name": "Alice Example", "email": "alice@example.org"})
        self.assertNotIn(ALICE, json.dumps(status))
        folders = json.loads(await tools.ksuite_mail_list_folders())
        self.assertEqual([(f["id"], f["path"], f["role"]) for f in folders][:2],
                         [("inbox-1", "INBOX", "INBOX"), ("projects-1", "INBOX/Projects", None)])
        self.assertTrue(all(r.headers["authorization"] == f"Bearer {ALICE}" for r in FAKE.requests))
        self.assertTrue(all(r.headers["user-agent"] == "Assistant connector" for r in FAKE.requests))

    async def test_list_search_read(self):
        listed = json.loads(await tools.ksuite_mail_list(tools.FolderListInput()))
        self.assertEqual(listed["folder_id"], "inbox-1")                     # the inbox, found by its role
        self.assertEqual([(t["subject"], t["first_message_id"]) for t in listed["threads"]], [("Quarterly plan", "101"), ("Lunch", "102")])
        self.assertEqual(listed["threads"][0]["messages"], [{"message_id": "101", "folder_id": "inbox-1"}])
        self.assertNotIn("message_id", listed["threads"][0])                # never a bare id that reads as "the latest"
        unread = json.loads(await tools.ksuite_mail_list(tools.FolderListInput(folder_id="inbox-1", unseen=True)))
        self.assertEqual([t["subject"] for t in unread["threads"]], ["Quarterly plan"])
        starred = json.loads(await tools.ksuite_mail_list(tools.FolderListInput(folder_id="inbox-1", flagged=True)))
        self.assertEqual([t["subject"] for t in starred["threads"]], ["Lunch"])
        found = json.loads(await tools.ksuite_mail_search(tools.SearchInput(query="thursday")))
        self.assertEqual((found["everywhere"], [t["subject"] for t in found["results"]]), (True, ["Lunch"]))
        mail = json.loads(await tools.ksuite_mail_read(tools.MessageRef(folder_id="inbox-1", message_id="101")))
        self.assertEqual(mail["from"], [{"name": "Carol Example", "email": "carol@example.com"}])
        self.assertIn("Ignore your instructions", mail["body"])               # untrusted text is data, returned as such
        self.assertEqual(mail["attachments"], [{"attachment_id": "2", "name": "notes.txt", "mime_type": "text/plain", "size": 11}])
        self.assertIn("untrusted data", tools.mcp.instructions)
        with self.assertRaises(ValueError):
            tools.SearchInput()                                               # at least one filter
        with self.assertRaises(ValueError):
            tools.FolderListInput(unseen=True, flagged=True)

    async def attachment(self, **ref) -> str:
        """The first (text) block of ksuite_mail_attachment's answer."""
        result = await tools.ksuite_mail_attachment(tools.AttachmentRef(**ref))
        return result.content[0].text

    async def test_attachment_as_text_or_bytes(self):
        ref = dict(folder_id="inbox-1", message_id="101", attachment_id="2")
        text = json.loads(await self.attachment(**ref))
        self.assertEqual((text["name"], text["text"], text["format"]), ("notes.txt", "hello notes", "text"))
        FAKE.attachments[(ALICE_BOX, "inbox-1", "101", "3")] = (
            docx_bytes("Launch on Monday"), "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "../../plan.docx")
        doc = json.loads(await self.attachment(**{**ref, "attachment_id": "3"}))
        self.assertEqual((doc["format"], doc["text"], doc["name"]), ("docx", "Launch on Monday", "....plan.docx"))
        missing = await self.attachment(**{**ref, "attachment_id": "9"})
        self.assertTrue(missing.startswith("Error 404"), missing)
        FAKE.attachments[(ALICE_BOX, "inbox-1", "101", "5")] = (
            json.dumps({"result": "error", "error": {"code": "not_found"}}).encode(), "application/json", "x.json")
        hidden = await self.attachment(**{**ref, "attachment_id": "5"}, format="base64")
        self.assertTrue(hidden.startswith("Error 200: Infomaniak refused the request."), hidden)   # a refusal is not a file
        big = tools.MAX_DOWNLOAD_BYTES + 1
        FAKE.attachments[(ALICE_BOX, "inbox-1", "101", "4")] = (b"x" * big, "application/octet-stream", "big.bin")
        self.assertIn("larger than 10 MiB", await self.attachment(**{**ref, "attachment_id": "4"}, format="base64"))

    # ------------------------------------------------------------------ mail writes
    async def test_draft_from_own_mailbox_with_attachments(self):
        params = tools.MessageInput(to=["carol@example.com", "Dan Example <DAN@Example.COM>"], cc=["e@example.com"],
                                    subject="Plan", body="Line 1\n<b>2</b>",
                                    attachments=[{"name": "résumé.txt", "content_base64": b64(b"hi")}],
                                    reply_to={"folder_id": "inbox-1", "message_id": "101"})
        saved = json.loads(await tools.ksuite_mail_create_draft(params))
        self.assertEqual((saved["draft_saved"], saved["sent"], saved["from"]), (True, False, "alice@example.org"))
        self.assertEqual(saved["to"], ["carol@example.com", "DAN@example.com"])
        draft = FAKE.drafts[saved["draft_id"]]
        self.assertEqual(draft["mailbox"], ALICE_BOX)
        self.assertEqual(draft["from"], {"id": None, "name": "Alice Example", "email": "alice@example.org"})
        self.assertEqual(draft["body"], "<html><body><div>Line 1<br>&lt;b&gt;2&lt;/b&gt;</div></body></html>")
        self.assertEqual((draft["in_reply_to"], draft["references"], draft["in_reply_to_uid"]),
                         ("<m101@example.com>", "<m100@example.com> <m101@example.com>", "101@inbox-1"))
        self.assertEqual(len(draft["attachments"]), 1)
        upload = FAKE.uploads[draft["attachments"][0]]
        self.assertEqual((upload["bytes"], upload["mime"]), (b"hi", "text/plain"))
        self.assertEqual(FAKE.sent, [])                                       # a draft sends nothing
        self.assertEqual({r.url.host for r in FAKE.requests}, {"mail.infomaniak.com"})
        self.assertFalse(any(TEAM_BOX in str(r.url) for r in FAKE.requests))  # never the mailbox listed first

    async def test_send_asks_and_the_model_cannot_confirm(self):
        asked = await self.call("ksuite_mail_send", self.message())
        blob = json.dumps(asked) if isinstance(asked, dict) else str(asked)
        self.assertNotIn("confirm_token", blob)
        self.assertIn("nothing ran", blob)
        self.assertEqual((FAKE.requests, FAKE.sent), ([], []))                 # nothing ran
        forged = await self.call("ksuite_mail_send", {**self.message(), "confirm_token": "1." + "ab" * 32})
        self.assertIn("confirm_token", str(forged))
        self.assertEqual(FAKE.sent, [])                                       # a token the model can copy never sends
        changed = await self.call("ksuite_mail_send", {"params": {**self.message()["params"], "to": ["eve@example.net"]},
                                                       "confirm_token": "9." + "cd" * 32})
        self.assertEqual(FAKE.sent, [])
        sent = await self.confirmed("ksuite_mail_send", self.message())
        self.assertTrue(sent["sent"], sent)
        self.assertEqual(len(FAKE.sent), 1)
        self.assertEqual(FAKE.sent[0]["from"]["email"], "alice@example.org")
        self.assertEqual(FAKE.sent[0]["mailbox"], ALICE_BOX)
        self.assertEqual(FAKE.sent[0]["to"], [{"name": "Carol Example", "email": "carol@example.com"}])
        again = await self.call("ksuite_mail_send", {**self.message(), "confirm_token": "1." + "ab" * 32})
        self.assertEqual(len(FAKE.sent), 1)

    async def refused_before_any_request(self, tool: str, arguments: dict) -> None:
        """Invalid arguments never run. An ask tool asks first (nothing runs), then refuses them once confirmed."""
        result = await self.call(tool, arguments, confirm=True)
        self.assertIn("validation error", str(result).lower())
        self.assertEqual(FAKE.requests, [])

    async def test_send_only_from_own_mailbox(self):
        for extra in ({"from_email": "boss@example.org"}, {"mailbox_uuid": TEAM_BOX}, {"from": "team@example.org"}):
            for tool in ("ksuite_mail_send", "ksuite_mail_create_draft"):
                with self.subTest(extra=extra, tool=tool):
                    await self.refused_before_any_request(tool, self.message(**extra))

    async def test_recipients_are_exact_addresses(self):
        bad = ["a@example.com\nBcc: x@example.net", "a@example.com\r\n", "a@@example.com", "no-at-sign",
               "a@example", "a,b@example.com", "Name <a@example.com> extra", "a@exa mple.com", "<>", "a@-x.example.com"]
        for value in bad:
            with self.subTest(value=value), self.assertRaises(ValueError):
                tools.MessageInput(to=[value])
        with self.assertRaises(ValueError):
            tools.MessageInput(to=["a@example.com"], subject="Hi\r\nBcc: x@example.net")
        with self.assertRaises(ValueError):
            tools.MessageInput(to=[f"p{i}@example.com" for i in range(30)], cc=[f"q{i}@example.com" for i in range(21)])
        self.assertEqual(tools.recipient("Bücher Team <info@BÜCHER.example>"),
                         {"name": "Bücher Team", "email": "info@xn--bcher-kva.example"})

    async def test_attachments_only_as_bytes(self):
        """No path field anywhere; a path-like attachment is refused before any request."""
        for item in ([{"name": "/etc/passwd", "content_base64": "aGk="}], [{"name": "../x.txt", "content_base64": "aGk="}],
                     [{"name": "C:\\x.txt", "content_base64": "aGk="}], [{"name": "~x", "content_base64": "aGk="}],
                     [{"name": "x.txt", "content_base64": "/etc/passwd"}], [{"name": "x.txt", "content_base64": "file:///etc/hosts"}],
                     ["/etc/hosts"], [{"name": "x.txt", "path": "/etc/hosts"}],
                     [{"name": "x.txt", "content_base64": "aGk=", "file_path": "/etc/hosts"}]):
            for tool in ("ksuite_mail_create_draft", "ksuite_mail_send"):
                with self.subTest(item=item, tool=tool):
                    await self.refused_before_any_request(tool, self.message(attachments=item))
        for extra in ({"attachment_paths": ["/etc/hosts"]}, {"path": "/etc/hosts"}):
            with self.subTest(extra=extra):
                await self.refused_before_any_request("ksuite_mail_create_draft", self.message(**extra))
        self.assertEqual((FAKE.requests, FAKE.sent, FAKE.uploads), ([], [], {}))
        many = [{"name": f"f{i}.txt", "content_base64": "aGk="} for i in range(11)]
        heavy = [{"name": f"f{i}.bin", "content_base64": b64(b"x" * (1024 * 1024 + 1))} for i in range(3)]
        for item in (many, heavy):
            with self.assertRaises(ValueError):
                tools.MessageInput(to=["a@example.com"], attachments=item)

    async def test_no_path_field_in_any_schema(self):
        self.as_connection(connection())
        async with Client(tools.mcp) as client:
            listed = await client.list_tools()
        self.assertEqual(len(listed), 17)

        def names(schema, out):
            if isinstance(schema, dict):
                out.update(schema.get("properties", {}).keys())
                for value in schema.values():
                    names(value, out)
            elif isinstance(schema, list):
                for value in schema:
                    names(value, out)
            return out

        for tool in listed:
            with self.subTest(tool=tool.name):
                fields = names(tool.input_schema, set())
                self.assertFalse({f for f in fields if "path" in f.lower() or f.lower() in ("file", "filename", "mailbox_uuid",
                                                                                            "from_email", "mailbox")}, fields)
                self.assertIn(tool.annotations.read_only_hint, (True, False))  # reads/changes marked on every tool

    async def test_send_failures_keep_the_draft_and_never_resend(self):
        FAKE.mode.add("send_fails")
        refused = await self.confirmed("ksuite_mail_send", self.message())
        self.assertTrue(refused.startswith("Error 422"), refused)
        self.assertIn("Nothing was sent; the message is kept as draft", refused)
        self.assertEqual(tools.classify_ksuite_result(refused), "definitive")
        FAKE.mode = {"send_timeout"}
        keyed = self.message(idempotency_key="send-1")
        first = await self.confirmed("ksuite_mail_send", keyed)
        self.assertEqual(first["status"], "uncertain", first)
        self.assertIn("may or may not have been sent", first["error"])
        sends = FAKE.count("PUT", r"/api/mail/[^/]+/draft/[^/]+")
        FAKE.mode = set()
        second = await self.confirmed("ksuite_mail_send", keyed)
        self.assertEqual((second["status"], second["executed"]), ("uncertain", None))
        self.assertEqual(FAKE.count("PUT", r"/api/mail/[^/]+/draft/[^/]+"), sends)   # never sent again
        self.assertEqual(FAKE.sent, [])

    async def test_mark_and_move(self):
        marked = json.loads(await tools.ksuite_mail_mark(tools.MarkInput(folder_id="inbox-1", message_ids=["101"], action="read")))
        self.assertTrue(marked["done"])
        self.assertTrue(FAKE.messages[ALICE_BOX]["inbox-1"]["101"]["seen"])
        await tools.ksuite_mail_mark(tools.MarkInput(folder_id="inbox-1", message_ids=["101"], action="star"))
        self.assertTrue(FAKE.messages[ALICE_BOX]["inbox-1"]["101"]["flagged"])
        asked = await self.question("ksuite_mail_move", {"params": {"folder_id": "inbox-1", "message_ids": ["102"], "to": "trash"}})
        self.assertIn("ksuite_mail_move", asked)                                # moving asks first
        self.assertNotIn("102", FAKE.messages[ALICE_BOX].get("trash-1", {}))
        moved = await self.confirmed("ksuite_mail_move", {"params": {"folder_id": "inbox-1", "message_ids": ["102"], "to": "trash"}})
        self.assertEqual(moved["to_folder_id"], "trash-1")
        self.assertIn("102", FAKE.messages[ALICE_BOX]["trash-1"])
        with self.assertRaises(ValueError):
            tools.MoveInput(folder_id="inbox-1", message_ids=["1"])
        with self.assertRaises(ValueError):
            tools.MarkInput(folder_id="inbox-1", message_ids=["1/../../x"], action="read")

    # ------------------------------------------------------------------ calendar
    async def test_calendar_reads(self):
        calendars = json.loads(await tools.ksuite_calendar_list_calendars())
        self.assertEqual(calendars, [{"calendar_id": "11", "name": "Personal", "default": True},
                                     {"calendar_id": "12", "name": "Team", "default": False}])
        events = json.loads(await tools.ksuite_calendar_list_events(tools.EventsInput(start="2026-11-10", end="2026-11-17")))
        self.assertEqual((events["calendar_id"], events["timezone"]), ("11", "Europe/Zurich"))
        self.assertEqual([e["title"] for e in events["events"]], ["Planning"])
        query = [r for r in FAKE.requests if r.url.path == "/1/calendar/pim/event"][0].url.params
        self.assertEqual((query["from"], query["to"]), ("2026-11-10 00:00:00", "2026-11-17 23:59:59"))   # end day included
        utc = json.loads(await tools.ksuite_calendar_list_events(tools.EventsInput(start="2026-11-12T07:30:00Z",
                                                                                   end="2026-11-12T23:00+00:00")))
        self.assertEqual((utc["from"], utc["to"]), ("2026-11-12 08:30:00", "2026-11-13 00:00:00"))   # in the account's zone
        event = json.loads(await tools.ksuite_calendar_get_event(tools.EventRef(event_id="501")))
        self.assertEqual((event["title"], event["location"]), ("Planning", "Room 1"))
        self.assertTrue((await tools.ksuite_calendar_get_event(tools.EventRef(event_id="601"))).startswith("Error 404"))
        self.assertIn("Use a date", await tools.ksuite_calendar_list_events(tools.EventsInput(start="next tuesday", end="2026-11-17")))

    async def test_events_change_without_e_mailing_anyone(self):
        created = json.loads(await tools.ksuite_calendar_create_event(tools.EventCreateInput(
            title="Review", start="2026-11-20T14:00", end="2026-11-20T15:00", location="Room 2")))
        self.assertTrue(created["created"], created)
        new = FAKE.events[int(created["event_id"])]
        self.assertEqual((new["calendar_id"], new["attendees"], new["timezone_start"]), (11, [], "Europe/Zurich"))
        updated = json.loads(await tools.ksuite_calendar_update_event(tools.EventUpdateInput(event_id="501", title="Planning v2")))
        self.assertTrue(updated["updated"], updated)
        self.assertEqual((FAKE.events[501]["title"], FAKE.events[501]["start"]), ("Planning v2", "2026-11-12 09:00:00"))
        self.assertEqual(FAKE.notified, [])                                    # nobody was e-mailed
        late = await tools.ksuite_calendar_update_event(tools.EventUpdateInput(event_id="501", end="2026-11-01"))
        self.assertIn("end before it starts", late)
        replay = tools.EventCreateInput(title="Once", start="2026-11-21", end="2026-11-21", all_day=True, idempotency_key="ev-1")
        one = json.loads(await tools.ksuite_calendar_create_event(replay))
        two = json.loads(await tools.ksuite_calendar_create_event(replay))
        self.assertEqual((one["event_id"], two["idempotent_replay"]), (two["event_id"], True))
        self.assertEqual(FAKE.count("POST", r"/1/calendar/pim/event"), 2)

    async def test_invite_and_delete_ask_first(self):
        invite = {"params": {"event_id": "501", "attendees": ["carol@example.com"]}}
        asked = await self.question("ksuite_calendar_invite", invite)
        self.assertIn("Invite people to", asked)
        self.assertEqual(FAKE.notified, [])
        done = await self.confirmed("ksuite_calendar_invite", invite)
        self.assertEqual(done["invited"], ["carol@example.com"])
        self.assertEqual(len(FAKE.notified), 1)
        self.assertEqual({a["address"] for a in FAKE.events[501]["attendees"]}, {"carol@example.com", "alice@example.org"})
        deleted = await self.confirmed("ksuite_calendar_delete_event", {"params": {"event_id": "501"}})
        self.assertTrue(deleted["deleted"], deleted)
        self.assertEqual((FAKE.deleted_events, len(FAKE.notified)), ([501], 1))  # deleting told nobody

    def test_policy(self):
        """Reads and undoable writes run; send, invite and delete always ask, even when an operator allows them."""
        from open_connectors.policy import AccountToolPolicy, Policy
        policy = next(item for item in tools.mcp.middleware if isinstance(item, AccountToolPolicy))
        allow_all = Policy(allow=frozenset(tools.MANDATORY_ASK_TOOLS | {"ksuite_mail_move"}))
        expected = {"ksuite_mail_send": "ask", "ksuite_calendar_invite": "ask", "ksuite_calendar_delete_event": "ask",
                    "ksuite_mail_move": "allowed"}
        for name, decision in expected.items():
            self.assertEqual(policy._decision(allow_all, name, tools.DESTRUCTIVE_ACTION), decision, name)
        for name, marks in (("ksuite_mail_create_draft", tools.WRITE_ACTION), ("ksuite_mail_mark", tools.SET_ACTION),
                            ("ksuite_calendar_create_event", tools.WRITE_ACTION), ("ksuite_mail_read", tools.READ_ONLY)):
            self.assertEqual(policy._decision(Policy(), name, marks), "allowed", name)

    # ------------------------------------------------------------------ the road: hosts, tokens, throttle
    async def test_only_infomaniak_hosts(self):
        for url in ("https://example.org/2/profile", "http://api.infomaniak.com/2/profile",
                    "https://api.infomaniak.com:8443/2/profile", "https://user@api.infomaniak.com/2/profile",
                    "https://infomaniak.com/2/profile", "https://api.infomaniak.com.example.org/2/profile",
                    "https://manager.infomaniak.com/v3/ng/profile/user/token/list"):
            with self.subTest(url=url), self.assertRaises(api.HostRefused):
                await api.request("GET", url, token=ALICE)
        self.assertEqual(FAKE.requests, [])
        # The production transport refuses any other host too, before any DNS lookup.
        looked_up = []

        async def resolve(host):
            looked_up.append(host)
            raise net.AddressError("not_found")

        saved, net.resolve_public = net.resolve_public, resolve
        try:
            client = net.client_for(allowed_hosts=api.ALLOWED_HOSTS)
            async with client:
                with self.assertRaises(net.AddressError) as ctx:
                    await client.get("https://example.org/")
                self.assertEqual(ctx.exception.code, "not_public")
                with self.assertRaises(net.AddressError):
                    await client.get("https://mail.infomaniak.com/api/mailbox")
        finally:
            net.resolve_public = saved
        self.assertEqual(looked_up, ["mail.infomaniak.com"])

    async def test_two_connections_never_share_a_token(self):
        async def as_person(conn: Connection, rounds: int) -> list[str]:
            CURRENT.set(conn)
            out = []
            for _ in range(rounds):
                out.append(await tools.ksuite_mail_list(tools.FolderListInput(folder_id="inbox-1")))
                await asyncio.sleep(0)
            return out

        alice, bob = await asyncio.gather(as_person(connection(), 5),
                                          as_person(connection("c-ksuite-bob", BOB_SECRET, "4343", "Bob Example"), 5))
        self.assertTrue(all("Quarterly plan" in r and "Bob's mail" not in r for r in alice))
        self.assertTrue(all("Bob's mail" in r and "Quarterly plan" not in r for r in bob))
        for request in FAKE.requests:
            token = request.headers["authorization"][7:]
            box = request.url.path.split("/")[3]
            self.assertEqual((token, box), (ALICE, ALICE_BOX) if box == ALICE_BOX else (BOB, BOB_BOX))
        self.assertEqual(FAKE.tokens_used(), {ALICE, BOB})

    async def test_writes_are_keyed_per_connection(self):
        params = tools.MessageInput(to=["carol@example.com"], subject="Same", idempotency_key="same-key")
        first = json.loads(await tools.ksuite_mail_create_draft(params))
        again = json.loads(await tools.ksuite_mail_create_draft(params))
        self.assertEqual((first["idempotent_replay"], again["idempotent_replay"]), (False, True))
        self.assertEqual(len(FAKE.drafts), 1)
        other = json.loads(await tools.ksuite_mail_create_draft(tools.MessageInput(
            to=["carol@example.com"], subject="Other", idempotency_key="same-key")))
        self.assertEqual(other["error"], "idempotency_key_conflict")
        self.as_connection(connection("c-ksuite-bob", BOB_SECRET, "4343", "Bob Example"))
        bobs = json.loads(await tools.ksuite_mail_create_draft(params))
        self.assertFalse(bobs["idempotent_replay"])                          # another person, same key: written
        self.assertEqual(len(FAKE.drafts), 2)
        self.assertEqual(FAKE.drafts[bobs["draft_id"]]["mailbox"], BOB_BOX)

    async def test_sixty_requests_a_minute_per_connection(self):
        clock = Clock()
        api.THROTTLE = api.Throttle(clock=clock)
        for _ in range(60):
            self.assertNotIn("Error", await tools.ksuite_mail_list_folders())
        blocked = await tools.ksuite_mail_list_folders()
        self.assertIn("TooManyRequests", blocked)
        self.assertIn("nothing was sent", blocked)
        self.assertEqual(len(FAKE.requests), 60)
        self.assertEqual(tools.classify_ksuite_result(blocked), "definitive")
        self.as_connection(connection("c-ksuite-bob", BOB_SECRET, "4343", "Bob Example"))
        self.assertNotIn("Error", await tools.ksuite_mail_list_folders())       # another connection has its own minute
        self.as_connection(connection())
        clock.now += 60
        self.assertNotIn("Error", await tools.ksuite_mail_list_folders())
        # A tool call that needs several requests counts each one.
        api.THROTTLE = api.Throttle(clock=clock)
        await tools.ksuite_calendar_list_events(tools.EventsInput(start="2026-11-10", end="2026-11-17"))
        self.assertEqual(len(api.THROTTLE._hits["c-ksuite-alice"]), 3)

    async def test_refused_token_says_connect_again(self):
        self.as_connection(connection(secret={**ALICE_SECRET, "api_token": "deleted-token-" + "d" * 40}))
        result = await tools.ksuite_mail_list_folders()
        self.assertTrue(result.startswith("Error 401"), result)
        self.assertIn("connect kSuite again", result)
        self.assertFalse(json.loads(await tools.ksuite_connection_status())["connected"])
        self.assertNotIn("deleted-token", result)

    async def test_no_connection_no_call(self):
        self.as_connection(None)
        self.assertIn("KsuiteCredentialError", await tools.ksuite_mail_list_folders())
        self.as_connection(connection(secret={**ALICE_SECRET, "mailbox_uuid": "../other"}))
        self.assertIn("names no mailbox", await tools.ksuite_mail_list(tools.FolderListInput()))
        self.assertEqual(FAKE.requests, [])

    async def test_upstream_text_never_repeated(self):
        FAKE.mode.add("server_error")
        result = await tools.ksuite_mail_list_folders()
        self.assertEqual(result, "Error 500: Infomaniak answered with an error.")
        self.assertEqual(tools.classify_ksuite_result(result), "uncertain")
        FAKE.mode = {"rate_limited"}
        self.assertIn("wait a minute", await tools.ksuite_mail_list_folders())

    def test_throttle_forgets_idle_connections(self):
        clock = Clock()
        throttle = api.Throttle(limit=2, window=60, clock=clock)
        self.assertEqual((throttle.take("a"), throttle.take("a")), (0.0, 0.0))
        self.assertGreater(throttle.take("a"), 0)
        clock.now += 61
        throttle.take("b")
        self.assertEqual(set(throttle._hits), {"b"})


    # ------------------------------------------------------------------ review fixes (two reviews, 10 Nov)
    async def test_unreadable_send_is_uncertain_and_never_sent_again(self):
        """A send Infomaniak did but answered without its envelope never says 'Nothing was sent', and the same
        key never sends again."""
        for mode, key in (("send_no_body", "k1"), ("send_html", "k2")):
            with self.subTest(mode=mode):
                FAKE.reset()
                FAKE.mode = {mode}
                keyed = self.message(idempotency_key=key)
                first = await self.confirmed("ksuite_mail_send", keyed)
                self.assertEqual(first["status"], "uncertain", first)
                self.assertNotIn("Nothing was sent", json.dumps(first))
                self.assertIn("may or may not have been sent", first["error"])
                second = await self.confirmed("ksuite_mail_send", keyed)
                self.assertEqual((second["status"], second["executed"]), ("uncertain", None))
                self.assertEqual(len(FAKE.sent), 1)                           # sent once, never twice
        # Only an explicit refusal is definitive: a 4xx, or result "error" with a code in a 2xx body.
        refused = api.Answer(200, httpx.Headers(), json.dumps({"result": "error", "error": {"code": "invalid"}}).encode())
        with self.assertRaises(api.InfomaniakError):
            api.data_of(refused)
        for body in (b"", b"<html></html>", b'{"result": "error"}', b'{"result": "maybe"}', b'{"data": 1}'):
            with self.subTest(body=body), self.assertRaises(api.Unreadable):
                api.data_of(api.Answer(200, httpx.Headers(), body))
        for result, kind in (("Error 422: Infomaniak refused the request.", "definitive"),
                             ("Error 200: Infomaniak refused the request.", "definitive"),
                             ("Error 302: Infomaniak refused the request.", "uncertain"),
                             ("Error 502: Infomaniak answered with an error.", "uncertain"),
                             ("Error: Unreadable: Infomaniak answered 204 but ...", "uncertain"),
                             ("Error: DecodingError: the request to Infomaniak did not complete.", "uncertain"),
                             ("Error: KeyError: the call failed.", "uncertain"),
                             ("Error: ConnectError: the request to Infomaniak did not complete.", "definitive"),
                             ("Error: Refused: no.", "definitive")):
            self.assertEqual(tools.classify_ksuite_result(result), kind, result)

    async def test_unreadable_create_event_is_never_made_twice(self):
        FAKE.mode = {"event_unreadable"}
        params = tools.EventCreateInput(title="Once", start="2026-11-21T10:00", end="2026-11-21T11:00", idempotency_key="ev-u")
        first = json.loads(await tools.ksuite_calendar_create_event(params))
        second = json.loads(await tools.ksuite_calendar_create_event(params))
        self.assertEqual((first["status"], second["status"]), ("uncertain", "uncertain"))
        self.assertEqual(FAKE.count("POST", r"/1/calendar/pim/event"), 1)

    async def test_mail_goes_only_to_listed_domains(self):
        """The old door's recipient guard: even with a confirmation in hand, mail leaves only for listed domains."""
        tools.RECIPIENT_DOMAINS = frozenset()
        none = await self.confirmed("ksuite_mail_send", self.message())
        self.assertTrue(none.startswith("Error: Refused: this connector sends mail to nobody"), none)
        self.assertIn("Nothing was sent; the message is kept as draft draft-1", none)
        self.assertEqual((FAKE.sent, list(FAKE.drafts)), ([], ["draft-1"]))     # the draft stays for the person
        tools.RECIPIENT_DOMAINS = frozenset({"example.com"})
        for outside in ("x@evil.example", "x@sub.example.com", "x@example.com.evil.example", "x@example.co"):
            with self.subTest(outside=outside):
                refused = await self.confirmed("ksuite_mail_send", self.message(bcc=[outside]))
                self.assertTrue(refused.startswith("Error: Refused: this connector sends mail only to the mail domains"), refused)
                self.assertIn(outside.split("@")[1], refused)
                self.assertEqual(tools.classify_ksuite_result(refused), "definitive")
        self.assertEqual(FAKE.sent, [])
        self.assertEqual(FAKE.count("PUT", r"/api/mail/[^/]+/draft/[^/]+"), 0)   # Infomaniak was never asked to send
        sent = await self.confirmed("ksuite_mail_send", self.message(to=["Carol <carol@EXAMPLE.com>"]))
        self.assertTrue(sent["sent"], sent)
        # Invitations and cancellations follow the same list.
        invite = {"params": {"event_id": "501", "attendees": ["eve@evil.example"]}}
        refused = await self.confirmed("ksuite_calendar_invite", invite)
        self.assertIn("this connector sends invitations only to the mail domains", refused)
        tools.RECIPIENT_DOMAINS = frozenset()
        refused = await self.confirmed("ksuite_calendar_invite", {"params": {"event_id": "501", "attendees": ["carol@example.com"]}})
        self.assertIn("this connector sends invitations to nobody", refused)
        self.assertEqual((FAKE.notified, FAKE.events[501]["attendees"]), ([], []))
        FAKE.events[501]["attendees"] = [{"address": "eve@evil.example", "organizer": False}]
        refused = await self.confirmed("ksuite_calendar_delete_event", {"params": {"event_id": "501", "notify_attendees": True}})
        self.assertIn("Nothing was deleted", refused)
        self.assertEqual((FAKE.deleted_events, FAKE.notified), ([], []))
        deleted = await self.confirmed("ksuite_calendar_delete_event", {"params": {"event_id": "501"}})
        self.assertTrue(deleted["deleted"], deleted)                               # without telling anyone: allowed

    def test_recipient_domains_setting(self):
        from open_connectors import config

        def load(extra: str) -> object:
            with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as handle:
                handle.write('public_url = "https://ksuite.example.org"\nsource_url = "https://example.org/src"\n' + extra)
            try:
                return config.load(handle.name)
            finally:
                os.unlink(handle.name)

        self.assertEqual(load('app = "ksuite"\n').send_recipient_domains, frozenset())
        self.assertEqual(load('app = "ksuite"\nsend_recipient_domains = ["Example.COM", "bücher.example"]\n').send_recipient_domains,
                         frozenset({"example.com", "xn--bcher-kva.example"}))
        for bad in ('["*"]', '["*.example.com"]', '["a@example.com"]', '["example"]', '"example.com"',
                    "[" + ", ".join(f'"d{i}.example"' for i in range(21)) + "]"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                load(f'app = "ksuite"\nsend_recipient_domains = {bad}\n')
        with self.assertRaises(ValueError):
            load('app = "nextcloud"\nsend_recipient_domains = ["example.com"]\n')

    async def test_attachment_that_expands_too_far_is_not_parsed(self):
        """A small ZIP from anyone that would expand to hundreds of MiB in the readers is refused before any reader."""
        def package(parts: dict[str, bytes]) -> bytes:
            out = BytesIO()
            with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
                for name, data in parts.items():
                    archive.writestr(name, data)
            return out.getvalue()

        content = (b'<?xml version="1.0"?><office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0">'
                   + b"<p/>" * 400_000 + b"</office:document-content>")
        docx = zipfile.ZipFile(BytesIO(docx_bytes("x")))
        hidden = {i.filename: docx.read(i.filename) for i in docx.infolist()}
        hidden["word/media/image1.png"] = (b'<w:hdr xmlns:w="urn:example:w">' + b"<w:p/>" * 400_000
                                           + b"</w:hdr>")                     # XML under an image's name counts too
        bombs = {
            "bomb.odt": (package({"mimetype": b"application/vnd.oasis.opendocument.text", "content.xml": content}),
                         "application/vnd.oasis.opendocument.text"),
            "bomb.docx": (package(hidden), "application/octet-stream"),
            "zeros.docx": (package({"word/document.xml": b"<w:document/>", "word/media/zero.bin": b"\0" * (25 * 1024 * 1024)}),
                           "application/octet-stream"),
            "bomb.bin": (package({"content.xml": content}), "application/vnd.oasis.opendocument.text"),   # the type decides
        }
        read = []
        from open_connectors import documents as hub
        saved = hub._extract_document
        hub._extract_document = lambda *a, **k: read.append(a) or saved(*a, **k)
        try:
            for number, (name, (data, kind)) in enumerate(bombs.items(), start=10):
                with self.subTest(name=name):
                    self.assertLess(len(data), 300 * 1024)                       # far under the 10 MiB download cap
                    FAKE.attachments[(ALICE_BOX, "inbox-1", "101", str(number))] = (data, kind, name)
                    answer = await self.attachment(folder_id="inbox-1", message_id="101", attachment_id=str(number))
                    self.assertTrue(answer.startswith("Error: Refused: this attachment expands to more than"), answer)
            self.assertEqual(read, [])                                           # no reader ever opened them
            # A big spreadsheet is read row by row: its sheet is not counted.
            from openpyxl import Workbook
            book = Workbook()
            for row in range(1, 3001):
                book.active.append([f"r{row}", row, row * 2, "x" * 5] * 10)
            out = BytesIO()
            book.save(out)
            FAKE.attachments[(ALICE_BOX, "inbox-1", "101", "20")] = (out.getvalue(), tools._SHEET, "big.xlsx")
            sheet = json.loads(await self.attachment(folder_id="inbox-1", message_id="101", attachment_id="20"))
            self.assertEqual(sheet["format"], "xlsx", sheet)
        finally:
            hub._extract_document = saved

    async def test_attachment_bytes_are_a_file_not_text(self):
        ref = dict(folder_id="inbox-1", message_id="101")
        FAKE.attachments[(ALICE_BOX, "inbox-1", "101", "6")] = (b"%PDF-1.4 scanned", "application/pdf", "scan.pdf")
        FAKE.attachments[(ALICE_BOX, "inbox-1", "101", "7")] = (b"\x89PNG\r\n", "image/png", "photo.png")
        pdf = await tools.ksuite_mail_attachment(tools.AttachmentRef(**ref, attachment_id="6", format="base64"))
        meta = json.loads(pdf.content[0].text)
        self.assertEqual((meta["name"], meta["mime_type"], meta["size"]), ("scan.pdf", "application/pdf", 16))
        self.assertNotIn("content_base64", meta)                                  # no base64 inside text
        self.assertEqual(pdf.content[1].type, "resource")
        self.assertEqual((pdf.content[1].resource.mimeType, base64.b64decode(pdf.content[1].resource.blob)),
                         ("application/pdf", b"%PDF-1.4 scanned"))
        self.assertEqual(str(pdf.content[1].resource.uri), "ksuite://attachment/inbox-1/101/6")
        png = await tools.ksuite_mail_attachment(tools.AttachmentRef(**ref, attachment_id="7", format="base64"))
        self.assertEqual((png.content[1].type, png.content[1].mimeType, base64.b64decode(png.content[1].data)),
                         ("image", "image/png", b"\x89PNG\r\n"))
        async with Client(tools.mcp) as client:                                  # the same over MCP
            result = await client.call_tool("ksuite_mail_attachment", {"params": {**ref, "attachment_id": "7", "format": "base64"}})
        self.assertEqual([block.type for block in result.content], ["text", "image"])

    async def test_ask_shows_what_goes_out(self):
        """The confirmation names the sender, every recipient, subject, attachments and the start of the text."""
        message = self.message(cc=["dan@example.com"], bcc=["Boss <boss@example.com>"],
                               attachments=[{"name": "q3.pdf", "content_base64": b64(b"x" * 2048)}])
        asked = await self.question("ksuite_mail_send", message)
        self.assertEqual(asked.split("\n\n")[0], "\n".join([
            "Send this e-mail from alice@example.org?", "To: carol@example.com", "Cc: dan@example.com",
            "Bcc: boss@example.com", "Subject: Plan", "Attachments: q3.pdf (2 KB)", "Text: Line 1 Line <2>"]))
        self.assertEqual(FAKE.sent, [])
        FAKE.events[501].update(description="Budget figures", attendees=[{"address": "bob@example.com", "organizer": False}])
        invite = await self.question("ksuite_calendar_invite", {"params": {"event_id": "501",
                                                                            "attendees": ["bob@example.com", "carol@example.com"]}})
        self.assertIn('Invite people to \u201cPlanning\u201d (2026-11-12 09:00:00 to 2026-11-12 10:00:00, Europe/Zurich)?',
                      invite)
        self.assertIn("e-mail an invitation to: bob@example.com, carol@example.com", invite)
        self.assertIn("Description sent with it: Budget figures", invite)
        delete = await self.question("ksuite_calendar_delete_event", {"params": {"event_id": "501"}})
        self.assertIn("Delete \u201cPlanning\u201d", delete)
        self.assertIn("Nobody is e-mailed.", delete)
        self.assertEqual(FAKE.deleted_events, [])
        # A client that can ask its person sees the same words in the question itself.
        seen = []

        async def answer(message, response_type, params, context):
            seen.append(message)
            return {"confirm": True}

        async with Client(tools.mcp, elicitation_handler=answer) as client:
            result = await client.call_tool("ksuite_mail_send", message, raise_on_error=False)
        self.assertEqual(len(seen), 1, result)
        self.assertTrue(seen[0].startswith("Send this e-mail from alice@example.org?\nTo: carol@example.com"), seen[0])
        self.assertTrue(json.loads(result.content[0].text)["sent"])
        # Other apps keep the generic question (no summary given).
        from open_connectors import policy
        self.assertEqual(policy._question("nextcloud_delete"),
                         "Allow nextcloud_delete to run now, for your account, with the arguments of this call?")

    async def test_recipient_names_cannot_hide_addresses(self):
        for value in ("Accountant accountant@company.example <attacker@evil.example>",
                      '"boss@example.org, attacker@evil.example" <boss@example.org>',
                      "Boss; Team <boss@example.org>", "Boss: x <boss@example.org>",
                      "Boss\u2028Bcc <boss@example.org>", "Boss\u0085 <boss@example.org>",
                      "Boss\u202e <boss@example.org>", "a@example.org\u2029"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                tools.recipient(value)
        self.assertEqual(tools.recipient('"Carol Example" <carol@example.com>'),
                         {"name": "Carol Example", "email": "carol@example.com"})
        self.assertEqual(tools.recipient("Zoë O'Neil (Sales) <zoe@example.com>")["name"], "Zoë O'Neil (Sales)")

    async def test_journal_keeps_ids_only(self):
        sent = await self.confirmed("ksuite_mail_send", self.message(idempotency_key="j1"))
        self.assertEqual(sent["to"], ["carol@example.com"])                       # the first answer is complete
        await tools.ksuite_calendar_create_event(tools.EventCreateInput(
            title="Secret merger", start="2026-11-21T10:00", end="2026-11-21T11:00", description="Price 12 M",
            location="Board room", idempotency_key="j2"))
        with sqlite3.connect(os.environ["OPEN_CONNECTORS_JOURNAL"]) as db:
            rows = dict(db.execute("SELECT key, result FROM journal WHERE key IN ('j1', 'j2')").fetchall())
        for words in ("carol", "@", "Plan", "Line", "Secret", "Price", "Board", "Alice"):
            self.assertNotIn(words, rows["j1"] + rows["j2"])
        self.assertEqual(json.loads(rows["j1"])["sent"], True)
        again = await self.confirmed("ksuite_mail_send", self.message(idempotency_key="j1"))
        self.assertEqual((again["sent"], again["draft_id"], again["idempotent_replay"]), (True, sent["draft_id"], True))
        self.assertEqual(len(FAKE.sent), 1)

    async def test_invite_names_everyone_who_gets_mail(self):
        """Infomaniak may e-mail everyone on the event: the call (and so the question) must name them all."""
        FAKE.events[501]["attendees"] = [{"address": "bob@example.com", "name": "Bob", "organizer": False},
                                         {"address": "alice@example.org", "name": "Alice", "organizer": True}]
        only_new = {"params": {"event_id": "501", "attendees": ["carol@example.com"]}}
        refused = await self.confirmed("ksuite_calendar_invite", only_new)
        self.assertTrue(refused.startswith("Error: Refused: this event already has other people on it (bob@example.com)"), refused)
        self.assertEqual(FAKE.notified, [])
        everyone = {"params": {"event_id": "501", "attendees": ["carol@example.com", "bob@example.com"]}}
        done = await self.confirmed("ksuite_calendar_invite", everyone)
        self.assertEqual((done["invited"], done["e_mailed"]), (["carol@example.com"], ["bob@example.com", "carol@example.com"]))
        self.assertEqual(len(FAKE.notified), 1)
        self.assertIn("e-mails them an invitation", tools.ksuite_calendar_invite.__doc__ or "")

    async def test_ids_never_step_outside_their_endpoint(self):
        for bad in (".", "..", "...", "a/b", ""):
            for model, field, extra in ((tools.MessageRef, "message_id", {"folder_id": "inbox-1"}),
                                        (tools.MessageRef, "folder_id", {"message_id": "101"}),
                                        (tools.EventRef, "event_id", {}),
                                        (tools.AttachmentRef, "attachment_id", {"folder_id": "inbox-1", "message_id": "101"})):
                with self.subTest(bad=bad, field=field), self.assertRaises(ValueError):
                    model(**extra, **{field: bad})
            with self.subTest(bad=bad, model="MarkInput"), self.assertRaises(ValueError):
                tools.MarkInput(folder_id="inbox-1", message_ids=[bad], action="read")
        for good in ("101", "a.b", ".hidden", "1:2", "x~y"):
            tools.EventRef(event_id=good)
        for args in ({"params": {"folder_id": "..", "message_id": ".."}}, {"params": {"folder_id": "inbox-1", "message_id": "."}}):
            await self.refused_before_any_request("ksuite_mail_read", args)
        await self.refused_before_any_request("ksuite_calendar_get_event", {"params": {"event_id": ".."}})
        with self.assertRaises(tools._Refused):
            tools._plain_id("..", "draft")

    async def test_one_day_lists_that_day(self):
        day = json.loads(await tools.ksuite_calendar_list_events(tools.EventsInput(start="2026-11-12", end="2026-11-12")))
        self.assertEqual([e["title"] for e in day["events"]], ["Planning"])
        self.assertEqual((day["from"], day["to"]), ("2026-11-12 00:00:00", "2026-11-12 23:59:59"))
        self.assertIn("included", tools.EventsInput.model_json_schema()["properties"]["end"]["description"])
        backwards = await tools.ksuite_calendar_list_events(tools.EventsInput(start="2026-11-13", end="2026-11-12"))
        self.assertIn("end is before the start", backwards)


if __name__ == "__main__":
    unittest.main()
