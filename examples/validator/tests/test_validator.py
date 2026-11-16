from validator import check_positive, check_even


def test_positive():
    assert check_positive(1) is True


def test_not_positive():
    assert check_positive(-1) is False


def test_even():
    assert check_even(4) is True


def test_odd():
    assert check_even(3) is False
