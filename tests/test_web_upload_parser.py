import unittest
from email.message import Message

from web import server


class WebUploadParserTests(unittest.TestCase):
    def test_server_no_longer_imports_cgi(self) -> None:
        self.assertFalse(hasattr(server, "cgi"))

    def test_parse_multipart_form_extracts_path_and_files(self) -> None:
        boundary = "----test-boundary"
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="path"\r\n'
            "\r\n"
            "reports\r\n"
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="daily.md"\r\n'
            "Content-Type: text/markdown\r\n"
            "\r\n"
            "# Daily\r\n"
            f"--{boundary}--\r\n"
        ).encode("utf-8")
        headers = Message()
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"

        fields, files = server._parse_multipart_form(headers, body)

        self.assertEqual(["reports"], fields["path"])
        self.assertEqual("file", files[0].field_name)
        self.assertEqual("daily.md", files[0].filename)
        self.assertEqual(b"# Daily", files[0].content.rstrip())


if __name__ == "__main__":
    unittest.main()
