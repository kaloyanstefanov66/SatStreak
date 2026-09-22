"""Tests for catalogue fetching and caching.

Nothing here touches the network: every test injects a fetcher. CelesTrak asks
machine clients not to poll, and a test suite that hits it on every CI run across
an eight-job matrix is exactly the behaviour that gets a client blocked.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from satstreak.catalog import (
    PUBLICATION_INTERVAL,
    CatalogError,
    TleRecord,
    cache_age,
    cache_path,
    load_catalog,
    parse_tle,
)

# A real, historical ISS element set. Used as a fixture because a hand-invented
# TLE would not exercise the checksum-bearing column layout the parser relies on.
ISS_NAME = "ISS (ZARYA)"
ISS_LINE1 = "1 25544U 98067A   24001.50000000  .00016717  00000-0  30777-3 0  9993"
ISS_LINE2 = "2 25544  51.6416 247.4627 0006703 130.5360 325.0288 15.49447149 10000"

HST_NAME = "HST"
HST_LINE1 = "1 20580U 90037B   24001.48752314  .00001262  00000-0  64592-4 0  9993"
HST_LINE2 = "2 20580  28.4696 288.8102 0002708 328.0797 194.8871 15.10309691 10001"

TWO_SATELLITES = "\n".join([ISS_NAME, ISS_LINE1, ISS_LINE2, HST_NAME, HST_LINE1, HST_LINE2])


def test_record_exposes_its_norad_id() -> None:
    record = TleRecord(name=ISS_NAME, line1=ISS_LINE1, line2=ISS_LINE2)
    assert record.norad_id == 25544


def test_record_decodes_its_epoch() -> None:
    record = TleRecord(name=ISS_NAME, line1=ISS_LINE1, line2=ISS_LINE2)
    # Day 1.5 of 2024 is noon on 1 January.
    assert record.epoch == datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)


def test_record_handles_the_two_digit_year_convention() -> None:
    # 57-99 mean 1957-1999; 00-56 mean 2000-2056. Sputnik-era elements exist in
    # the catalogue, so a naive "20xx" would date them eighty years late.
    old = TleRecord(
        name="VANGUARD 1",
        line1="1 00005U 58002B   98001.50000000  .00000000  00000-0  00000-0 0  9990",
        line2="2 00005  34.2682 348.7242 1859667 331.7664  19.3264 10.82419157  1000",
    )
    assert old.epoch.year == 1998


def test_record_rejects_lines_from_different_objects() -> None:
    # Two lines that each parse but describe different satellites would silently
    # produce an orbit belonging to neither.
    with pytest.raises(ValueError, match="different objects"):
        TleRecord(name="mismatched", line1=ISS_LINE1, line2=HST_LINE2)


@pytest.mark.parametrize("line", ["1 25544U 98067A", "not a tle line at all"])
def test_record_rejects_malformed_lines(line: str) -> None:
    with pytest.raises(ValueError):
        TleRecord(name="x", line1=line, line2=ISS_LINE2)


def test_parse_reads_several_records() -> None:
    records = parse_tle(TWO_SATELLITES)
    assert [r.norad_id for r in records] == [25544, 20580]


def test_parse_skips_a_corrupt_record_but_keeps_the_rest() -> None:
    # One bad entry in a 28,000-object download should not deny the caller the
    # other 27,999.
    text = "\n".join(["BROKEN", "1 garbage", "2 garbage", ISS_NAME, ISS_LINE1, ISS_LINE2])
    records = parse_tle(text)
    assert [r.norad_id for r in records] == [25544]


def test_parse_rejects_a_response_with_nothing_usable() -> None:
    # CelesTrak sometimes serves an error page with a 200 status. Accepting it as
    # "zero satellites" would present a server error as an empty sky.
    with pytest.raises(CatalogError, match="not GP element data"):
        parse_tle("<html><body>Service unavailable</body></html>")


def test_load_writes_a_cache_then_serves_from_it(tmp_path: Path) -> None:
    calls: list[str] = []

    def fetcher(url: str) -> str:
        calls.append(url)
        return TWO_SATELLITES

    first = load_catalog(group="stations", cache_dir=tmp_path, fetcher=fetcher)
    assert len(first) == 2
    assert len(calls) == 1

    second = load_catalog(group="stations", cache_dir=tmp_path, fetcher=fetcher)
    assert len(second) == 2
    assert len(calls) == 1, "a fresh cache must not trigger a second download"


def test_load_refetches_once_the_cache_passes_its_publication_interval(
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    def fetcher(url: str) -> str:
        calls.append(url)
        return TWO_SATELLITES

    load_catalog(group="stations", cache_dir=tmp_path, fetcher=fetcher)
    path = cache_path("stations", tmp_path)

    # Age the cache past CelesTrak's two-hour publication interval.
    import os

    old = (datetime.now(timezone.utc) - PUBLICATION_INTERVAL - timedelta(minutes=5)).timestamp()
    os.utime(path, (old, old))
    assert (age := cache_age(path)) is not None and age > PUBLICATION_INTERVAL

    load_catalog(group="stations", cache_dir=tmp_path, fetcher=fetcher)
    assert len(calls) == 2


def test_load_falls_back_to_a_stale_cache_when_the_download_fails(tmp_path: Path) -> None:
    def working(url: str) -> str:
        return TWO_SATELLITES

    def broken(url: str) -> str:
        raise CatalogError("CelesTrak returned HTTP 503")

    load_catalog(group="stations", cache_dir=tmp_path, fetcher=working)

    import os

    old = (datetime.now(timezone.utc) - timedelta(days=3)).timestamp()
    os.utime(cache_path("stations", tmp_path), (old, old))

    # Stale elements beat no elements, and the staleness stays visible through
    # cache_age() rather than being hidden from the caller.
    records = load_catalog(group="stations", cache_dir=tmp_path, fetcher=broken)
    assert len(records) == 2


def test_load_raises_when_the_download_fails_and_there_is_no_cache(tmp_path: Path) -> None:
    def broken(url: str) -> str:
        raise CatalogError("CelesTrak returned HTTP 403")

    with pytest.raises(CatalogError, match="403"):
        load_catalog(group="stations", cache_dir=tmp_path, fetcher=broken)


def test_load_does_not_overwrite_a_good_cache_with_a_bad_download(tmp_path: Path) -> None:
    def working(url: str) -> str:
        return TWO_SATELLITES

    def garbage(url: str) -> str:
        return "<html>error page served with status 200</html>"

    load_catalog(group="stations", cache_dir=tmp_path, fetcher=working)

    import os

    old = (datetime.now(timezone.utc) - timedelta(days=1)).timestamp()
    os.utime(cache_path("stations", tmp_path), (old, old))

    with pytest.raises(CatalogError):
        load_catalog(group="stations", cache_dir=tmp_path, fetcher=garbage)

    # The previous good copy must survive, or one bad response would leave the
    # tool with nothing until CelesTrak recovered.
    assert len(load_catalog(group="stations", cache_dir=tmp_path, fetcher=working)) == 2


def test_offline_mode_uses_the_cache_and_never_fetches(tmp_path: Path) -> None:
    def working(url: str) -> str:
        return TWO_SATELLITES

    def must_not_run(url: str) -> str:  # pragma: no cover - asserted never called
        raise AssertionError("network was used while allow_network=False")

    load_catalog(group="stations", cache_dir=tmp_path, fetcher=working)

    import os

    old = (datetime.now(timezone.utc) - timedelta(days=30)).timestamp()
    os.utime(cache_path("stations", tmp_path), (old, old))

    records = load_catalog(
        group="stations", cache_dir=tmp_path, fetcher=must_not_run, allow_network=False
    )
    assert len(records) == 2


def test_offline_mode_raises_when_there_is_no_cache(tmp_path: Path) -> None:
    with pytest.raises(CatalogError, match="network access is disabled"):
        load_catalog(group="stations", cache_dir=tmp_path, allow_network=False)
