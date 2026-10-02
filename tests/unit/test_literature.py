"""Source retrieval is tested locally; no model or outside network is used."""

import io

import httpx
from mathagent.tools.literature import read_paper, search_papers
from pypdf import PdfWriter


def test_search_preserves_source_metadata_and_never_invents_papers():
    xml = '''<feed xmlns="http://www.w3.org/2005/Atom"><entry>
    <id>http://arxiv.org/abs/2601.12345</id><title> A Mathematical Paper </title>
    <author><name>A. Researcher</name></author><summary>The actual abstract.</summary>
    <published>2026-01-01</published><updated>2026-01-02</updated>
    </entry></feed>'''

    def respond(request):
        assert request.url.host == "export.arxiv.org"
        assert request.url.params["search_query"] == "cat:math.NT"
        return httpx.Response(200, text=xml)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        result = search_papers("cat:math.NT", client=client)
    assert result["papers"][0] == {"url": "https://arxiv.org/abs/2601.12345",
        "title": "A Mathematical Paper", "authors": ["A. Researcher"],
        "abstract": "The actual abstract.", "published": "2026-01-01", "updated": "2026-01-02"}


def test_html_reading_preserves_latex_and_treats_paper_text_as_source():
    body = '<h1>A theorem</h1><p>For all <math alttext="x^2"><mi>x</mi><mn>2</mn></math>, the relation holds.</p><script>not paper text</script>'
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, text=body))) as client:
        result = read_paper("https://arxiv.org/abs/2601.12345", client=client)
    assert result["format"] == "html" and "$x^2$" in result["body"]
    assert "not paper text" not in result["body"]
    assert "verified" not in result


def test_no_html_falls_back_to_primary_pdf_with_extraction_limit_disclosed():
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    pdf = io.BytesIO()
    writer.write(pdf)

    def respond(request):
        return httpx.Response(404) if "/html/" in request.url.path else httpx.Response(200, content=pdf.getvalue())

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        result = read_paper("https://arxiv.org/pdf/2601.12345.pdf", client=client)
    assert result["format"] == "pdf_text" and result["source_url"] == "https://arxiv.org/pdf/2601.12345"
    assert "lose mathematical layout" in result["note"]


def test_paper_tools_do_not_fetch_host_services_or_follow_off_source_redirects():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": "http://127.0.0.1:8000/private"})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        assert read_paper("http://127.0.0.1:8000", client=client)["error"] == "invalid_paper_url"
        assert requests == []
        assert read_paper("https://arxiv.org/abs/2601.12345", client=client)["error"] == "literature_unavailable"
        assert len(requests) == 1
