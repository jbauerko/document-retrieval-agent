"""Document categories, filenames, and validation of observed page data."""

import re

CATEGORIES = ("Exhibits", "Key Documents", "Other Documents", "Transcripts", "Recordings")
METADATA_FIELDS = (
    "title",
    "type",
    "category",
    "status",
    "date_received",
    "decision_date",
    "outcome",
)


def safe_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")[:180]
    return name if name and name not in {".", ".."} else "document"


def normalize(text: str) -> str:
    return " ".join(text.split()).casefold()


def document_in_text(document: dict, text: str) -> bool:
    return bool(re.search(rf"(?<!\w){re.escape(document['id'])}(?!\w)", text)) and normalize(
        document["title"]
    ) in normalize(text)


def parse_counts(text: str) -> dict[str, int]:
    counts = {}
    for category in CATEGORIES:
        match = re.search(rf"(?m)^\s*{re.escape(category)}\s*-\s*([\d,]+)\s*$", text)
        if not match:
            raise ValueError(f"Could not read the live count for {category}")
        counts[category] = int(match[1].replace(",", ""))
    return counts
