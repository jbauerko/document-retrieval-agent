"""Model-visible task and recovery tool definitions."""

from .documents import CATEGORIES, METADATA_FIELDS


def tool(name, description, properties=None):
    properties = properties or {}
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": list(properties),
                "additionalProperties": False,
            },
        },
    }


REF = {"type": "string", "description": "Element ref from the latest page observation"}
TOOL_SCHEMAS = [
    tool(
        "search_matter",
        "Preferred: search the matter and return verified metadata and counts.",
        {
            "matter_number": {"type": "string", "pattern": "^M[0-9]{5}$"},
            "document_type": {"type": "string", "enum": list(CATEGORIES)},
        },
    ),
    tool(
        "open_category",
        "Preferred: open the requested category and return its first up to ten documents.",
        {
            "category": {"type": "string", "enum": list(CATEGORIES)},
        },
    ),
    tool(
        "download_document",
        "Preferred: download one listed document; handles its dialog and verifies the file.",
        {
            "document_id": {"type": "string"},
        },
    ),
    tool(
        "open_site",
        "Start one matter/category request and observe the website. Does NOT search for you.",
        {
            "matter_number": {"type": "string", "pattern": "^M[0-9]{5}$"},
            "document_type": {"type": "string", "enum": list(CATEGORIES)},
        },
    ),
    tool(
        "observe_page",
        "Refresh the page observation and element refs; use when loading or after an error.",
    ),
    tool(
        "click",
        "Click an observed control. Returns a fresh page observation.",
        {"element_ref": REF},
    ),
    tool(
        "fill",
        "Fill an editable field. Custom fields may need a separate click first.",
        {"element_ref": REF, "text": {"type": "string"}},
    ),
    tool(
        "press_key",
        "Press Enter, Tab, Shift+Tab, Escape, arrows, PageUp/PageDown, Home or End.",
        {"key": {"type": "string"}},
    ),
    tool(
        "scroll",
        "Scroll the page or a container under an observed element.",
        {
            "direction": {"type": "string", "enum": ["up", "down"]},
            "element_ref": {"type": ["string", "null"]},
        },
    ),
    tool(
        "record_matter",
        "Record metadata EXACTLY as shown. Counts are verified from page text by code.",
        {
            "metadata": {
                "type": "object",
                "properties": {key: {"type": ["string", "null"]} for key in METADATA_FIELDS},
                "required": list(METADATA_FIELDS),
                "additionalProperties": False,
            }
        },
    ),
    tool(
        "record_documents",
        "Record the first up to 10 requested-category documents from observed rows.",
        {
            "documents": {
                "type": "array",
                "maxItems": 10,
                "items": {
                    "type": "object",
                    "properties": {"id": {"type": "string"}, "title": {"type": "string"}},
                    "required": ["id", "title"],
                    "additionalProperties": False,
                },
            }
        },
    ),
    tool(
        "download",
        "Click the actual file download control for the document whose row you opened. Saves one file.",
        {"element_ref": REF, "document_id": {"type": "string"}},
    ),
    tool(
        "create_archive", "ZIP successfully downloaded files; verifies completeness and integrity."
    ),
]
