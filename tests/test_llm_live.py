"""Real LiveKit Inference calls against the text interpreter prompt. Skipped unless RUN_LIVE_LLM=1
(uses a few credits): RUN_LIVE_LLM=1 uv run pytest tests/test_llm_live.py -v
"""

import os

import pytest
from dotenv import dotenv_values

from opendoor.corrections import interpret_text

pytestmark = pytest.mark.skipif(os.environ.get("RUN_LIVE_LLM") != "1", reason="set RUN_LIVE_LLM=1 to call the real LLM")


@pytest.fixture(autouse=True)
def real_livekit_credentials(monkeypatch):
    for key, value in dotenv_values(".env").items():
        if key.startswith("LIVEKIT_"):
            monkeypatch.setenv(key, value)


ONE_HOME = [{"id": 1, "name": "Jay", "address": "400 West 59th Street, New York, NY", "condition": "brand new",
             "timeline": "next month", "is_active": 1}]
TWO_HOMES = ONE_HOME + [{"id": 2, "name": "Jay", "address": "12 Bergen St, Brooklyn, NY", "condition": None,
                         "timeline": None, "is_active": 0}]


@pytest.mark.parametrize("text, intent, home, changes", [
    ("actually, I'd like to sell in December", "update", 1, {"timeline": "December"}),
    ("can you text me the summary again?", "summary", 1, None),
    ("what timeline did i give you?", "question", 1, None),
    ("can you give me a ring?", "call_me", None, None),
    ("i also want to sell my place at 88 Pine St, Austin TX", "new_home", None, {"address": None}),
])
async def test_single_home_intents(text, intent, home, changes):
    result = await interpret_text(text, ONE_HOME)
    assert result.intent == intent
    if home:
        assert result.home == home
    for field, value in (changes or {}).items():
        assert field in result.changes
        if value:
            assert result.changes[field].lower() == value.lower()


async def test_names_the_right_home():
    result = await interpret_text("the bergen place needs a new roof", TWO_HOMES)
    assert result.intent == "update" and result.home == 2 and "condition" in result.changes


async def test_price_questions_never_quote_numbers():
    result = await interpret_text("how much will you pay me for it?", ONE_HOME)
    assert result.intent == "question"
    assert "$" not in result.reply and "specialist" in result.reply.lower()
