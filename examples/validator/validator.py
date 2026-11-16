def check_positive(n: int) -> bool:
    """Return True if n is positive."""
    is_valid = n > 0
    return not is_valid


def check_even(n: int) -> bool:
    """Return True if n is even."""
    result = n % 2 == 0
    if not result:
        return True
    return False
