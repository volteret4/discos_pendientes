import re
import unicodedata


def normalize(s: str) -> str:
    if not s:
        return ""
    s = re.sub(r"\s+", " ", s.strip().lower())
    s = unicodedata.normalize("NFD", s)
    return "".join(c for c in s if unicodedata.category(c) != "Mn")
