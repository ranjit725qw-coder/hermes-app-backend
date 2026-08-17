"""Static compatibility checks for the Android WebView startup path.

The affected device is treated as requiring an ES2015-capable parser at minimum
for the current application bundle, while ES2020 optional chaining is explicitly
forbidden because it was the confirmed splash-startup parser blocker. The test
scans the complete main application script, not only the previously failing line.
"""

from pathlib import Path
import re
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parent
FRONTEND = ROOT / "frontend" / "index.html"


def main_application_script() -> str:
    html = FRONTEND.read_text(encoding="utf-8")
    script_start = html.index("<script>\n/* ============================================================\n   HERMES AI — APPLICATION LOGIC")
    script_end = html.index("</script>", script_start)
    return html[script_start:script_end]


def syntax_markers(script: str) -> dict[str, list[str]]:
    patterns = {
        "optional_chaining": r"\?\.",
        "nullish_coalescing": r"\?\?(?!=)",
        "private_class_field": r"^\s*#[A-Za-z_]",
        "logical_assignment": r"(?<![=!<>])[&|]{2}=|(?<![=!<>])\?\?=",
    }
    return {
        name: re.findall(pattern, script, flags=re.MULTILINE)
        for name, pattern in patterns.items()
    }


def test_complete_main_script_has_no_es2020_parser_blockers() -> None:
    script = main_application_script()
    markers = syntax_markers(script)
    assert not markers["optional_chaining"], "optional chaining remains in the main application script"
    assert not markers["nullish_coalescing"], "nullish coalescing remains in the main application script"
    assert not markers["private_class_field"], "private class fields remain in the main application script"
    assert not markers["logical_assignment"], "logical assignment syntax remains in the main application script"


def test_main_script_keeps_startup_and_splash_contract() -> None:
    script = main_application_script()
    html = FRONTEND.read_text(encoding="utf-8")
    assert "function init()" in script
    assert "elements.splash.classList.add('hidden')" in script
    assert "elements.app.classList.add('visible')" in script
    assert "init();" in script
    assert "parentId: state.lastUserMessage ? state.lastUserMessage.id : undefined" in html


def test_complete_scan_covers_the_large_application_block() -> None:
    script = main_application_script()
    assert len(script) > 45_000
    assert script.count("=>") > 20
    assert script.count("const ") > 50
    assert script.count("async ") > 1


def test_complete_main_script_passes_node_parser_check() -> None:
    script = main_application_script().split("\n", 1)[1]
    with tempfile.NamedTemporaryFile(mode="w", suffix=".js", encoding="utf-8") as handle:
        handle.write(script)
        handle.flush()
        result = subprocess.run(
            ["node", "--check", handle.name],
            text=True,
            capture_output=True,
            check=False,
        )
    assert result.returncode == 0, result.stderr


if __name__ == "__main__":
    test_complete_main_script_has_no_es2020_parser_blockers()
    test_main_script_keeps_startup_and_splash_contract()
    test_complete_scan_covers_the_large_application_block()
    test_complete_main_script_passes_node_parser_check()
    print("PASS: complete main-script Android compatibility scan")
    print("PASS: splash/init startup contract preserved")
    print("PASS: full application block was scanned")
    print("PASS: full application block passes node --check")
