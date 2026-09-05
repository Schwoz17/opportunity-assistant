"""
Universal Opportunity Application Requirements Extractor
==========================================================
Give it ANY application form (URL, PDF, or screenshot) and it returns
a structured checklist of everything the form asks for.

Extraction cascade (cheapest/most reliable first):
  1. Google Forms parser   -> exact fields from embedded JSON, no LLM
  2. Generic HTML parser   -> <input>/<textarea>/<select> scraping
  3. LLM text pass         -> Groq reads the page text (prose requirements,
                              JS-heavy portals, ambiguous pages)
  4. Vision fallback       -> screenshot of login-walled forms
  5. PDF mode              -> extract text from PDF, run LLM pass

All paths return the same schema:
  {
    "source": "google_forms" | "html_form" | "llm_text" | "vision" | "pdf",
    "title": str,
    "deadline": str | None,
    "fields": [
      {
        "field": str,          # e.g. "Statement of Purpose"
        "type": str,           # text | essay | document | date | select | email | number
        "required": bool,
        "notes": str | None,   # word limits, file formats, etc.
        "effort": str          # "quick" (minutes) or "heavy" (needs lead time)
      }
    ],
    "documents_needed": [str], # transcripts, rec letters, CV...
    "warnings": [str]          # anything that needs lead time or is unusual
  }

Setup:
  pip install requests beautifulsoup4 groq pypdf
  export GROQ_API_KEY=your_key_here

Usage:
  python extractor.py https://forms.google.com/...        # any URL
  python extractor.py application_form.pdf                # PDF file
  python extractor.py screenshot.png                      # image fallback
"""

import json
import os
import re
import sys
import base64
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_TEXT_MODEL = "openai/gpt-oss-120b"
GROQ_VISION_MODEL = "qwen/qwen3.6-27b"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    )
}

# Fields that always need lead time regardless of what the form says
HEAVY_KEYWORDS = [
    "essay", "statement", "letter", "recommendation", "referee", "reference",
    "transcript", "portfolio", "cv", "resume", "video", "proposal", "motivation",
]

EXTRACTION_PROMPT = """You are analyzing a scholarship/program application form or its description.

Extract EVERY piece of information the application requests from the applicant.

Return ONLY valid JSON (no markdown fences, no commentary) with this exact structure:
{
  "title": "name of the scholarship/program if identifiable, else null",
  "deadline": "deadline as stated, else null",
  "fields": [
    {
      "field": "what is being asked for",
      "type": "text|essay|document|date|select|email|number|phone",
      "required": true/false (assume true if unclear),
      "notes": "word limits, file format requirements, character counts, or null"
    }
  ],
  "documents_needed": ["list of documents/uploads required, e.g. transcript, CV"],
  "warnings": ["anything needing lead time (rec letters, transcripts) or unusual requirements"]
}

Content to analyze:
"""


# ---------------------------------------------------------------------------
# Tier 1: Google Forms parser (no LLM needed)
# ---------------------------------------------------------------------------

def extract_google_form(html: str) -> dict | None:
    """Google Forms embed all questions in a JS variable FB_PUBLIC_LOAD_DATA_."""
    match = re.search(r"FB_PUBLIC_LOAD_DATA_\s*=\s*(\[.*?\]);", html, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None

    try:
        form_meta = data[1]
        questions = form_meta[1] or []
        title = form_meta[8] if len(form_meta) > 8 and form_meta[8] else (data[3] if len(data) > 3 else None)
    except (IndexError, TypeError):
        return None

    TYPE_MAP = {
        0: "text", 1: "essay", 2: "select", 3: "select", 4: "select",
        5: "select", 7: "select", 9: "date", 10: "text", 13: "document",
    }

    fields = []
    for q in questions:
        try:
            label = q[1]
            qtype = q[3]
            required = False
            if q[4] and isinstance(q[4], list) and q[4][0]:
                required = bool(q[4][0][2])
            fields.append({
                "field": label,
                "type": TYPE_MAP.get(qtype, "text"),
                "required": required,
                "notes": None,
            })
        except (IndexError, TypeError):
            continue

    if not fields:
        return None

    return {
        "source": "google_forms",
        "title": title,
        "deadline": None,
        "fields": fields,
        "documents_needed": [f["field"] for f in fields if f["type"] == "document"],
        "warnings": [],
    }


# ---------------------------------------------------------------------------
# Tier 2: Generic HTML form parser
# ---------------------------------------------------------------------------

def extract_html_form(html: str) -> dict | None:
    soup = BeautifulSoup(html, "html.parser")
    forms = soup.find_all("form")
    if not forms:
        return None

    def input_count(f):
        return len(f.find_all(["input", "textarea", "select"]))

    form = max(forms, key=input_count)
    if input_count(form) < 3:
        return None

    fields = []
    for el in form.find_all(["input", "textarea", "select"]):
        itype = el.get("type", "text") if el.name == "input" else el.name
        if itype in ("hidden", "submit", "button", "csrf", "password"):
            continue

        label = None
        el_id = el.get("id")
        if el_id:
            lab = form.find("label", attrs={"for": el_id})
            if lab:
                label = lab.get_text(strip=True)
        if not label:
            parent_label = el.find_parent("label")
            if parent_label:
                label = parent_label.get_text(strip=True)
        if not label:
            label = el.get("placeholder") or el.get("aria-label") or el.get("name")
        if not label:
            continue

        TYPE_MAP = {
            "textarea": "essay", "select": "select", "file": "document",
            "email": "email", "date": "date", "number": "number", "tel": "phone",
        }
        fields.append({
            "field": label.strip(": *"),
            "type": TYPE_MAP.get(itype, "text"),
            "required": el.has_attr("required") or "*" in (label or ""),
            "notes": None,
        })

    if len(fields) < 3:
        return None

    title_el = soup.find("h1") or soup.find("title")
    return {
        "source": "html_form",
        "title": title_el.get_text(strip=True) if title_el else None,
        "deadline": None,
        "fields": fields,
        "documents_needed": [f["field"] for f in fields if f["type"] == "document"],
        "warnings": [],
    }


# ---------------------------------------------------------------------------
# Tier 3: LLM text pass (Groq)
# ---------------------------------------------------------------------------

def _groq_chat(messages: list, model: str) -> str:
    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY not set — export it before running.")
    resp = requests.post(
        GROQ_URL,
        headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
        json={"model": model, "messages": messages, "temperature": 0.1, "max_tokens": 2048},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def _parse_llm_json(raw: str) -> dict | None:
    raw = re.sub(r"```(json)?", "", raw).strip()
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def extract_llm_text(page_text: str) -> dict | None:
    text = page_text[:15000]
    raw = _groq_chat(
        [{"role": "user", "content": EXTRACTION_PROMPT + text}],
        GROQ_TEXT_MODEL,
    )
    parsed = _parse_llm_json(raw)
    if not parsed or not parsed.get("fields"):
        return None
    parsed["source"] = "llm_text"
    return parsed


# ---------------------------------------------------------------------------
# Tier 4: Vision fallback (screenshots of login-walled forms)
# ---------------------------------------------------------------------------

def extract_from_image(image_path: str) -> dict | None:
    ext = Path(image_path).suffix.lower().strip(".")
    mime = "jpeg" if ext in ("jpg", "jpeg") else ext
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()

    raw = _groq_chat(
        [{
            "role": "user",
            "content": [
                {"type": "text", "text": EXTRACTION_PROMPT + "(see attached form screenshot)"},
                {"type": "image_url", "image_url": {"url": f"data:image/{mime};base64,{b64}"}},
            ],
        }],
        GROQ_VISION_MODEL,
    )
    parsed = _parse_llm_json(raw)
    if not parsed or not parsed.get("fields"):
        return None
    parsed["source"] = "vision"
    return parsed


# ---------------------------------------------------------------------------
# Tier 5: PDF mode
# ---------------------------------------------------------------------------

def extract_from_pdf(pdf_path: str) -> dict | None:
    from pypdf import PdfReader
    reader = PdfReader(pdf_path)
    text = "\n".join((page.extract_text() or "") for page in reader.pages)
    if len(text.strip()) < 100:
        return None
    result = extract_llm_text(text)
    if result:
        result["source"] = "pdf"
    return result


# ---------------------------------------------------------------------------
# Post-processing: effort tagging + lead-time warnings
# ---------------------------------------------------------------------------

def tag_effort(result: dict) -> dict:
    warnings = list(result.get("warnings") or [])
    for f in result["fields"]:
        name = f["field"].lower()
        # Essays always need lead time. For "document" fields, only ones
        # matching a HEAVY_KEYWORDS term (transcript, letter, CV, portfolio,
        # etc.) count as heavy — a passport photo or ID scan is a document
        # upload too, but takes minutes, not days.
        is_heavy = f["type"] == "essay" or any(k in name for k in HEAVY_KEYWORDS)
        f["effort"] = "heavy" if is_heavy else "quick"
        if any(k in name for k in ("recommendation", "referee", "reference")):
            w = f"'{f['field']}' needs a third party — request it IMMEDIATELY."
            if w not in warnings:
                warnings.append(w)
    result["warnings"] = warnings
    return result


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def extract(source: str) -> dict:
    """Main entry point. Accepts a URL, a PDF path, or an image path."""
    path = Path(source)

    if path.exists():
        ext = path.suffix.lower()
        if ext == ".pdf":
            result = extract_from_pdf(source)
            if not result:
                raise RuntimeError(
                    "PDF has no extractable text (probably scanned). "
                    "Screenshot the pages and run those images instead."
                )
        elif ext in (".png", ".jpg", ".jpeg", ".webp"):
            result = extract_from_image(source)
            if not result:
                raise RuntimeError("Could not extract fields from image.")
        else:
            raise ValueError(f"Unsupported file type: {ext}")
        return tag_effort(result)

    resp = requests.get(source, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    html = resp.text

    if "docs.google.com/forms" in resp.url or "FB_PUBLIC_LOAD_DATA_" in html:
        result = extract_google_form(html)
        if result:
            return tag_effort(result)

    result = extract_html_form(html)
    if result:
        return tag_effort(result)

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer"]):
        tag.decompose()
    page_text = soup.get_text(separator="\n", strip=True)

    if len(page_text) < 200:
        raise RuntimeError(
            "Page has almost no readable text (JS-rendered or login-walled). "
            "Screenshot the form and run: python extractor.py screenshot.png"
        )

    result = extract_llm_text(page_text)
    if not result:
        raise RuntimeError("LLM could not identify application fields on this page.")
    return tag_effort(result)


# ---------------------------------------------------------------------------
# Pretty-print checklist
# ---------------------------------------------------------------------------

def print_checklist(r: dict):
    print("=" * 60)
    print(f"  {r.get('title') or 'Application Requirements'}")
    print(f"  extracted via: {r['source']}")
    if r.get("deadline"):
        print(f"  DEADLINE: {r['deadline']}")
    print("=" * 60)

    quick = [f for f in r["fields"] if f["effort"] == "quick"]
    heavy = [f for f in r["fields"] if f["effort"] == "heavy"]

    if heavy:
        print("\n  HEAVY ITEMS (start these NOW):")
        for f in heavy:
            req = "required" if f.get("required") else "optional"
            notes = f" — {f['notes']}" if f.get("notes") else ""
            print(f"   [ ] {f['field']}  ({f['type']}, {req}){notes}")

    if quick:
        print("\n  QUICK FILLS:")
        for f in quick:
            req = "required" if f.get("required") else "optional"
            print(f"   [ ] {f['field']}  ({f['type']}, {req})")

    if r.get("documents_needed"):
        print("\n  DOCUMENTS TO UPLOAD:")
        for d in r["documents_needed"]:
            print(f"   [ ] {d}")

    if r.get("warnings"):
        print("\n  ⚠ WARNINGS:")
        for w in r["warnings"]:
            print(f"   ! {w}")
    print()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    result = extract(sys.argv[1])
    print_checklist(result)
    out = Path("last_extraction.json")
    out.write_text(json.dumps(result, indent=2))
    print(f"JSON saved to {out.resolve()}")
