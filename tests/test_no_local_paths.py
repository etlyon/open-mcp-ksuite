"""kSuite attachments travel only as bytes; paths are refused."""
from __future__ import annotations
import re
import unittest
from pathlib import Path
from pydantic import ValidationError
from open_connectors.apps.ksuite.tools import AttachmentInput, MessageInput

PDF_B64 = "JVBERi0xLjQKJcOkw7zDtsOfCjEgMCBvYmoKPDw+PgplbmRvYmoKdHJhaWxlcgo8PD4+CiUlRU9GCg=="

class NoLocalPaths(unittest.TestCase):
    def test_ksuite_attachments_are_bytes_only(self):
        for extra in ({"path": "/etc/passwd"}, {"file_path": "/data/x.pdf"}):
            with self.subTest(extra=extra), self.assertRaises(ValidationError):
                AttachmentInput(name="x.pdf", content_base64=PDF_B64, **extra)
        for name in ("../../etc/passwd.pdf", "/etc/passwd.pdf", r"C:\x.pdf", "a/b.pdf", "~/.ssh/id_rsa"):
            with self.subTest(name=name), self.assertRaises(ValidationError):
                AttachmentInput(name=name, content_base64=PDF_B64)
        for value in ("/etc/passwd", "file:///etc/passwd", "~/.ssh/id_rsa"):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                AttachmentInput(name="x.pdf", content_base64=value)
        with self.assertRaises(ValidationError):
            MessageInput(to=["a@example.com"], attachments=["/etc/hosts"])

    def test_source_has_no_file_reads_from_input(self):
        allowed = set()
        pattern = re.compile(r"\bopen\(|\.read_bytes\(|\.read_text\(|readFileSync|os\.open\(|Path\(")
        root = Path(__file__).resolve().parent.parent / "src" / "open_connectors" / "apps"
        scanned = {str(path.relative_to(root)) for path in root.rglob("*.py")}
        for module in ("ksuite/adapter.py", "ksuite/api.py", "ksuite/tools.py"):
            self.assertIn(module, scanned)
        found = []
        for path in root.rglob("*.py"):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if pattern.search(line) and not any(path.name == f and token in line for f, token in allowed):
                    found.append(f"{path.relative_to(root)}:{number}: {line.strip()}")
        self.assertEqual(found, [], "an app module reads files; caller-given paths must never be read")

if __name__ == "__main__":
    unittest.main()
