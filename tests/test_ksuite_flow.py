"""kSuite end to end in one process: no address step → paste an API token → refused / missing scope /
not listed → connected → tokens → a tool call → revoke (nothing to call in Infomaniak).

Infomaniak is the in-process fake in tests/fake_infomaniak.py (no network).
"""

from __future__ import annotations

import html as htmllib
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from starlette.testclient import TestClient

from tests.fake_infomaniak import ALICE, ALICE_MAIL_ONLY, ALICE_NO_CALENDAR, BOB, FakeInfomaniak
from tests.helpers import back_link, csrf_of, pkce, visible

PUBLIC = "https://ksuite.connector.test"
CLAUDE_CB = "https://claude.ai/api/mcp/auth_callback"
TOKEN_PAGE = "https://manager.infomaniak.com/v3/ng/profile/user/token/list"
FAKE = FakeInfomaniak()


def plain(page: str) -> str:
    return htmllib.unescape(visible(page))


class KsuiteFlow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        conf = Path(cls.tmp.name) / "config.toml"
        conf.write_text(f'public_url = "{PUBLIC}"\napp = "ksuite"\nstate_dir = "{cls.tmp.name}/state"\n'
                        f'source_url = "https://code.example.org/open-connectors"\n')
        os.environ["OPEN_CONNECTORS_JOURNAL"] = f"{cls.tmp.name}/journal.sqlite"
        from open_connectors import config, server
        from open_connectors.apps.ksuite import adapter, api
        from open_connectors.oauth import server as oauth_server
        cls.oauth, cls.adapter, cls.api = oauth_server, adapter, api
        cls.saved = (api.TRANSPORT, api.THROTTLE)
        api.TRANSPORT = FAKE.transport
        cls.application = server.Application(config.load(conf))
        cls.client = TestClient(cls.application, base_url=PUBLIC, follow_redirects=False)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.api.TRANSPORT, cls.api.THROTTLE = cls.saved
        cls.adapter.ALLOWED_MAIL_DOMAINS = frozenset()
        cls.client.__exit__(None, None, None)
        cls.tmp.cleanup()

    def setUp(self):
        FAKE.reset()
        self.api.THROTTLE = self.api.Throttle()
        self.adapter.ALLOWED_MAIL_DOMAINS = frozenset()
        self.application.auth.limits = self.oauth.RateLimiter()
        with self.application.store._tx() as db:              # each test counts its own connections
            db.execute("DELETE FROM connections")

    # ------------------------------------------------------------------ helpers
    def register(self, redirect=CLAUDE_CB, name="Claude"):
        r = self.client.post("/register", json={"redirect_uris": [redirect], "client_name": name,
                                                "token_endpoint_auth_method": "none",
                                                "grant_types": ["authorization_code", "refresh_token"]})
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()["client_id"]

    def start(self, client_id, redirect=CLAUDE_CB, resource=f"{PUBLIC}/mcp"):
        verifier, challenge = pkce()
        r = self.client.get("/authorize", params={"response_type": "code", "client_id": client_id, "redirect_uri": redirect,
                                                  "code_challenge": challenge, "code_challenge_method": "S256",
                                                  "state": "st-1", "resource": resource, "scope": "read write send"})
        return verifier, r

    def token_step(self, client_id, redirect=CLAUDE_CB):
        verifier, r = self.start(client_id, redirect)
        self.assertEqual(r.status_code, 302, r.text)
        path = r.headers["location"]
        page = self.client.get(path)
        self.assertEqual(page.status_code, 200, page.text)
        return verifier, path, page.text

    def tokens(self, client_id, page, verifier):
        q = parse_qs(urlsplit(back_link(page)).query)
        self.assertEqual(q["iss"], [PUBLIC])
        r = self.client.post("/token", data={"grant_type": "authorization_code", "code": q["code"][0],
                                             "redirect_uri": CLAUDE_CB, "code_verifier": verifier, "client_id": client_id})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def mcp(self, token, method, params=None, rid=1):
        return self.client.post("/mcp", json={"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}},
                                headers={"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream",
                                         "MCP-Protocol-Version": "2025-11-25"})

    def connections(self) -> int:
        return self.application.store._db.execute("SELECT COUNT(*) FROM connections").fetchone()[0]

    def held_secret(self, path) -> int:
        return self.application.store._db.execute("SELECT COUNT(*) FROM flows WHERE flow_id=? AND sealed IS NOT NULL",
                                                  (path.rsplit("/", 1)[-1],)).fetchone()[0]

    # ------------------------------------------------------------------ the whole sign-in
    def test_whole_sign_in(self):
        client_id = self.register()
        verifier, path, page = self.token_step(client_id)
        text = plain(page)
        # The first page is the token step: no address box, no "Connecting to … · Change".
        self.assertIn("Connect kSuite to Claude", text)
        self.assertIn("Claude will use kSuite as you, with your own rights — nothing more.", text)
        self.assertIn("Next: Open Infomaniak ↗ Create a token with user_info, workspace:mail and workspace:calendar "
                      "Paste it below", text)
        self.assertIn("API token", text)
        self.assertIn('placeholder="Paste your token here"', page)
        self.assertIn(f'href="{TOKEN_PAGE}" target="_blank" rel="noopener noreferrer"', page)
        self.assertNotIn("Connecting to", text)
        self.assertNotIn("Change", text)
        self.assertNotIn('name="address"', page)
        self.assertNotIn("/change", page)
        self.assertNotRegex(text.lower(), r"\bmcp\b|plugin|connector|\bends?\b|expir|month|valid|\d{5,}")
        self.assertIn("--accent:#E0B84A;--circle:#C99A2E;--circle-ink:#101116;--accent-light:#9C7A1E", page)
        light = "@media (prefers-color-scheme:light){:root{--accent:var(--accent-light)}}"   # #9C7A1E on white, not honey
        self.assertGreater(page.rindex(light), page.index("--accent:#E0B84A"))
        self.assertEqual(FAKE.requests, [])                                # nothing asked before a paste
        csrf = csrf_of(page)
        # 1. A refused token: the orange step, one sentence, nothing kept.
        bad = self.client.post(path + "/key", data={"csrf": csrf, "key": "wrong-token-" + "w" * 40})
        self.assertEqual(bad.status_code, 400)
        self.assertIn('class="fail"', bad.text)
        self.assertIn("kSuite didn't connect. Nothing was changed.", plain(bad.text))
        self.assertIn("Infomaniak refused this token. Make a new one and paste it again.", plain(bad.text))
        self.assertIn(f'href="{path}"', bad.text)                          # Try again = the token step
        self.assertEqual((self.held_secret(path), self.connections()), (0, 0))
        self.assertIn("API token", plain(self.client.get(path).text))
        # 2. A token without Calendar still connects Mail and the profile. Calendar tools stay hidden.
        #    Its own sign-in, so the journey below still has an open token step.
        partial_id = self.register()
        partial_verifier, partial_path, partial_page = self.token_step(partial_id)
        missing = self.client.post(partial_path + "/key", data={"csrf": csrf_of(partial_page), "key": ALICE_NO_CALENDAR})
        self.assertEqual(missing.status_code, 200, missing.text)
        self.assertIn("kSuite is connected to Claude.", plain(missing.text))
        partial_tok = self.tokens(partial_id, missing.text, partial_verifier)
        partial_grant = self.application.store.check_access(partial_tok["access_token"])
        partial = self.application.store.get_connection(partial_grant)
        self.assertEqual(partial.secret["opened"], ["profile", "mail"])
        self.mcp(partial_tok["access_token"], "initialize", {
            "protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}})
        partial_names = {t["name"] for t in self.mcp(partial_tok["access_token"], "tools/list", rid=2).json()["result"]["tools"]}
        self.assertIn("ksuite_mail_list", partial_names)
        self.assertNotIn("ksuite_calendar_list_events", partial_names)
        # 3. The right token: connected.
        before = len(FAKE.requests)
        done = self.client.post(path + "/key", data={"csrf": csrf, "key": "  " + ALICE + "\n"})
        self.assertEqual(done.status_code, 200, done.text)
        self.assertEqual(len(FAKE.requests) - before, 3)                     # profile, mailboxes, calendars
        self.assertTrue(all(r.method == "GET" for r in FAKE.requests))       # the check only reads
        text = plain(done.text)
        self.assertIn('class="ok"', done.text)
        self.assertIn("kSuite is connected to Claude.", text)
        self.assertIn("Signed in as Alice Example (alice@example.org)", text)
        self.assertIn("Not the right account? Delete the token in Infomaniak, then connect again.", text)
        self.assertNotRegex(text.lower(), r"4242|mbx-|\bends?\b|expir|month")   # no raw ids, no expiry words
        self.assertEqual(FAKE.other_hosts(), set())
        tok = self.tokens(client_id, done.text, verifier)
        # The connection keeps the token and the person's own mailbox (not the one listed first).
        grant = self.application.store.check_access(tok["access_token"])
        conn = self.application.store.get_connection(grant)
        self.assertEqual((conn.app, conn.server, conn.remote_user), ("ksuite", "https://api.infomaniak.com", "4242"))
        self.assertEqual(conn.secret["api_token"], ALICE)
        self.assertEqual((conn.secret["mailbox_uuid"], conn.secret["mailbox_email"]), ("mbx-alice-0001", "alice@example.org"))
        self.assertNotIn(ALICE, json.dumps(self.application.store._db.execute("SELECT * FROM connections").fetchall(),
                                           default=repr))                    # sealed, never in clear
        # A tool call, as that person.
        self.assertEqual(self.mcp(tok["access_token"], "initialize", {
            "protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}).status_code, 200)
        names = {t["name"] for t in self.mcp(tok["access_token"], "tools/list", rid=2).json()["result"]["tools"]}
        self.assertEqual(len(names), 17, names)
        self.assertTrue({"ksuite_mail_list", "ksuite_mail_send", "ksuite_calendar_list_events",
                         "ksuite_connection_status"} <= names)
        self.assertNotIn("ksuite_connect", names)
        call = self.mcp(tok["access_token"], "tools/call", {"name": "ksuite_mail_list", "arguments": {"params": {}}}, rid=3).json()
        listed = json.loads(call["result"]["content"][0]["text"])
        self.assertEqual([t["subject"] for t in listed["threads"]], ["Quarterly plan", "Lunch"])
        status = json.loads(self.mcp(tok["access_token"], "tools/call", {"name": "ksuite_connection_status", "arguments": {}},
                                     rid=4).json()["result"]["content"][0]["text"])
        self.assertEqual((status["connected"], status["mailbox"]), (True, "alice@example.org"))
        self.assertNotIn(ALICE, json.dumps(status))
        # Revoke = disconnect: no API lets a token delete itself, so nothing is called in Infomaniak.
        before = len(FAKE.requests)
        r = self.client.post("/revoke", data={"token": tok["refresh_token"], "client_id": client_id})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(FAKE.requests), before)
        self.assertEqual(self.mcp(tok["access_token"], "tools/list", rid=5).status_code, 401)
        self.assertEqual(FAKE.other_hosts(), set())

    def test_every_part_missing_is_named(self):
        """Mail alone still connects. Profile and Calendar stay closed, so their tools are hidden."""
        client_id = self.register()
        verifier, path, page = self.token_step(client_id)
        r = self.client.post(path + "/key", data={"csrf": csrf_of(page), "key": ALICE_MAIL_ONLY})
        self.assertEqual(r.status_code, 200, r.text)
        tok = self.tokens(client_id, r.text, verifier)
        grant = self.application.store.check_access(tok["access_token"])
        conn = self.application.store.get_connection(grant)
        self.assertEqual(conn.secret["opened"], ["mail"])
        self.assertEqual(conn.remote_user, "0")
        self.assertTrue(conn.display["label"])
        self.assertEqual(conn.display["email"], "alice@example.org")
        self.mcp(tok["access_token"], "initialize", {
            "protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}})
        names = {t["name"] for t in self.mcp(tok["access_token"], "tools/list", rid=2).json()["result"]["tools"]}
        self.assertIn("ksuite_mail_list", names)
        self.assertNotIn("ksuite_calendar_list_events", names)
        self.assertIn("ksuite_connection_status", names)

    def test_a_badly_pasted_token_asks_nothing(self):
        _, path, page = self.token_step(self.register())
        for key in ("short", "has spaces " + "x" * 30, "é" * 30):
            with self.subTest(key=key):
                r = self.client.post(path + "/key", data={"csrf": csrf_of(page), "key": key})
                self.assertIn("Paste the whole token, without spaces.", plain(r.text))
        self.assertEqual(FAKE.requests, [])

    def test_infomaniak_trouble_words(self):
        for mode, words in (("rate_limited", "Infomaniak received too many requests. Wait a minute and try again."),
                            ("server_error", "Infomaniak answered with an error. Try again in a minute.")):
            with self.subTest(mode=mode):
                FAKE.reset()
                FAKE.mode.add(mode)
                _, path, page = self.token_step(self.register())
                r = self.client.post(path + "/key", data={"csrf": csrf_of(page), "key": ALICE})
                self.assertEqual(r.status_code, 400)
                self.assertIn(words, plain(r.text))
                self.assertEqual(self.connections(), 0)
        # A profile refusal hidden in HTTP 200 still connects Mail and Calendar. The profile stays closed.
        FAKE.reset()
        FAKE.mode.add("hidden_refusal")
        client_id = self.register()
        verifier, path, page = self.token_step(client_id)
        r = self.client.post(path + "/key", data={"csrf": csrf_of(page), "key": ALICE})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.connections(), 1)
        tok = self.tokens(client_id, r.text, verifier)
        grant = self.application.store.check_access(tok["access_token"])
        conn = self.application.store.get_connection(grant)
        self.assertEqual(conn.remote_user, "0")
        self.assertEqual(conn.secret["opened"], ["mail", "calendar"])

    # ------------------------------------------------------------------ no address step, nothing to change
    def test_no_per_server_address(self):
        client_id = self.register()
        _, r = self.start(client_id, resource=f"{PUBLIC}/api.infomaniak.com/mcp")
        self.assertEqual(r.status_code, 303)
        self.assertIn("error=invalid_target", r.headers["location"])
        self.assertEqual(self.client.get("/.well-known/oauth-protected-resource/api.infomaniak.com/mcp").status_code, 404)
        prm = self.client.get("/.well-known/oauth-protected-resource").json()
        self.assertEqual((prm["resource"], prm["resource_name"]), (f"{PUBLIC}/mcp", "kSuite"))
        self.assertEqual(self.client.post("/api.infomaniak.com/mcp", json={}).status_code, 404)

    def test_change_and_server_posts_change_nothing(self):
        _, path, page = self.token_step(self.register())
        csrf = csrf_of(page)
        for action, data in (("change", {"csrf": csrf}), ("server", {"csrf": csrf, "address": "evil.example"})):
            with self.subTest(action=action):
                r = self.client.post(f"{path}/{action}", data=data)
                self.assertEqual(r.status_code, 303)
                again = plain(self.client.get(path).text)
                self.assertIn("API token", again)
                self.assertNotIn("web address", again)
        self.assertEqual(FAKE.requests, [])

    def test_not_now(self):
        _, path, page = self.token_step(self.register())
        r = self.client.post(path + "/cancel", data={"csrf": csrf_of(page)})
        self.assertEqual(r.status_code, 303)
        self.assertIn("error=access_denied", r.headers["location"])
        self.assertIn("state=st-1", r.headers["location"])
        self.assertEqual((self.connections(), FAKE.requests), (0, []))
        self.assertEqual(self.client.get(path).status_code, 400)            # this sign-in is over

    def test_french_and_german_words(self):
        _, path, page = self.token_step(self.register())
        fr = plain(self.client.get(path + "?lang=fr").text)
        for words in ("Connecter kSuite à Claude", "Ouvrir Infomaniak ↗",
                      "Créer un token avec user_info, workspace:mail et workspace:calendar", "Collez-le ci-dessous",
                      "Token API", "Pas maintenant"):
            self.assertIn(words, fr)
        self.assertIn('placeholder="Collez votre token ici"', self.client.get(path).text)
        de = plain(self.client.get(path + "?lang=de").text)
        for words in ("kSuite mit Claude verbinden", "Infomaniak öffnen ↗",
                      "Token mit user_info, workspace:mail und workspace:calendar erstellen", "Unten einfügen", "API-Token"):
            self.assertIn(words, de)
        refused = plain(self.client.post(path + "/key", data={"csrf": csrf_of(page), "key": "wrong-" + "w" * 40}).text)
        self.assertIn("Infomaniak hat dieses Token abgelehnt.", refused)
        self.assertIn("kSuite wurde nicht verbunden. Nichts wurde geändert.", refused)
        done = plain(self.client.post(path + "/key", data={"csrf": csrf_of(page), "key": ALICE}).text)
        self.assertIn("Angemeldet als Alice Example (alice@example.org)", done)
        self.assertIn("Falsches Konto? Löschen Sie das Token bei Infomaniak und verbinden Sie sich erneut.", done)
        from open_connectors.pages import flow as pages
        for key in ("step.open.ksuite", "step.add.ksuite", "step.paste.ksuite", "token.label", "token.placeholder",
                    "ok.wrong.ksuite"):
            self.assertEqual(set(pages.T[key]), {"en", "fr", "de"}, key)
        self.assertEqual(pages.ERRORS["no_mailbox"]["fr"],
                         "Ce compte Infomaniak n’a aucune boîte mail que ce token puisse ouvrir.")   # subjunctive after aucune … que
        for key in ("bad_token", "token_refused", "missing_scope", "no_mailbox", "which_mailbox", "account_not_listed",
                    "ksuite_unreachable", "ksuite_error", "ksuite_busy"):
            self.assertEqual(set(pages.ERRORS[key]), {"en", "fr", "de"}, key)

    def test_unknown_assistant_says_allow_first(self):
        client_id = self.register(redirect="https://assistant.example/cb", name="Helper")
        verifier, path, page = self.token_step(client_id, redirect="https://assistant.example/cb")
        text = plain(page)
        self.assertIn("“Helper” (assistant.example) wants to use your kSuite.", text)
        moved = self.client.post(path + "/key", data={"csrf": csrf_of(page), "key": ALICE})
        self.assertEqual(moved.status_code, 303)
        allow = self.client.get(path).text
        self.assertIn("Allow “Helper”?", plain(allow))
        self.assertIn("It will read and change what you can in kSuite, as Alice Example (alice@example.org).", plain(allow))
        self.assertEqual(self.connections(), 0)
        done = self.client.post(path + "/allow", data={"csrf": csrf_of(allow)})
        self.assertEqual(done.status_code, 200)
        self.assertIn("kSuite is connected to Helper.", plain(done.text))
        self.assertEqual(self.connections(), 1)

    def test_two_people_two_connections(self):
        tokens = {}
        for key in (ALICE, BOB):
            client_id = self.register()
            verifier, path, page = self.token_step(client_id)
            done = self.client.post(path + "/key", data={"csrf": csrf_of(page), "key": key})
            tokens[key] = self.tokens(client_id, done.text, verifier)["access_token"]
        FAKE.requests.clear()
        for key, mailbox in ((ALICE, "alice@example.org"), (BOB, "bob@example.net")):
            status = json.loads(self.mcp(tokens[key], "tools/call", {"name": "ksuite_connection_status", "arguments": {}})
                                .json()["result"]["content"][0]["text"])
            self.assertEqual(status["mailbox"], mailbox)
        self.assertEqual([r.headers["authorization"][7:] for r in FAKE.requests], [ALICE, ALICE, BOB, BOB])

    # ------------------------------------------------------------------ who may connect (the operator's list)
    def test_mail_domain_list(self):
        self.adapter.ALLOWED_MAIL_DOMAINS = frozenset({"example.net"})
        _, path, page = self.token_step(self.register())
        r = self.client.post(path + "/key", data={"csrf": csrf_of(page), "key": ALICE})
        self.assertEqual(r.status_code, 400)
        self.assertIn("This service works only with the mail domains its operator lists. Ask them to add yours.", plain(r.text))
        self.assertEqual((self.held_secret(path), self.connections()), (0, 0))
        # Listed: connects; taken off the list: stops at once.
        self.adapter.ALLOWED_MAIL_DOMAINS = frozenset({"example.org"})
        client_id = self.register()
        verifier, path, page = self.token_step(client_id)
        done = self.client.post(path + "/key", data={"csrf": csrf_of(page), "key": ALICE})
        tok = self.tokens(client_id, done.text, verifier)["access_token"]
        self.assertEqual(self.mcp(tok, "tools/list").status_code, 200)
        self.adapter.ALLOWED_MAIL_DOMAINS = frozenset({"example.net"})
        self.assertEqual(self.mcp(tok, "tools/list").status_code, 401)

    def test_settings(self):
        from open_connectors import config
        base = f'public_url = "{PUBLIC}"\napp = "ksuite"\nsource_url = "https://code.example.org/x"\n'
        conf = Path(self.tmp.name) / "c2.toml"
        conf.write_text(base + 'allowed_mail_domains = ["Example.ORG", "bücher.example"]\n')
        self.assertEqual(config.load(conf).allowed_mail_domains, frozenset({"example.org", "xn--bcher-kva.example"}))
        for extra in ('allowed_mail_domains = ["alice@example.org"]', 'allowed_mail_domains = ["localhost"]',
                      'allowed_servers = ["api.infomaniak.com"]'):
            with self.subTest(extra=extra):
                conf.write_text(base + extra + "\n")
                with self.assertRaises(ValueError):
                    config.load(conf)
        conf.write_text(base.replace('"ksuite"', '"nextcloud"') + 'allowed_mail_domains = ["example.org"]\n')
        with self.assertRaises(ValueError):
            config.load(conf)


if __name__ == "__main__":
    unittest.main()
