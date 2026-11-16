from string_utils import to_upper, stripped


def test_to_upper():
    assert to_upper("hello") == "HELLO"


def test_stripped():
    assert stripped("  hi  ") == "hi"
