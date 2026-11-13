"""kSuite-only security fixes (SECFIX13). No network. No credential file."""

from __future__ import annotations

import unittest
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

from pypdf import PdfWriter

from open_connectors.apps.ksuite import adapter, tools
from open_connectors.pdf_text import OfficeTextError, extract_office_text, extract_pdf_text


class KsuiteSecurity(unittest.IsolatedAsyncioTestCase):
    def test_pdf_text_is_not_parsed_in_this_process_api(self):
        writer = PdfWriter()
        writer.add_blank_page(width=72, height=72)
        buf = BytesIO()
        writer.write(buf)
        text, stats, _warnings = extract_pdf_text(buf.getvalue())
        self.assertEqual(stats["pages_total"], 1)
        self.assertIsInstance(text, str)

    def test_office_xml_is_parsed_in_the_capped_child(self):
        package = BytesIO()
        with ZipFile(package, "w", ZIP_DEFLATED) as archive:
            archive.writestr("content.xml", (
                b"<office:document-content xmlns:office='urn:oasis:names:tc:opendocument:xmlns:office:1.0' "
                b"xmlns:text='urn:oasis:names:tc:opendocument:xmlns:text:1.0'>"
                b"<text:p>safe child</text:p></office:document-content>"
            ))
        text, stats, _warnings = extract_office_text(package.getvalue(), "odf", 20_000)
        self.assertEqual(text, "safe child")
        self.assertEqual(stats["paragraphs"], 1)

        hostile = BytesIO()
        with ZipFile(hostile, "w", ZIP_DEFLATED) as archive:
            archive.writestr("content.xml", b"<!DOCTYPE a [<!ENTITY x 'boom'>]><a>&x;</a>")
        with self.assertRaises(OfficeTextError):
            extract_office_text(hostile.getvalue(), "odf", 20_000)

    def test_tools_follow_the_parts_that_opened(self):
        self.assertTrue(tools._part_open("ksuite_mail_send", None))
        self.assertFalse(tools._part_open("ksuite_mail_send", {"calendar", "profile"}))
        self.assertTrue(tools._part_open("ksuite_calendar_list_events", {"calendar"}))
        self.assertTrue(tools._part_open("ksuite_connection_status", {"calendar"}))

    async def test_identify_keeps_the_parts_that_opened(self):
        async def probe(url, params, token, denied):
            if "calendar" in url:
                return "refused", None
            if url.endswith("/2/profile"):
                return "ok", {"id": 5, "email": "a@example.org", "display_name": "A"}
            return "ok", [{"uuid": "box-1", "email": "a@example.org", "mailbox": "a"}]

        saved = adapter._probe
        adapter._probe = probe
        try:
            identity = await adapter.identify(adapter.SERVER_URL, "a" * 20, "")
        finally:
            adapter._probe = saved
        self.assertEqual(identity.opened, ("profile", "mail"))
        stored = adapter.secret("a" * 20, identity)
        self.assertEqual(stored["opened"], ["profile", "mail"])
        self.assertEqual(stored["mailbox_email"], "a@example.org")
        self.assertNotIn("api_token_echo", stored)


if __name__ == "__main__":
    unittest.main()
