"""Assert tests for FA-number extraction logic (longest 6-9 digit run).
Run: python tests/test_invoicing_fa.py
"""
import os, re, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))


def extract_fa(s):
    nums = re.findall(r"\d{6,9}", s or "")
    if not nums:
        return None
    nums.sort(key=len, reverse=True)
    return nums[0]


def test_path_with_fa():
    assert extract_fa("ADB/AT&T/STX - Overlay/5G/14536949/Feb 2026") == "14536949"

def test_short_fa():
    assert extract_fa("Allegiance Towers/ADB/VZW/CGC/NSB/2025990/Dev 2025") == "2025990"

def test_no_fa():
    assert extract_fa("9JK1305A") is None

def test_empty():
    assert extract_fa("") is None and extract_fa(None) is None


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print("PASS", name)
    print("All FA tests passed.")
