"""Document-tree parser for structured source ingestion (OHM-document-tree).

Takes HTML or Markdown text and extracts a heading hierarchy as a tree of
sections, paragraphs, tables, and lists. Each node has a stable id, title,
text, level, parent_id, and node_type.

Reference: MoDora (arXiv 2602.23061) — documents are trees, not flat chunks.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from bs4 import BeautifulSoup


@dataclass
class DocumentNode:
    """One node in a document tree."""

    id: str
    title: str
    text: str
    level: int
    parent_id: str | None
    node_type: str
    position: int = 0
    children: list["DocumentNode"] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "text": self.text,
            "level": self.level,
            "parent_id": self.parent_id,
            "node_type": self.node_type,
            "position": self.position,
            "children": [c.to_dict() for c in self.children],
            "metadata": dict(self.metadata),
        }


@dataclass
class DocumentTree:
    """Root of a parsed document tree."""

    source_id: str
    title: str
    content_type: str  # 'html' or 'markdown'
    root: DocumentNode
    flat: list[DocumentNode] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "title": self.title,
            "content_type": self.content_type,
            "root": self.root.to_dict(),
        }

    def iter_flat(self) -> list[DocumentNode]:
        """Return all nodes in pre-order including root."""
        out: list[DocumentNode] = []

        def _walk(node: DocumentNode) -> None:
            out.append(node)
            for child in node.children:
                _walk(child)

        _walk(self.root)
        return out


# ── HTML parsing ────────────────────────────────────────────────────────────


def _looks_like_html(text: str) -> bool:
    """Heuristic: does the text contain HTML tags?"""
    return bool(re.search(r"<[^>]+>", text.strip()))


def _extract_text(element) -> str:  # type: ignore[no-untyped-def]
    """Extract clean text from a BeautifulSoup element."""
    text = element.get_text(separator="\n", strip=True)
    # Collapse multiple blank lines
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def _heading_level(tag_name: str) -> int | None:
    """Return 1-6 for h1-h6, None otherwise."""
    if tag_name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
        return int(tag_name[1])
    return None


def _paragraph_like(tag_name: str) -> bool:
    return tag_name in {"p", "blockquote", "pre", "div"}


def _list_like(tag_name: str) -> bool:
    return tag_name in {"ul", "ol"}


def _table_like(tag_name: str) -> bool:
    return tag_name in {"table"}


def parse_html_to_tree(
    html: str,
    source_id: str | None = None,
    default_title: str = "Untitled Document",
) -> DocumentTree:
    """Parse HTML into a DocumentTree.

    The source node itself becomes the root (level=0). Children are sections
    and leaf blocks (paragraphs, tables, lists) attached to their nearest
    heading ancestor.
    """
    soup = BeautifulSoup(html, "html.parser")

    # Try to find a document title
    title = default_title
    h1 = soup.find("h1")
    if h1:
        title = h1.get_text(strip=True)
    elif soup.title and soup.title.string:
        title = soup.title.string.strip()

    source_id = source_id or f"source-{uuid.uuid4().hex[:8]}"
    root = DocumentNode(
        id=source_id,
        title=title,
        text=title,
        level=0,
        parent_id=None,
        node_type="source",
        position=0,
    )
    tree = DocumentTree(source_id=source_id, title=title, content_type="html", root=root)

    body = soup.body if soup.body else soup
    top_level = [child for child in body.children if getattr(child, "name", None)]

    heading_stack: list[DocumentNode] = [root]
    position = 0

    def _current_parent() -> DocumentNode:
        return heading_stack[-1]

    def _close_to_level(level: int) -> None:
        while len(heading_stack) > 1 and heading_stack[-1].level >= level:
            heading_stack.pop()

    def _attach(node: DocumentNode) -> None:
        parent = _current_parent()
        node.parent_id = parent.id
        node.position = position
        parent.children.append(node)

    for element in top_level:
        tag = element.name
        if tag is None:
            continue

        level = _heading_level(tag)
        if level is not None:
            _close_to_level(level)
            heading = DocumentNode(
                id=f"{source_id}-sec-{uuid.uuid4().hex[:8]}",
                title=element.get_text(strip=True),
                text=element.get_text(strip=True),
                level=level,
                parent_id=None,
                node_type="section",
                position=position,
            )
            _attach(heading)
            heading_stack.append(heading)
            position += 1
            continue

        if _paragraph_like(tag):
            text = _extract_text(element)
            if text:
                node = DocumentNode(
                    id=f"{source_id}-para-{uuid.uuid4().hex[:8]}",
                    title="",
                    text=text,
                    level=_current_parent().level + 1,
                    parent_id=None,
                    node_type="paragraph",
                    position=position,
                )
                _attach(node)
                position += 1
            continue

        if _list_like(tag):
            text = _extract_text(element)
            if text:
                node = DocumentNode(
                    id=f"{source_id}-list-{uuid.uuid4().hex[:8]}",
                    title="",
                    text=text,
                    level=_current_parent().level + 1,
                    parent_id=None,
                    node_type="list",
                    position=position,
                )
                _attach(node)
                position += 1
            continue

        if _table_like(tag):
            text = _extract_text(element)
            if text:
                node = DocumentNode(
                    id=f"{source_id}-table-{uuid.uuid4().hex[:8]}",
                    title="",
                    text=text,
                    level=_current_parent().level + 1,
                    parent_id=None,
                    node_type="table",
                    position=position,
                )
                _attach(node)
                position += 1
            continue

        # Unknown block: if it has text, treat as paragraph under current parent
        text = _extract_text(element)
        if text:
            node = DocumentNode(
                id=f"{source_id}-block-{uuid.uuid4().hex[:8]}",
                title="",
                text=text,
                level=_current_parent().level + 1,
                parent_id=None,
                node_type="block",
                position=position,
            )
            _attach(node)
            position += 1

    tree.flat = tree.iter_flat()
    return tree


# ── Markdown parsing ────────────────────────────────────────────────────────


def parse_markdown_to_tree(
    markdown: str,
    source_id: str | None = None,
    default_title: str = "Untitled Document",
) -> DocumentTree:
    """Parse Markdown into a DocumentTree.

    Uses the markdown parser to convert to HTML, then reuses the HTML tree
    parser. Headings become sections; paragraphs/lists/tables become leaf
    nodes under their nearest heading ancestor.
    """
    import markdown as md

    html = md.markdown(markdown, extensions=["tables", "fenced_code"])
    source_id = source_id or f"source-{uuid.uuid4().hex[:8]}"

    # The HTML parser will pick up the first <h1> as title. We preserve the
    # default title fallback in case the markdown has no h1.
    tree = parse_html_to_tree(html, source_id=source_id, default_title=default_title)
    tree.content_type = "markdown"
    return tree


# ── Unified entry point ─────────────────────────────────────────────────────


def parse_document(
    text: str,
    source_id: str | None = None,
    title: str | None = None,
    content_type: str | None = None,
) -> DocumentTree:
    """Parse HTML or Markdown text into a DocumentTree.

    Args:
        text: The document text.
        source_id: Optional stable id for the source node/root.
        title: Optional document title (fallback if no h1/title found).
        content_type: 'html' or 'markdown'. If None, auto-detect.

    Returns:
        A DocumentTree rooted at the source node.
    """
    if not text or not text.strip():
        source_id = source_id or f"source-{uuid.uuid4().hex[:8]}"
        root = DocumentNode(
            id=source_id,
            title=title or "Untitled Document",
            text="",
            level=0,
            parent_id=None,
            node_type="source",
        )
        tree = DocumentTree(
            source_id=source_id,
            title=title or "Untitled Document",
            content_type=content_type or "markdown",
            root=root,
            flat=[root],
        )
        return tree

    is_html = content_type == "html" or (content_type is None and _looks_like_html(text))
    if is_html:
        return parse_html_to_tree(text, source_id=source_id, default_title=title or "Untitled Document")
    return parse_markdown_to_tree(text, source_id=source_id, default_title=title or "Untitled Document")


# ── Semantic matching ───────────────────────────────────────────────────────


def keyword_overlap(text: str, labels: list[str], stop_words: set[str] | None = None) -> list[tuple[str, float]]:
    """Score how much *text* overlaps with each label via keyword matching.

    Returns (label, score) pairs sorted by descending score. Stop words are
    ignored. Score is Jaccard-ish: |shared tokens| / |text tokens|.
    """
    if not text:
        return []

    stop_words = stop_words or _DEFAULT_STOP_WORDS
    text_tokens = _tokenize(text, stop_words)
    if not text_tokens:
        return []

    scored: list[tuple[str, float]] = []
    for label in labels:
        label_tokens = _tokenize(label, stop_words)
        if not label_tokens:
            continue
        shared = len(text_tokens & label_tokens)
        score = shared / max(len(text_tokens), len(label_tokens), 1)
        if score > 0:
            scored.append((label, score))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored


_DEFAULT_STOP_WORDS: set[str] = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "must", "can", "shall", "of", "in", "on",
    "at", "to", "for", "with", "by", "from", "up", "about", "into", "through",
    "during", "before", "after", "above", "below", "between", "among", "and",
    "or", "but", "so", "yet", "that", "this", "these", "those", "it", "its",
    "they", "them", "their", "we", "our", "you", "your", "he", "she", "his",
    "her", "i", "me", "my", "what", "which", "who", "when", "where", "why",
    "how", "all", "any", "both", "each", "few", "more", "most", "other",
    "some", "such", "no", "nor", "not", "only", "own", "same", "than", "too",
    "very", "just", "as",
}


def _tokenize(text: str, stop_words: set[str]) -> set[str]:
    """Normalize and tokenize text into a set of non-stop-word tokens."""
    tokens: set[str] = set()
    for raw in re.findall(r"[A-Za-z0-9]+", text.lower()):
        if raw not in stop_words and len(raw) > 2:
            tokens.add(raw)
    return tokens
