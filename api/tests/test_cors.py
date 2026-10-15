"""CORS: no cross-origin browser access unless it is listed (or DEBUG is on)."""
from django.test import SimpleTestCase, override_settings
from django.urls import reverse

from algo_tracker_backend.settings import cors_settings


class CorsSettingsTests(SimpleTestCase):
    def test_production_allows_no_origin_by_default(self):
        self.assertEqual(cors_settings(False, ''), (False, []))

    def test_reads_comma_separated_origins_and_trims_blanks(self):
        raw = ' https://app.example.com, ,https://admin.example.com '
        self.assertEqual(cors_settings(False, raw),
                         (False, ['https://app.example.com', 'https://admin.example.com']))

    def test_debug_allows_every_origin(self):
        allow_all, _ = cors_settings(True, '')
        self.assertTrue(allow_all)


@override_settings(CORS_ORIGIN_ALLOW_ALL=False, CORS_ALLOWED_ORIGINS=['https://app.example.com'])
class CorsMiddlewareTests(SimpleTestCase):
    def preflight(self, origin):
        return self.client.options(
            reverse('login'),
            HTTP_ORIGIN=origin,
            HTTP_ACCESS_CONTROL_REQUEST_METHOD='POST',
        )

    def test_listed_origin_is_allowed(self):
        response = self.preflight('https://app.example.com')
        self.assertEqual(response['Access-Control-Allow-Origin'], 'https://app.example.com')

    def test_unlisted_origin_gets_no_cors_headers(self):
        response = self.preflight('https://evil.example.net')
        self.assertNotIn('Access-Control-Allow-Origin', response)
