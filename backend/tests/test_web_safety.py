"""The SSRF boundary, case by case.

This is the control that decides which hosts the platform will open a socket
to. Every refusal here is asserted directly, and the resolution is always
injected, because a test that needs DNS is a test that passes or fails for
reasons unrelated to the code.
"""
from __future__ import annotations

import ipaddress

import pytest

from app.domain.web.types import WebRejectionReason
from app.services.web.safety import (
    METADATA_ADDRESSES,
    UrlSafetyError,
    UrlSafetyPolicy,
    is_public_address,
    peer_address_is_safe,
)

PINNED = "acme.example"


def _policy(addresses=("93.184.216.34",), *, host=PINNED, **kwargs):
    """A policy resolving every host to ``addresses``, with no DNS involved."""
    seen: list[tuple[str, int]] = []

    def resolver(name: str, port: int) -> list[str]:
        seen.append((name, port))
        return list(addresses)

    policy = UrlSafetyPolicy(
        allowed_hosts=kwargs.pop("allowed_hosts", (host,)), resolver=resolver,
        **kwargs,
    )
    return policy, seen


def _reason(url: str, **kwargs) -> WebRejectionReason:
    """The typed refusal for a URL, failing the test if it is accepted."""
    policy, _ = _policy(**kwargs)
    with pytest.raises(UrlSafetyError) as excinfo:
        policy.check(url)
    return excinfo.value.reason


# ===========================================================================
class TestSchemes:
    @pytest.mark.parametrize("url,expected", [
        ("ftp://acme.example/file", WebRejectionReason.SCHEME_NOT_ALLOWED),
        ("file:///etc/passwd", WebRejectionReason.SCHEME_NOT_ALLOWED),
        ("gopher://acme.example/x", WebRejectionReason.SCHEME_NOT_ALLOWED),
        ("data:text/html,<h1>x</h1>", WebRejectionReason.SCHEME_NOT_ALLOWED),
        ("javascript:alert(1)", WebRejectionReason.SCHEME_NOT_ALLOWED),
        ("//acme.example/x", WebRejectionReason.MALFORMED_URL),
        ("/relative", WebRejectionReason.MALFORMED_URL),
        ("", WebRejectionReason.MALFORMED_URL),
    ])
    def test_non_http_schemes_are_refused(self, url, expected):
        assert _reason(url) is expected

    def test_http_and_https_are_what_remain(self):
        policy, _ = _policy()
        assert policy.check("http://acme.example/x").scheme == "http"
        assert policy.check("https://acme.example/x").scheme == "https"


# ===========================================================================
class TestHosts:
    def test_an_unpinned_host_is_refused_before_anything_else(self):
        policy, seen = _policy()
        with pytest.raises(UrlSafetyError) as excinfo:
            policy.check("https://evil.example/x")
        assert excinfo.value.reason is WebRejectionReason.HOST_NOT_PINNED
        # Refused before resolution: a host we were never going to contact
        # must not cause a DNS query either.
        assert seen == []

    def test_a_subdomain_of_a_pinned_host_is_allowed(self):
        policy, _ = _policy()
        assert policy.host_is_pinned("www.acme.example") is True
        target = policy.check("https://www.acme.example/x")
        assert target.host == "www.acme.example"

    def test_a_host_that_merely_ends_with_the_pinned_name_is_not(self):
        """Label boundary, not suffix: this is the classic allowlist bug."""
        policy, _ = _policy()
        assert policy.host_is_pinned("evilacme.example") is False
        with pytest.raises(UrlSafetyError):
            policy.check("https://evilacme.example/x")

    def test_suffix_matching_can_be_switched_off_entirely(self):
        policy, _ = _policy(allow_subdomains=False)
        with pytest.raises(UrlSafetyError):
            policy.check("https://www.acme.example/x")
        assert policy.check("https://acme.example/x").host == "acme.example"

    def test_an_unresolvable_host_is_typed_and_not_raised_raw(self):
        def failing(name, port):
            raise OSError("name or service not known")

        policy = UrlSafetyPolicy(allowed_hosts=(PINNED,), resolver=failing)
        with pytest.raises(UrlSafetyError) as excinfo:
            policy.check(f"https://{PINNED}/x")
        assert excinfo.value.reason is WebRejectionReason.UNRESOLVABLE_HOST

    def test_every_address_in_the_answer_must_be_public(self):
        """One private address in the answer is a rebinding attempt."""
        policy, _ = _policy(addresses=("93.184.216.34", "10.0.0.5"))
        with pytest.raises(UrlSafetyError) as excinfo:
            policy.check(f"https://{PINNED}/x")
        assert excinfo.value.reason is WebRejectionReason.ADDRESS_NOT_PUBLIC

    def test_a_redirect_target_is_validated_like_any_other_url(self):
        policy, _ = _policy()
        allowed = policy.check_redirect(
            f"https://{PINNED}/a", f"https://{PINNED}/b"
        )
        assert allowed.url == f"https://{PINNED}/b"

        with pytest.raises(UrlSafetyError) as excinfo:
            policy.check_redirect(f"https://{PINNED}/a", "http://127.0.0.1/admin")
        assert excinfo.value.reason is not WebRejectionReason.UNRESOLVABLE_HOST


# ===========================================================================
class TestPorts:
    @pytest.mark.parametrize("url", [
        f"https://{PINNED}:8080/x",
        f"https://{PINNED}:22/x",
        f"http://{PINNED}:9200/x",
        f"https://{PINNED}:6379/x",
    ])
    def test_a_forbidden_port_is_refused(self, url):
        assert _reason(url) is WebRejectionReason.PORT_NOT_ALLOWED

    def test_the_default_ports_are_allowed_explicitly_and_by_omission(self):
        policy, _ = _policy()
        assert policy.check("https://acme.example/x").port == 443
        assert policy.check("http://acme.example/x").port == 80
        assert policy.check("https://acme.example:443/x").port == 443


# ===========================================================================
class TestAddresses:
    @pytest.mark.parametrize("address", [
        "127.0.0.1",              # loopback
        "127.1.2.3",              # whole loopback range
        "::1",                    # v6 loopback
        "10.0.0.5",               # RFC1918
        "172.16.9.9",
        "192.168.1.1",
        "169.254.1.1",            # link-local
        "fe80::1",
        "169.254.169.254",        # cloud metadata
        "169.254.170.2",          # ECS metadata
        "fd00:ec2::254",          # metadata over v6
        "0.0.0.0",                # unspecified
        "fc00::1",                # unique-local v6
        "224.0.0.1",              # multicast
        "::ffff:127.0.0.1",       # v4-mapped loopback
        "::ffff:10.0.0.1",        # v4-mapped private
        "not-an-address",
    ])
    def test_these_destinations_are_refused(self, address):
        assert is_public_address(address) is False

    @pytest.mark.parametrize("address", [
        "93.184.216.34", "8.8.8.8", "1.1.1.1", "2606:4700::1111",
    ])
    def test_these_destinations_are_permitted(self, address):
        assert is_public_address(address) is True

    def test_the_metadata_addresses_are_named_and_refused(self):
        assert "169.254.169.254" in METADATA_ADDRESSES
        for address in METADATA_ADDRESSES:
            assert is_public_address(address) is False

    def test_the_check_is_an_allowlist_over_globally_routable_addresses(self):
        """Whatever the flags say, only ``is_global`` addresses pass."""
        for candidate in (
            "0.0.0.0", "100.64.0.1", "192.0.2.5", "198.18.0.1",
            "240.0.0.1", "::",
        ):
            parsed = ipaddress.ip_address(candidate)
            if not parsed.is_global:
                assert is_public_address(candidate) is False


# ===========================================================================
class TestPeerAddress:
    """The rebinding check, which fails closed on any shape it cannot read."""

    def test_a_public_peer_tuple_is_accepted(self):
        assert peer_address_is_safe(("93.184.216.34", 443)) is True

    def test_a_private_or_metadata_peer_is_refused(self):
        assert peer_address_is_safe(("127.0.0.1", 443)) is False
        assert peer_address_is_safe(("169.254.169.254", 80)) is False
        assert peer_address_is_safe("10.0.0.1") is False

    def test_an_address_object_is_read(self):
        assert peer_address_is_safe((ipaddress.ip_address("8.8.8.8"), 443)) is True
        assert peer_address_is_safe((ipaddress.ip_address("::1"), 443)) is False

    def test_an_unrecognised_shape_fails_closed(self):
        assert peer_address_is_safe(None) is False
        assert peer_address_is_safe(()) is False
        assert peer_address_is_safe(object()) is False
        assert peer_address_is_safe((None, 443)) is False
        assert peer_address_is_safe((1234, 443)) is False
