"""``format_hit`` surfaces credits when the adapter captured them.

The HIT card has always shown email/plan/renewal; this adds a 💰
Credits line for sites (Freepik, future image-gen sites) that expose
a per-account credit balance.
"""

from __future__ import annotations

from tgbot import formatting
from tgbot.scanner import ScanOutcome


def _outcome(info: dict) -> ScanOutcome:
    return ScanOutcome(
        site="freepik.com",
        filename="freepik_cookies.txt",
        alive=True,
        info=info,
        cookies=[{"name": "GR_TOKEN", "value": "x", "domain": ".freepik.com"}],
        elapsed_s=0.5,
    )


def test_hit_includes_credits_with_total() -> None:
    body = formatting.format_hit(_outcome({
        "email": "akaza@freepik.com",
        "plan": "Premium+",
        "credits": 142,
        "credits_total": 200,
    }))
    assert "💰 Credits" in body
    assert "142 / 200" in body
    # Plan and email survive.
    assert "akaza@freepik.com" in body
    assert "Premium+" in body


def test_hit_includes_credits_without_total() -> None:
    body = formatting.format_hit(_outcome({
        "email": "a@b.com",
        "plan": "Free",
        "credits": 5,
    }))
    assert "💰 Credits" in body
    assert "<code>5</code>" in body


def test_hit_renders_zero_total_explicitly() -> None:
    """A legitimate ``credits_total=0`` must NOT be swallowed by ``or``."""
    body = formatting.format_hit(_outcome({
        "email": "a@b.com",
        "plan": "Free",
        "credits": 0,
        "credits_total": 0,
    }))
    assert "💰 Credits" in body
    assert "0 / 0" in body


def test_hit_falls_back_to_downloads_left() -> None:
    body = formatting.format_hit(_outcome({
        "email": "a@b.com",
        "plan": "Free",
        "downloads_left": 3,
        "downloads_quota": 10,
    }))
    assert "💰 Credits" in body
    assert "3 / 10" in body


def test_hit_omits_credits_line_when_field_absent() -> None:
    body = formatting.format_hit(_outcome({
        "email": "a@b.com",
        "plan": "Free",
    }))
    assert "💰 Credits" not in body
    # Other lines still present.
    assert "Email" in body
    assert "Plan" in body
    assert "Renewal" in body
