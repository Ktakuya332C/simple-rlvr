from rlvr.loaders import get_gsm8k, get_math, _extract_number


def test_get_gsm8k():
    dataset = get_gsm8k()
    item = next(iter(dataset.iter_rows()))
    assert set(item.keys()) == {"question", "answer"}


def test_get_math():
    dataset = get_math()
    item = next(iter(dataset.iter_rows()))
    # The get_math function renames "solution" to "answer" after processing
    assert set(item.keys()) == {"problem", "level", "type", "answer"}


def test_get_math_answer_extraction():
    assert _extract_number("The answer is 1234.") == "1234"
    assert _extract_number("The final answer is $\boxed{5}$.") == "5"
    assert _extract_number("The result is 42 apples.") == "42"
    assert _extract_number("No number here.") == ""
    assert _extract_number("The number is 12.34, not 12.") == "12" # TODO: Decide if this is desired behavior for floats
    assert _extract_number("The answer is 1,000.") == "000" # TODO: Decide if this is desired behavior for numbers with commas
