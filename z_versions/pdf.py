"""The new version plan as a PDF (fpdf2, the report PDF's fonts and colours): what goes in, what waits, and why."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.utils import timezone

from z_devices.report_pdf import FONT, INK, LINE, MUTED, ORANGE, TINT, _report_pdf_class, _text, _use_fonts
from z_main.branding import draw_pdf_logo

if TYPE_CHECKING:
    from .models import VersionPlan

SECTIONS = (
    ("in", "Goes into the next version"),
    ("review", "To review"),
    ("planned", "Already planned"),
    ("later", "Later"),
)
KIND_LABELS = {"bug": "Bug", "feature": "Feature", "improvement": "Improvement"}


def plan_filename(plan: "VersionPlan") -> str:
    return f"New-version-plan-{timezone.localtime(plan.period_end):%Y-%m-%d}.pdf"


def _sources(item: dict[str, Any]) -> str:
    return ", ".join(f"{'Feedback' if s['type'] == 'feedback' else 'Issue'} #{s['id']}" for s in item.get("sources", []))


def plan_pdf(plan: "VersionPlan") -> bytes:
    from .services import period_label

    pdf = _report_pdf_class()(format="A4")
    pdf.set_auto_page_break(auto=True, margin=16)
    pdf.set_margins(14, 14, 14)
    pdf.alias_nb_pages()
    _use_fonts(pdf)
    made = timezone.localtime()

    def footer() -> None:
        pdf.set_y(-12)
        pdf.set_font(FONT, "", 7.5)
        pdf.set_text_color(*MUTED)
        pdf.cell(0, 5, _text(f"Made by Zinwe on {made.day} {made:%b %Y, %I:%M %p} IST. Page {pdf.page_no()} of {{nb}}"), align="C")

    pdf.footer = footer  # type: ignore[method-assign]
    pdf.add_page()
    w = pdf.w - 28

    draw_pdf_logo(pdf, w / 2, height=7)
    pdf.set_text_color(*INK)
    pdf.set_font(FONT, "B", 13)
    title = f"NEW VERSION PLAN{' · ' + plan.version if plan.version else ''}"
    pdf.cell(w / 2, 10, _text(title), align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font(FONT, "", 10)
    pdf.set_text_color(*MUTED)
    counts = plan.counts or {}
    rating = f", average rating {counts['average_rating']}/10" if counts.get("average_rating") is not None else ""
    pdf.cell(w, 6, _text(f"{period_label(plan)} · {counts.get('issues', 0)} issue reports, "
                         f"{counts.get('feedback', 0)} feedback{rating}"), new_x="LMARGIN", new_y="NEXT")
    pdf.set_draw_color(*ORANGE)
    pdf.set_line_width(0.6)
    pdf.line(14, pdf.get_y() + 2, 14 + w, pdf.get_y() + 2)
    pdf.set_line_width(0.2)
    pdf.ln(6)

    pdf.set_text_color(*INK)
    pdf.set_font(FONT, "B", 12)
    pdf.multi_cell(w, 6, _text(plan.headline or "New version plan"), new_x="LMARGIN", new_y="NEXT")
    if plan.summary:
        pdf.ln(1)
        pdf.set_font(FONT, "", 10)
        pdf.multi_cell(w, 5, _text(plan.summary), new_x="LMARGIN", new_y="NEXT")
    if plan.error:
        pdf.ln(2)
        pdf.set_fill_color(*TINT)
        pdf.set_font(FONT, "", 9)
        pdf.multi_cell(w, 5, _text(plan.error), fill=True, padding=2, new_x="LMARGIN", new_y="NEXT")

    for decision, heading in SECTIONS:
        items = [i for i in plan.items if i.get("decision") == decision]
        if not items:
            continue
        pdf.ln(5)
        pdf.set_font(FONT, "B", 11)
        pdf.set_text_color(*INK)
        pdf.cell(w, 7, _text(f"{heading} ({len(items)})"), new_x="LMARGIN", new_y="NEXT")
        pdf.set_draw_color(*LINE)
        for n, item in enumerate(items, 1):
            pdf.line(14, pdf.get_y(), 14 + w, pdf.get_y())
            pdf.ln(1.5)
            pdf.set_font(FONT, "B", 10)
            pdf.set_text_color(*INK)
            pdf.multi_cell(w, 5, _text(f"{n}. {item['title']}  ·  {KIND_LABELS.get(item['kind'], item['kind'])}"),
                           new_x="LMARGIN", new_y="NEXT")
            pdf.set_font(FONT, "", 9.5)
            if item.get("why"):
                pdf.multi_cell(w, 4.8, _text(item["why"]), new_x="LMARGIN", new_y="NEXT")
            if item.get("sources"):
                pdf.set_text_color(*MUTED)
                pdf.set_font(FONT, "", 8.5)
                pdf.multi_cell(w, 4.5, _text(f"From {_sources(item)}"), new_x="LMARGIN", new_y="NEXT")
            pdf.ln(1.5)

    if not plan.items:
        pdf.ln(4)
        pdf.set_font(FONT, "", 10)
        pdf.set_text_color(*MUTED)
        pdf.multi_cell(w, 5, _text("Nothing to plan from this week."), new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())
