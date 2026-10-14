"""14 Oct 2026: the mirror page called an AI model for every mirror signal on every load and never loaded."""
import inspect


def test_mirror_api_never_waits_on_ai_for_every_signal():
    from scheduler import health
    src = inspect.getsource(health._api_crypto_signals)
    assert "asyncio.gather(*[_fetch_ai(r) for r in rows])" not in src
    assert "_AI_REASONING_CACHE.get(r.id)" in src and "asyncio.create_task(_explain(todo))" in src
    assert "[:3]" in src                                   # at most three explained per page view


def test_mirror_cards_never_dress_template_text_as_ai():
    from scheduler import health
    assert "Technical momentum and volume expansion supported the move" not in health._HTML
    assert "template text (no AI was available)" in health._HTML
