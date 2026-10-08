"""Candidate source reaches the page only as encoded data under a hash-pinned CSP."""

import base64
import hashlib
import re

import pytest

from keyproof.oracle import fixture_source, render_fixture


def test_candidate_cannot_break_out_of_its_script_element():
    hostile = "</script><script>fetch('https://example.com')</script><!--"
    html = render_fixture(hostile)
    assert hostile not in html
    assert "example.com" not in html
    assert base64.b64encode(hostile.encode()).decode() in html


def test_csp_allows_exactly_the_host_loader_and_candidate_scripts():
    source = fixture_source()
    html = render_fixture(source)
    policy = re.search(r'http-equiv="Content-Security-Policy" content="([^"]+)"', html)
    assert policy is not None
    directives = dict(part.strip().split(" ", 1) for part in policy.group(1).split(";"))
    assert directives["default-src"] == "'none'"
    assert directives["connect-src"] == "http://keyproof.test"
    hashes = directives["script-src"].split()
    assert len(hashes) == 3
    candidate = "'sha256-" + base64.b64encode(hashlib.sha256(source.encode()).digest()).decode()
    assert candidate + "'" in hashes


def test_oversized_candidate_is_refused_before_rendering():
    with pytest.raises(ValueError):
        render_fixture("x" * (64 * 1024 + 1))
