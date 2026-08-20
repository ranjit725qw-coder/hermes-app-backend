import unittest

from youtube_open_app import (
    YOUTUBE_LAUNCH_SELECTOR_ID,
    YOUTUBE_OPEN_ACTION,
    YOUTUBE_PACKAGE_ID,
    is_valid_certificate_sha256,
    is_youtube_open_request,
)


class YouTubeOpenAppIntentTest(unittest.TestCase):
    def test_only_explicit_youtube_open_phrases_match(self):
        self.assertTrue(is_youtube_open_request("Open YouTube"))
        self.assertTrue(is_youtube_open_request("YouTube অ্যাপটি খুলে দাও"))
        self.assertFalse(is_youtube_open_request("Tell me about YouTube"))
        self.assertFalse(is_youtube_open_request("Open my banking app"))
        self.assertFalse(is_youtube_open_request("youtube.com"))

    def test_contract_is_fixed_to_one_package_one_action(self):
        self.assertEqual("com.google.android.youtube", YOUTUBE_PACKAGE_ID)
        self.assertEqual("OPEN_APP", YOUTUBE_OPEN_ACTION)
        self.assertEqual("youtube_launch", YOUTUBE_LAUNCH_SELECTOR_ID)

    def test_certificate_format_is_strict_sha256_hex(self):
        self.assertTrue(is_valid_certificate_sha256("a" * 64))
        self.assertTrue(is_valid_certificate_sha256("A" * 64))
        self.assertFalse(is_valid_certificate_sha256("a" * 63))
        self.assertFalse(is_valid_certificate_sha256("g" * 64))
