"""1.0.4 (review, 2026-11-24): an IPv6 address that carries an IPv4 address
is judged by the IPv4 address it reaches. Python's ``is_global`` calls the
IPv4-compatible (::/96) and NAT64 (64:ff9b::/96) forms global whatever they
carry, so on an IPv6-only network with NAT64 a hostile DNS answer could
reach a home-network device. One owner: ``network_safety.public_address``,
which the downloader egress guard uses too."""
from __future__ import annotations

import pytest

from services import downloader_egress_guard, network_safety
from services.network_safety import UnsafeDestinationError, public_address


@pytest.mark.parametrize(("address", "public"), [
    ("8.8.8.8", True), ("2606:4700:4700::1111", True),
    ("64:ff9b::8.8.8.8", True), ("::ffff:8.8.8.8", True), ("::8.8.8.8", True),
    ("64:ff9b::192.168.1.1", False), ("64:ff9b::c0a8:101", False), ("64:ff9b::7f00:1", False),
    ("64:ff9b::a9fe:a9fe", False),                       # 169.254.169.254, the cloud metadata address
    ("::192.168.1.1", False), ("::c0a8:101", False), ("::ffff:192.168.1.1", False),
    ("::1", False), ("::", False), ("64:ff9b:1::c0a8:101", False),
    ("2002:c0a8:101::1", False), ("fe80::1%eth0", False), ("192.168.1.1", False),
    ("", False), ("not-an-address", False),
])
def test_an_address_is_public_only_when_what_it_reaches_is(address, public):
    assert public_address(address) is public
    assert network_safety.destination_admitted(address) is public
    assert downloader_egress_guard._is_public(address) is public


def test_a_resolution_to_a_nat64_wrapped_home_address_is_refused_whole():
    with pytest.raises(UnsafeDestinationError, match="non-public"):
        network_safety.reject_non_public_resolution(["2606:4700:4700::1111", "64:ff9b::c0a8:101"], host="x.example")
    network_safety.reject_non_public_resolution(["64:ff9b::808:808"], host="x.example")


@pytest.mark.parametrize("url", ["http://[64:ff9b::c0a8:101]/f.bin", "http://[::c0a8:101]/f.bin",
                                 "https://[::ffff:192.168.1.1]/f.bin"])
def test_a_provider_link_naming_one_is_refused(url):
    with pytest.raises(UnsafeDestinationError, match="non-public"):
        network_safety.validate_provider_download_url(url, context="download link")
