import unittest

from rssbot.feed import FeedError, clean_link, parse_feed

ATOM = """<?xml version='1.0' encoding='UTF-8'?>
<feed xmlns='http://www.w3.org/2005/Atom'>
<title>GeekNews</title>
<entry>
  <title><![CDATA[첫 번째  글]]></title>
  <link rel='alternate' type='text/html' href='https://news.hada.io/topic?id=2&amp;utm_source=geeknews&amp;utm_medium=rss' />
  <id>https://news.hada.io/topic?id=2</id>
</entry>
<entry>
  <title>두 번째 &amp;amp; 글</title>
  <link href='https://news.hada.io/topic?id=1' />
  <id>https://news.hada.io/topic?id=1</id>
</entry>
</feed>""".encode("utf-8")

RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>Blog</title>
<item><title>A</title><link>https://ex.com/a</link><guid>g-a</guid></item>
<item><title>B</title><link>https://ex.com/b</link></item>
</channel></rss>"""


class ParseTests(unittest.TestCase):
    def test_atom(self):
        f = parse_feed(ATOM)
        self.assertEqual(f.title, "GeekNews")
        self.assertEqual([e.id for e in f.entries], ["https://news.hada.io/topic?id=2", "https://news.hada.io/topic?id=1"])
        self.assertEqual(f.entries[0].title, "첫 번째 글")  # 공백 정리
        self.assertEqual(f.entries[0].link, "https://news.hada.io/topic?id=2")  # utm 제거
        self.assertEqual(f.entries[1].title, "두 번째 & 글")

    def test_rss_uses_guid_then_link(self):
        f = parse_feed(RSS)
        self.assertEqual([e.id for e in f.entries], ["g-a", "https://ex.com/b"])

    def test_rejects_non_feed_and_bad_xml(self):
        with self.assertRaises(FeedError):
            parse_feed(b"<html><body>no</body></html>")
        with self.assertRaises(FeedError):
            parse_feed(b"not xml")

    def test_rejects_entity_declarations(self):
        evil = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><rss><channel><title>&a;</title></channel></rss>'
        with self.assertRaises(FeedError):
            parse_feed(evil)

    def test_rejects_entity_declarations_in_utf16(self):
        evil = '<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE x [<!ENTITY a "aaaa">]><rss><channel><title>&a;</title></channel></rss>'
        with self.assertRaises(FeedError):
            parse_feed(evil.encode("utf-16"))

    def test_rejects_parameter_entity_declarations(self):
        evil = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY % p "x">]><rss><channel><title>t</title></channel></rss>'
        with self.assertRaises(FeedError):
            parse_feed(evil)

    def test_plain_doctype_without_entities_is_ok(self):
        ok = b'<?xml version="1.0"?><!DOCTYPE rss><rss version="2.0"><channel><title>t</title></channel></rss>'
        self.assertEqual(parse_feed(ok).title, "t")

    def test_clean_link_keeps_other_params(self):
        self.assertEqual(clean_link("https://a.com/p?id=3&utm_source=x&ref=y"), "https://a.com/p?id=3&ref=y")


class FetchErrorMessageTests(unittest.TestCase):
    """오류 문구는 공개 저장소의 상태 파일에 기록될 수 있으므로 URL·예외 원문이 섞이면 안 된다."""

    def message(self, exc):
        from unittest import mock
        from rssbot.feed import fetch_feed
        with mock.patch("urllib.request.urlopen", side_effect=exc):
            with self.assertRaises(FeedError) as ctx:
                fetch_feed("https://ex.com/rss")
        return str(ctx.exception)

    def test_http_error_does_not_include_message_or_url(self):
        import urllib.error
        e = urllib.error.HTTPError("https://leak.example/x?token=1", 302, "Found https://leak2.example/y", {}, None)
        msg = self.message(e)
        self.assertEqual(msg, "HTTP 302 응답")

    def test_url_error_uses_errno_text_only(self):
        import urllib.error
        msg = self.message(urllib.error.URLError(OSError(8, "nodename nor servname provided, or not known")))
        self.assertIn("nodename", msg)
        msg2 = self.message(urllib.error.URLError("redirect to https://leak.example/?session=abc"))
        self.assertNotIn("leak", msg2)
        self.assertNotIn("session", msg2)

    def test_timeout_and_unexpected_errors(self):
        self.assertEqual(self.message(TimeoutError("https://leak.example")), "응답 시간 초과")
        msg = self.message(RuntimeError("boom https://leak.example/?k=1"))
        self.assertNotIn("leak", msg)
        self.assertIn("RuntimeError", msg)


if __name__ == "__main__":
    unittest.main()
