from pathlib import Path

from intellipage.admin import _python_relaunch_arguments


def test_source_script_relaunch_preserves_script_path(tmp_path: Path) -> None:
    script = tmp_path / "run_intellipage.py"
    assert _python_relaunch_arguments(["demo"], script) == [str(script), "demo"]


def test_module_and_console_entry_relaunch_through_package(tmp_path: Path) -> None:
    module = tmp_path / "intellipage" / "__main__.py"
    console = tmp_path / "intellipage.exe"
    assert _python_relaunch_arguments(["snapshot"], module) == [
        "-m",
        "intellipage",
        "snapshot",
    ]
    assert _python_relaunch_arguments(["snapshot"], console) == [
        "-m",
        "intellipage",
        "snapshot",
    ]
