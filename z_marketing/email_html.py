"""Email bodies are stored as clean HTML from the rich editor. This keeps them safe to send and to show."""
from __future__ import annotations

import html
import re

from z_adminapi.services import clean_email_html, html_to_text

_TAG = re.compile(r"</?(p|br|ul|ol|li|strong|em|u|a)\b", re.I)
_EMPTY = re.compile(r"<p>(\s|<br\s*/?>)*</p>", re.I)
_LINK_OR_URL = re.compile(r"(<a\b.*?</a>)|(https?://[^\s<]+)", re.I | re.S)


def plain_to_html(text: str) -> str:
    """Plain text as paragraphs: a blank line starts a new one."""
    blocks = (b for b in re.split(r"\n{2,}", (text or "").replace("\r\n", "\n").strip()) if b.strip())
    return "".join("<p>" + html.escape(b.strip(), quote=False).replace("\n", "<br>") + "</p>" for b in blocks)


def as_html(body: str) -> str:
    return body if _TAG.search(body or "") else plain_to_html(body)


def drop_empty(body: str) -> str:
    """The editor marks a blank line as an empty paragraph; paragraphs are already spaced in the email."""
    return _EMPTY.sub("", body).strip()


def clean_body(raw: str) -> str:
    """What gets stored: anything outside the allow-list is gone."""
    return drop_empty(clean_email_html(as_html(raw)))


def to_text(body: str) -> str:
    """The plain-text part of the email. A link whose text is its own address is not repeated."""
    spaced = body.replace("</p>", "</p>\n")  # a blank line between paragraphs, as in the HTML part
    return re.sub(r"(\S+) \(\1\)", r"\1", html_to_text(spaced))


def styled(body: str) -> str:
    """Inline styles (mail clients drop style sheets), and bare addresses become links."""
    linked = _LINK_OR_URL.sub(lambda m: m.group(1) or f'<a href="{m.group(2)}">{m.group(2)}</a>', body)
    return (
        linked.replace("<p>", '<p style="margin:0 0 14px;">')
        .replace("<ul>", '<ul style="margin:0 0 14px;padding-left:22px;">')
        .replace("<ol>", '<ol style="margin:0 0 14px;padding-left:22px;">')
    )
