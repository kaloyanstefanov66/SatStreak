"""Tests for the command-line layer."""

from __future__ import annotations

import json

import pytest

from satstreak.cli import EXIT_NO_MATCH, main


def test_identify_reports_that_pointing_is_unknown(capsys: pytest.CaptureFixture[str]) -> None:
    # Until plate solving exists the only honest answer is that pointing is unknown.
    # This test is expected to change when milestone 6 lands, not to be deleted.
    assert main(["identify", "photo.jpg", "--json"]) == EXIT_NO_MATCH
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "no_pointing"
    assert payload["candidates"] == []
    assert payload["diagnostics"]["image"] == "photo.jpg"


def test_identify_without_json_prints_the_message(capsys: pytest.CaptureFixture[str]) -> None:
    main(["identify", "photo.jpg"])
    assert "Not implemented yet" in capsys.readouterr().out


def test_a_missing_subcommand_is_an_error() -> None:
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code != 0
