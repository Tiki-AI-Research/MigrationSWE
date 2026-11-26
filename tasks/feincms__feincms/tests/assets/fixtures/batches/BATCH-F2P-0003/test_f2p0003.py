"""Integration predicates for BATCH-F2P-0003 (R030 / F2P-052).

Run from the repository root with the repository's test settings::

    DJANGO_SETTINGS_MODULE=testapp.settings PYTHONPATH=<repo>:<repo>/tests \\
        timeout 300 python -m pytest <this file>::test_handler_404 -q
"""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

import testapp.tests.test_page as page_tests
from feincms import settings as feincms_settings


class TestHandler404(TestCase):
    def setUp(self):
        page_tests.PagesTestCase.setUp(self)

    def _create_page(self, title):
        from django.template.defaultfilters import slugify

        from feincms.module.page.models import Page

        return Page.objects.create(
            title=title,
            slug=slugify(title),
            parent=None,
            template_key="base",
            site=self.site_1,
            in_navigation=False,
            active=False,
            navigation_group="default",
            publication_date=timezone.now() - timedelta(days=1),
        )

    def test_handler_404(self):
        old = feincms_settings.FEINCMS_CMS_404_PAGE
        try:
            feincms_settings.FEINCMS_CMS_404_PAGE = "/404/"
            page = self._create_page("Not found page")
            page.override_url = "/404/"
            page.active = True
            page.publication_date = timezone.now() - timedelta(days=1)
            page.save()
            page.rawcontent_set.create(region="main", ordering=0, text="CUSTOM404BODY")

            response = self.client.get("/missing-path/")
            assert response.status_code == 404
            assert "CUSTOM404BODY" in response.content.decode()
        finally:
            feincms_settings.FEINCMS_CMS_404_PAGE = old

        # Without the setting the Http404 propagates unchanged (default page).
        fallback = self.client.get("/missing-path/")
        assert fallback.status_code == 404
        assert "CUSTOM404BODY" not in fallback.content.decode()
