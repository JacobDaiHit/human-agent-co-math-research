"""Primary-source mathematics papers, returned as material rather than instructions."""

import io
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree

import httpx
from pypdf import PdfReader
from pypdf.errors import PdfReadError

ARXIV_API = "https://export.arxiv.org/api/query"
MAX_DOCUMENT_BYTES = 32 * 1024 * 1024
ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV_ID = re.compile(r"(?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?$")


class PaperText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0
        self.math_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag == "math":
            latex = dict(attrs).get("alttext")
            if latex:
                self.parts.append(" $" + latex + "$ ")
                self.math_depth += 1
        if tag in {"script", "style"}:
            self.hidden += 1
        if tag in {"p", "div", "section", "h1", "h2", "h3", "li", "br", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag == "math":
            self.math_depth = max(0, self.math_depth - 1)
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        if tag in {"p", "section", "li", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden and not self.math_depth:
            self.parts.append(data)

    def text(self):
        return re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", "".join(self.parts)).strip()


def _download(client, url, *, params=None):
    for _ in range(4):
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in {"arxiv.org", "export.arxiv.org", "www.arxiv.org"}:
            raise ValueError("Only primary arXiv sources are available through this tool")
        with client.stream("GET", url, params=params, follow_redirects=False) as response:
            if response.is_redirect:
                url, params = urljoin(url, response.headers["location"]), None
                continue
            response.raise_for_status()
            parts, size = [], 0
            for part in response.iter_bytes():
                size += len(part)
                if size > MAX_DOCUMENT_BYTES:
                    raise ValueError("This paper exceeds the download size available to the tool")
                parts.append(part)
            return b"".join(parts), url, response.headers.get("content-type", "")
    raise ValueError("Too many redirects when retrieving the paper")


def search_papers(query, limit=5, *, client=None):
    own = client is None
    client = client or httpx.Client(timeout=60, trust_env=False)
    try:
        raw, _, _ = _download(client, ARXIV_API, params={
            "search_query": query, "start": 0, "max_results": limit,
            "sortBy": "relevance", "sortOrder": "descending",
        })
        feed = ElementTree.fromstring(raw)
        papers = []
        for entry in feed.findall(ATOM + "entry"):
            identifier = entry.findtext(ATOM + "id", "")
            if identifier.startswith("http://"):
                identifier = "https://" + identifier.removeprefix("http://")
            papers.append({"url": identifier, "title": " ".join(entry.findtext(ATOM + "title", "").split()),
                "authors": [author.findtext(ATOM + "name", "") for author in entry.findall(ATOM + "author")],
                "abstract": entry.findtext(ATOM + "summary", "").strip(),
                "published": entry.findtext(ATOM + "published", ""),
                "updated": entry.findtext(ATOM + "updated", "")})
        return {"source": "arXiv", "query": query, "papers": papers}
    except (httpx.HTTPError, ValueError, ElementTree.ParseError) as error:
        return {"error": "literature_unavailable", "detail": str(error)[:1000]}
    finally:
        if own:
            client.close()


def read_paper(url, *, client=None):
    parsed = urlparse(url)
    identifier = re.sub(r"^/(?:abs|html|pdf)/", "", parsed.path).removesuffix(".pdf")
    if parsed.scheme != "https" or parsed.hostname not in {"arxiv.org", "export.arxiv.org", "www.arxiv.org"} or not ARXIV_ID.fullmatch(identifier):
        return {"error": "invalid_paper_url", "detail": "Use an arXiv abstract, HTML or PDF link"}
    own = client is None
    client = client or httpx.Client(timeout=120, trust_env=False)
    try:
        try:
            raw, source, _ = _download(client, "https://arxiv.org/html/" + identifier)
            parser = PaperText()
            parser.feed(raw.decode("utf-8", "replace"))
            text = parser.text()
            if text:
                return {"source_url": source, "format": "html", "body": text}
        except httpx.HTTPStatusError as error:
            if error.response.status_code not in {404, 400}:
                raise
        raw, source, _ = _download(client, "https://arxiv.org/pdf/" + identifier)
        reader = PdfReader(io.BytesIO(raw))
        text = "\n\n".join("Page " + str(index + 1) + "\n" + (page.extract_text() or "")
                           for index, page in enumerate(reader.pages))
        return {"source_url": source, "format": "pdf_text", "body": text,
                "note": "PDF text extraction can lose mathematical layout; this is source material, not a verified transcription."}
    except (httpx.HTTPError, ValueError, PdfReadError) as error:
        return {"error": "literature_unavailable", "detail": str(error)[:1000]}
    finally:
        if own:
            client.close()
