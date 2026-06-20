from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
from xml.sax.saxutils import escape, quoteattr


FONT_NAME = "STSong-Light"
SUPPORTED_IMAGE_SUFFIXES = {".gif", ".jpeg", ".jpg", ".png", ".webp"}


@dataclass
class RenderResult:
    local_images: int = 0
    skipped_images: int = 0


def render_markdown_pdf(
    *,
    markdown_text: str,
    source_path: Path,
    output_path: Path,
    workspace: Path,
    title: str | None = None,
) -> RenderResult:
    try:
        import mistune
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_CENTER
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        from reportlab.platypus import (
            HRFlowable,
            Image,
            ListFlowable,
            ListItem,
            Paragraph,
            Preformatted,
            SimpleDocTemplate,
            Spacer,
        )
    except ImportError as exc:
        raise RuntimeError(
            "Markdown PDF dependencies are missing. Run: "
            "python -m pip install -r requirements.txt"
        ) from exc

    pdfmetrics.registerFont(UnicodeCIDFont(FONT_NAME))
    pdfmetrics.registerFontFamily(
        FONT_NAME,
        normal=FONT_NAME,
        bold=FONT_NAME,
        italic=FONT_NAME,
        boldItalic=FONT_NAME,
    )

    stylesheet = getSampleStyleSheet()
    body = ParagraphStyle(
        "TaleClawBody",
        parent=stylesheet["BodyText"],
        fontName=FONT_NAME,
        fontSize=10.5,
        leading=17,
        textColor=colors.HexColor("#20242b"),
        spaceAfter=2 * mm,
        wordWrap="CJK",
    )
    styles = {
        "body": body,
        "title": ParagraphStyle(
            "TaleClawTitle",
            parent=body,
            alignment=TA_CENTER,
            fontSize=20,
            leading=27,
            textColor=colors.HexColor("#111827"),
            spaceAfter=6 * mm,
        ),
        "h1": ParagraphStyle(
            "TaleClawH1",
            parent=body,
            fontSize=17,
            leading=23,
            textColor=colors.HexColor("#111827"),
            spaceBefore=4 * mm,
            spaceAfter=2 * mm,
        ),
        "h2": ParagraphStyle(
            "TaleClawH2",
            parent=body,
            fontSize=14,
            leading=20,
            textColor=colors.HexColor("#18243a"),
            spaceBefore=3 * mm,
            spaceAfter=1.5 * mm,
        ),
        "h3": ParagraphStyle(
            "TaleClawH3",
            parent=body,
            fontSize=12,
            leading=18,
            textColor=colors.HexColor("#26354f"),
            spaceBefore=2 * mm,
            spaceAfter=1 * mm,
        ),
        "quote": ParagraphStyle(
            "TaleClawQuote",
            parent=body,
            leftIndent=5 * mm,
            textColor=colors.HexColor("#4b5563"),
            borderColor=colors.HexColor("#aeb9c8"),
            borderWidth=1,
            borderPadding=3,
        ),
        "code": ParagraphStyle(
            "TaleClawCode",
            parent=body,
            fontName=FONT_NAME,
            fontSize=8.5,
            leading=12,
            leftIndent=3 * mm,
            rightIndent=3 * mm,
            borderColor=colors.HexColor("#c9d1dc"),
            borderWidth=0.5,
            borderPadding=4,
            backColor=colors.HexColor("#f4f6f8"),
            spaceBefore=1 * mm,
            spaceAfter=2 * mm,
        ),
    }
    document = SimpleDocTemplate(
        str(output_path),
        pagesize=A4,
        rightMargin=18 * mm,
        leftMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=16 * mm,
    )
    result = RenderResult()

    def raw(node: dict) -> str:
        return str(node.get("raw", node.get("text", "")))

    def attrs(node: dict) -> dict:
        return node.get("attrs") or {}

    def image_source(node: dict) -> str:
        return str(node.get("src", attrs(node).get("url", ""))).strip()

    def plain_text(nodes: list[dict]) -> str:
        chunks = []
        for node in nodes:
            node_type = node.get("type")
            if node_type == "image":
                chunks.append(str(node.get("alt") or attrs(node).get("alt") or "image"))
            elif node.get("children"):
                chunks.append(plain_text(node["children"]))
            else:
                chunks.append(raw(node))
        return "".join(chunks)

    def image_alt(node: dict) -> str:
        return str(
            node.get("alt")
            or attrs(node).get("alt")
            or plain_text(node.get("children") or [])
            or "image"
        )

    def inline_markup(nodes: list[dict]) -> str:
        chunks = []
        for node in nodes:
            node_type = node.get("type")
            children = node.get("children") or []
            if node_type == "text":
                chunks.append(escape(raw(node)).replace("\n", "<br/>"))
            elif node_type == "strong":
                chunks.append(f"<b>{inline_markup(children)}</b>")
            elif node_type == "emphasis":
                chunks.append(f"<i>{inline_markup(children)}</i>")
            elif node_type == "codespan":
                chunks.append(
                    '<font color="#334155">'
                    f"{escape(raw(node))}"
                    "</font>"
                )
            elif node_type == "link":
                url = str(node.get("link", attrs(node).get("url", ""))).strip()
                label = inline_markup(children) or escape(url)
                if _safe_link(url):
                    chunks.append(f"<link href={quoteattr(url)}>{label}</link>")
                else:
                    chunks.append(label)
            elif node_type == "image":
                chunks.append(escape(f"[image: {image_alt(node)}]"))
            elif node_type in {"linebreak", "softbreak"}:
                chunks.append("<br/>" if node_type == "linebreak" else " ")
            elif children:
                chunks.append(inline_markup(children))
            else:
                chunks.append(escape(raw(node)))
        return "".join(chunks)

    def image_flowable(node: dict):
        src = image_source(node)
        alt = image_alt(node)
        if not src or urlparse(src).scheme or urlparse(src).netloc:
            result.skipped_images += 1
            return Paragraph(escape(f"[image skipped: {alt}]"), styles["body"])

        candidate = (source_path.parent / src).resolve()
        try:
            candidate.relative_to(workspace)
        except ValueError:
            result.skipped_images += 1
            return Paragraph(escape(f"[image outside workspace: {alt}]"), styles["body"])

        if (
            not candidate.is_file()
            or candidate.suffix.lower() not in SUPPORTED_IMAGE_SUFFIXES
        ):
            result.skipped_images += 1
            return Paragraph(escape(f"[image unavailable: {alt}]"), styles["body"])

        try:
            image = Image(str(candidate))
            image._restrictSize(document.width, 90 * mm)
        except Exception:
            result.skipped_images += 1
            return Paragraph(escape(f"[image unreadable: {alt}]"), styles["body"])

        result.local_images += 1
        return image

    def render_blocks(nodes: list[dict], *, quote: bool = False) -> list:
        flowables = []
        paragraph_style = styles["quote"] if quote else styles["body"]
        for node in nodes:
            node_type = node.get("type")
            children = node.get("children") or []
            if node_type == "heading":
                level = int(node.get("level", attrs(node).get("level", 3)))
                style = styles["h1" if level == 1 else "h2" if level == 2 else "h3"]
                flowables.append(Paragraph(inline_markup(children), style))
            elif node_type in {"paragraph", "block_text"}:
                if children and all(child.get("type") == "image" for child in children):
                    for image_node in children:
                        flowables.append(image_flowable(image_node))
                        flowables.append(Spacer(1, 1.5 * mm))
                else:
                    flowables.append(Paragraph(inline_markup(children), paragraph_style))
            elif node_type == "list":
                ordered = bool(node.get("ordered", attrs(node).get("ordered", False)))
                items = []
                for child in children:
                    item_flowables = render_blocks(child.get("children") or [])
                    if not item_flowables:
                        item_flowables = [Paragraph("", styles["body"])]
                    items.append(ListItem(item_flowables, leftIndent=4 * mm))
                flowables.append(
                    ListFlowable(
                        items,
                        bulletType="1" if ordered else "bullet",
                        start="1",
                        leftIndent=7 * mm,
                        bulletFontName=FONT_NAME,
                        bulletFontSize=9,
                    )
                )
                flowables.append(Spacer(1, 1 * mm))
            elif node_type == "block_quote":
                flowables.extend(render_blocks(children, quote=True))
            elif node_type == "block_code":
                code = raw(node).rstrip("\n")
                flowables.append(Preformatted(escape(code), styles["code"]))
            elif node_type == "thematic_break":
                flowables.append(
                    HRFlowable(
                        width="100%",
                        thickness=0.5,
                        color=colors.HexColor("#cbd5e1"),
                        spaceBefore=2 * mm,
                        spaceAfter=2 * mm,
                    )
                )
            elif node_type in {"newline", "blank_line"}:
                continue
            elif children:
                flowables.extend(render_blocks(children, quote=quote))
            elif raw(node).strip():
                flowables.append(Paragraph(escape(raw(node)), paragraph_style))
        return flowables

    markdown = mistune.create_markdown(renderer="ast")
    ast = markdown(markdown_text)
    story = []
    if title:
        story.append(Paragraph(escape(title), styles["title"]))
    story.extend(render_blocks(ast))
    if not story:
        story.append(Paragraph("", styles["body"]))

    document_title = title or source_path.stem

    def draw_footer(canvas, doc) -> None:
        canvas.saveState()
        canvas.setAuthor("taleclaw")
        canvas.setTitle(document_title)
        canvas.setFont(FONT_NAME, 8)
        canvas.setFillColor(colors.HexColor("#64748b"))
        canvas.drawRightString(A4[0] - 18 * mm, 8 * mm, str(doc.page))
        canvas.restoreState()

    document.build(story, onFirstPage=draw_footer, onLaterPages=draw_footer)
    return result


def _safe_link(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https", "mailto"}
