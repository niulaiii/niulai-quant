"""start.bat must run under cmd.exe on Chinese Windows (GBK code page).

LF-only line endings make cmd.exe misparse labels/goto and blocks, and non-ASCII
bytes get decoded as GBK into garbage commands. Keep the file ASCII + CRLF.
"""
from pathlib import Path

BAT = Path(__file__).resolve().parent.parent / "scripts" / "start.bat"


def test_start_bat_is_ascii():
    data = BAT.read_bytes()
    bad = [i for i, b in enumerate(data) if b > 0x7F]
    assert not bad, f"non-ASCII byte at offset {bad[0]}"


def test_start_bat_uses_crlf():
    data = BAT.read_bytes()
    assert b"\r\n" in data
    assert data.count(b"\n") == data.count(b"\r\n"), "every line must end with CRLF"


def test_start_bat_has_no_multiline_paren_blocks():
    # multi-line ( ... ) blocks are fragile in cmd.exe; use goto labels instead
    for line in BAT.read_text(encoding="ascii").splitlines():
        assert not line.rstrip().endswith("("), line
