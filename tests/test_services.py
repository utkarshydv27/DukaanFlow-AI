import pytest

from services import classify_message


@pytest.fixture(autouse=True)
def disable_ollama(monkeypatch):
    monkeypatch.setenv("OLLAMA_ENABLED", "false")


def test_explicit_today_skip():
    result = classify_message("Bhaiya, aaj mere tiffin mat lana.")
    assert result["intent"] == "skip_today"
    assert result["date"] == "today"
    assert result["confidence"] >= 0.75


def test_recurring_subscription_change_is_not_a_one_day_skip():
    result = classify_message("Bhaiya meri tiffin subscription ab se band kar do.")
    assert result["intent"] == "cancel_subscription"
    assert result["date"] == "unknown"


def test_missing_date_is_not_assumed_to_be_today():
    result = classify_message("Tiffin mat bhejna")
    assert result["intent"] == "skip_today"
    assert result["date"] == "unknown"
    assert result["confidence"] < 0.75


def test_tomorrow_goes_to_date_review():
    result = classify_message("Kal mere tiffin mat bhejna please.")
    assert result["intent"] == "skip_today"
    assert result["date"] == "tomorrow"


def test_non_skip_message_is_unknown():
    result = classify_message("Good morning bhaiya")
    assert result["intent"] == "unknown"



def test_hindi_script_same_day_skip():
    result = classify_message("आज मेरा टिफिन मत भेजना")
    assert result["intent"] == "skip_today"
    assert result["date"] == "today"
    assert result["confidence"] >= 0.75
