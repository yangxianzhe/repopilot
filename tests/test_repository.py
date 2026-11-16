from __future__ import annotations

from pathlib import Path

from repopilot.repository import GitRepository


def test_search_and_tree_sitter_symbol_lookup(git_repository: Path) -> None:
    repository = GitRepository(git_repository)
    assert repository.search_code("return a - b")[0]["path"] == "maths.py"
    symbols = repository.find_symbol("add")
    assert symbols
    assert symbols[0]["name"] == "add"
    assert symbols[0]["line"] == 1


def test_checked_patch_and_diff(git_repository: Path) -> None:
    repository = GitRepository(git_repository)
    content = (git_repository / "maths.py").read_text(encoding="utf-8")
    patch = repository.create_patch("maths.py", content, content.replace("a - b", "a + b"))
    paths = repository.apply_patch(patch)
    assert paths == ["maths.py"]
    assert "return a + b" in repository.git_diff()


def test_patch_lf_file_on_windows(git_repository: Path) -> None:
    target = git_repository / "maths.py"
    target.write_bytes(b"def add(a, b):\n    return a - b\n")
    repository = GitRepository(git_repository)
    content = target.read_text(encoding="utf-8")
    patch = repository.create_patch("maths.py", content, content.replace("a - b", "a + b"))
    repository.apply_patch(patch)
    assert "return a + b" in target.read_text(encoding="utf-8")
