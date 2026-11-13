"""Against REAL Infomaniak (a throwaway test account), when one is given.

Set:
- OC_TEST_KSUITE_TOKEN_FILE: a file (mode 0600) holding an Infomaniak API token of the test account,
  made with the scopes user_info, workspace:mail and workspace:calendar;
- OC_TEST_KSUITE_WRITE=1 (optional): also create one event far in the future in the default calendar,
  change it, and delete it again. Without it, the run only reads.

It never sends mail, never saves a draft, never invites anyone and never moves or marks a message.
Requests go through the connector's own guarded road (the real network, Infomaniak's two hosts only).
Skipped when not configured.

Still to prove by hand on the test account before kSuite goes LIVE (nothing here can, without sending):
- the token form's words: identifiers (user_info, workspace:mail, workspace:calendar) or display names,
  in EN, FR and DE, and the product/application choice before them (DESIGN.md, step 2); step 2 of the
  connect window names the accesses exactly as the form shows them;
- the word for "token" in Infomaniak's own French and German pages (FR "token" or "jeton", DE "Token"):
  the connect window's FR/DE words (step 2, the box label, "Collez-le") use the same one;
- "Open Infomaniak" (adapter.TOKEN_PAGE) lands on the token page after sign-in, and that address is
  stable (no session or account number in it); if it is not, link to Infomaniak's own help page instead;
- the send answer: Infomaniak's reply to the send PUT is the usual envelope (otherwise every send is
  reported "may or may not have been sent");
- an invitation to one new person on an event that already has attendees: who gets an e-mail;
- the order of a thread's messages in a listing (first_message_id is messages[0]: the oldest?).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

TOKEN_FILE = os.environ.get("OC_TEST_KSUITE_TOKEN_FILE", "")
WRITE = os.environ.get("OC_TEST_KSUITE_WRITE") == "1"


@unittest.skipUnless(TOKEN_FILE, "set OC_TEST_KSUITE_TOKEN_FILE to run against a real Infomaniak test account")
class RealKsuite(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(TOKEN_FILE)
        if path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise unittest.SkipTest("the token file must be readable by its owner only (chmod 600)")
        cls.token = path.read_text(encoding="utf-8").strip()
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["OPEN_CONNECTORS_JOURNAL"] = f"{cls.tmp.name}/journal.sqlite"
        from open_connectors.apps.ksuite import adapter, api
        cls.saved = (api.TRANSPORT, api.THROTTLE)
        api.TRANSPORT = None                         # the real, guarded road
        api.THROTTLE = api.Throttle()
        cls.identity = asyncio.run(adapter.identify(adapter.SERVER_URL, cls.token, ""))
        from open_connectors.store import Connection
        cls.connection = Connection(conn_id="real-ksuite", app="ksuite", server=adapter.SERVER_URL,
                                    remote_user=adapter.remote_user(cls.identity), client_id="test", resource="test",
                                    display=adapter.display(cls.identity), secret=adapter.secret(cls.token, cls.identity))

    @classmethod
    def tearDownClass(cls):
        from open_connectors.apps.ksuite import api
        api.TRANSPORT, api.THROTTLE = cls.saved
        cls.tmp.cleanup()

    def run_tool(self, call):
        from open_connectors.oauth.server import CURRENT

        async def go():
            CURRENT.set(self.connection)
            return await call()
        result = asyncio.run(go())
        self.assertFalse(result.startswith("Error"), result)
        return json.loads(result)

    def test_identity_and_pinned_mailbox(self):
        self.assertTrue(self.identity.user_id.isdigit())
        self.assertIn("@", self.identity.mailbox.email)
        self.assertNotIn(self.token, json.dumps(self.connection.display))

    def test_reads(self):
        from open_connectors.apps.ksuite import tools
        status = self.run_tool(tools.ksuite_connection_status)
        self.assertTrue(status["connected"], status)
        folders = self.run_tool(tools.ksuite_mail_list_folders)
        self.assertIn("INBOX", {(f.get("role") or "").upper() for f in folders})
        self.assertIn("threads", self.run_tool(lambda: tools.ksuite_mail_list(tools.FolderListInput(limit=3))))
        calendars = self.run_tool(tools.ksuite_calendar_list_calendars)
        self.assertTrue(calendars)
        today = dt.date.today()
        events = self.run_tool(lambda: tools.ksuite_calendar_list_events(tools.EventsInput(
            start=today.isoformat(), end=(today + dt.timedelta(days=7)).isoformat())))
        self.assertIn("events", events)

    @unittest.skipUnless(WRITE, "set OC_TEST_KSUITE_WRITE=1 to create and delete one test event")
    def test_one_event_made_and_removed(self):
        from open_connectors.apps.ksuite import tools
        day = (dt.date.today() + dt.timedelta(days=400)).isoformat()
        created = self.run_tool(lambda: tools.ksuite_calendar_create_event(tools.EventCreateInput(
            title="open-connectors test event", start=f"{day}T09:00", end=f"{day}T09:30")))
        event_id = created["event_id"]
        try:
            self.assertTrue(created["created"])
            self.run_tool(lambda: tools.ksuite_calendar_update_event(tools.EventUpdateInput(
                event_id=event_id, title="open-connectors test event (changed)")))
            read = self.run_tool(lambda: tools.ksuite_calendar_get_event(tools.EventRef(event_id=event_id)))
            self.assertEqual(read["title"], "open-connectors test event (changed)")
        finally:
            gone = self.run_tool(lambda: tools.ksuite_calendar_delete_event(tools.EventDeleteInput(event_id=event_id)))
            self.assertTrue(gone["deleted"])


if __name__ == "__main__":
    unittest.main()
