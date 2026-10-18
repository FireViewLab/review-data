"""상세 소개 HTML에서 텍스트와 이미지 주소만 추출한다."""

from collections.abc import Iterable
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup


def parse_description(
    html: str | None, base_url: str, extra_images: Iterable[str] = ()
) -> tuple[str | None, list[str]]:
    soup = BeautifulSoup(html if isinstance(html, str) else "", "lxml")
    for tag in soup.select("script, style, noscript"):
        tag.decompose()
    text = " ".join(soup.get_text(" ", strip=True).split())[:20_000] or None
    sources = [image.get("data-src") or image.get("src") for image in soup.select("img")]
    images = []
    seen = set()
    for source in [*sources, *extra_images]:
        if not isinstance(source, str) or not source.strip() or source.strip().startswith("#"):
            continue
        try:
            url = urlsplit(urljoin(base_url, source.strip()))
            if (
                url.scheme not in {"http", "https"}
                or not url.hostname
                or url.username
                or url.password
            ):
                continue
            normalized = urlunsplit(("https", url.netloc, url.path, url.query, ""))
        except ValueError:
            continue
        if normalized in seen:
            continue
        images.append(normalized)
        seen.add(normalized)
        if len(images) == 100:
            break
    return text, images
