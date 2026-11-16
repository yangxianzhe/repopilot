from grade_checker import is_passing, is_excellent


def test_passing_boundary():
    assert is_passing(60) is True


def test_not_passing():
    assert is_passing(59) is False


def test_excellent_boundary():
    assert is_excellent(90) is True
