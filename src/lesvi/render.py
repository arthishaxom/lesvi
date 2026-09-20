"""Server-rendered pages: the cross-shelf home feed and per-shelf pages.

Pages are plain HTML that reads without JavaScript: every card is an ``<a>`` to
the raw artifact, relative times are precomputed labels, and the theme follows
``prefers-color-scheme`` until the reader picks a side (persisted in
``localStorage`` and applied by a tiny inline boot script before first paint).
"""

from __future__ import annotations

import html
from datetime import datetime
from urllib.parse import quote

from lesvi.index import Index, Shelf
from lesvi.scanner import Artifact

HOME_PAGE_SIZE = 50
MAX_HOME_LIMIT = 500
RECENT_SORT = "recent"

_STYLESHEET = "/assets/app.css"
_SCRIPT = "/assets/app.js"
_THEME_BOOT = (
    '<script>(function(){try{var theme=localStorage.getItem("lesvi-theme");'
    'if(theme==="dark"||theme==="light"){document.documentElement.dataset.theme=theme}'
    "}catch(error){}})();</script>"
)

_MINUTE = 60
_HOUR = 60 * _MINUTE
_DAY = 24 * _HOUR
_WEEK = 7 * _DAY
_MONTH = 30 * _DAY
_YEAR = 365 * _DAY


def relative_time(mtime: str, now: datetime | None = None) -> str:
    """A short label like ``2h ago`` for an ISO mtime; empty when unparsable."""
    try:
        moment = datetime.fromisoformat(mtime)
    except ValueError:
        return ""
    if moment.tzinfo is None:
        moment = moment.astimezone()
    if now is not None and now.tzinfo is None:
        now = now.astimezone()
    reference = now if now is not None else datetime.now(tz=moment.tzinfo)
    seconds = (reference - moment).total_seconds()
    if seconds < _MINUTE:
        return "just now"
    if seconds < _HOUR:
        return f"{int(seconds // _MINUTE)}m ago"
    if seconds < _DAY:
        return f"{int(seconds // _HOUR)}h ago"
    if seconds < _WEEK:
        return f"{int(seconds // _DAY)}d ago"
    if seconds < _MONTH:
        return f"{int(seconds // _WEEK)}w ago"
    if seconds < _YEAR:
        return f"{int(seconds // _MONTH)}mo ago"
    return f"{int(seconds // _YEAR)}y ago"


def render_home(
    index: Index, *, limit: int = HOME_PAGE_SIZE, now: datetime | None = None
) -> str:
    """The ``/`` page: pinned cards, then the cross-shelf recency feed.

    Pinned cards move out of the feed so nothing is shown twice; the feed
    reveals ``limit`` cards at a time, and the "Show more" link extends it.
    """
    pinned = index.pinned()
    pinned_urls = {artifact.url for artifact in pinned}
    feed_total = len(index.visible_artifacts) - len(pinned)
    recent = tuple(
        artifact
        for artifact in index.recent(limit=limit + len(pinned))
        if artifact.url not in pinned_urls
    )[:limit]

    main: list[str] = [
        '<main id="main" class="wrap">',
        '<h1 class="visually-hidden">lesvi</h1>',
    ]
    if pinned:
        main.append(
            '<section class="pinned" aria-labelledby="pinned-heading">'
            '<h2 id="pinned-heading">Pinned</h2>'
            f"{_cards(pinned, now)}"
            "</section>"
        )
    if recent:
        main.append(
            '<section class="feed" aria-labelledby="recent-heading">'
            '<h2 id="recent-heading">Recently updated</h2>'
            f"{_cards(recent, now)}"
        )
        if len(recent) < feed_total:
            next_limit = min(limit * 2, feed_total, MAX_HOME_LIMIT)
            if next_limit > limit:
                main.append(
                    '<p class="show-more">'
                    f'<a class="show-more" href="/?limit={next_limit}">'
                    "Show more</a></p>"
                )
        main.append("</section>")
    elif not pinned:
        main.append(
            '<section class="feed" aria-labelledby="recent-heading">'
            '<h2 id="recent-heading">Recently updated</h2>'
            '<p class="empty">Nothing to show yet. Register a shelf with '
            "<code>lesvi add &lt;path&gt;</code>.</p>"
            "</section>"
        )
    main.append("</main>")

    return _document(title="lesvi", index=index, active=None, main="".join(main))


def render_shelf(
    index: Index,
    shelf_name: str,
    *,
    sort: str = "",
    now: datetime | None = None,
) -> str:
    """The ``/s/<shelf>/`` page: sections per visible category.

    ``sort="recent"`` orders each section by mtime instead of the default
    curriculum order.
    """
    shelf = index.shelves[shelf_name]
    recent = sort == RECENT_SORT
    ordered = shelf.recency if recent else shelf.curriculum
    main: list[str] = [
        '<main id="main" class="wrap">',
        f'<h1 class="shelf-title">{_esc(shelf.title)}</h1>',
        f'<p class="shelf-counts">{_shelf_counts(shelf)}</p>',
        _sort_toggle(shelf, recent=recent),
    ]

    rendered_any = False
    for category in shelf.categories:
        if category.hidden:
            continue
        records = tuple(
            artifact for artifact in ordered if artifact.category_key == category.key
        )
        if not records:
            continue
        rendered_any = True
        section_id = f"category-{category.key}"
        main.append(
            f'<section class="category" aria-labelledby="{_esc(section_id)}">'
            f'<h2 id="{_esc(section_id)}">{_esc(category.label)}</h2>'
            f"{_cards(records, now)}"
            "</section>"
        )
    if not rendered_any:
        main.append('<p class="empty">No visible artifacts in this shelf yet.</p>')
    main.append("</main>")

    title = f"{shelf.title} · lesvi"
    return _document(title=title, index=index, active=shelf.name, main="".join(main))


def _sort_toggle(shelf: Shelf, *, recent: bool) -> str:
    base = _shelf_url(shelf.name)
    curriculum_current = "" if recent else ' aria-current="page"'
    recent_current = ' aria-current="page"' if recent else ""
    return (
        '<nav class="sort-toggle" aria-label="Order">'
        f'<a class="sort-link" href="{base}"{curriculum_current}>Curriculum</a>'
        f'<a class="sort-link" href="{base}?sort={RECENT_SORT}"{recent_current}>'
        "Recent</a></nav>"
    )


def _shelf_counts(shelf: Shelf) -> str:
    visible = [category for category in shelf.categories if not category.hidden]
    total = sum(category.count for category in visible)
    parts = [f"{total} {_plural(total, 'artifact', 'artifacts')}"]
    parts.extend(
        f"{_esc(category.label)} {category.count}"
        for category in visible
        if category.count
    )
    return " · ".join(parts)


def _card(artifact: Artifact, now: datetime | None) -> str:
    number = (
        f'<span class="card-number">{artifact.number}</span>'
        if artifact.number is not None
        else ""
    )
    description = (
        f'<p class="card-description">{_esc(artifact.description)}</p>'
        if artifact.description
        else ""
    )
    tags = ""
    if artifact.tags:
        items = "".join(
            f'<li class="card-tag">{_esc(tag)}</li>' for tag in artifact.tags[:3]
        )
        tags = f'<ul class="card-tags">{items}</ul>'
    moment = _esc(artifact.mtime)
    return (
        '<li class="card-item"><article class="card">'
        '<p class="card-meta">'
        f'<span class="card-shelf">{_esc(artifact.shelf)}</span>'
        f"{number}</p>"
        f'<h3 class="card-title"><a class="card-link" href="{_esc(artifact.url)}">'
        f"{_esc(artifact.title)}</a></h3>"
        f"{description}{tags}"
        f'<time class="card-time" datetime="{moment}" title="{moment}">'
        f"{_esc(relative_time(artifact.mtime, now))}</time>"
        "</article></li>"
    )


def _cards(artifacts: tuple[Artifact, ...], now: datetime | None) -> str:
    return (
        '<ul class="cards">'
        + "".join(_card(artifact, now) for artifact in artifacts)
        + "</ul>"
    )


def _document(*, title: str, index: Index, active: str | None, main: str) -> str:
    return (
        "<!doctype html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{_esc(title)}</title>\n"
        f"{_THEME_BOOT}\n"
        f'<link rel="stylesheet" href="{_STYLESHEET}">\n'
        f'<script src="{_SCRIPT}" defer></script>\n'
        "</head>\n"
        "<body>\n"
        '<a class="skip-link" href="#main">Skip to content</a>\n'
        f"{_header(index, active)}\n"
        f"{main}\n"
        "</body>\n"
        "</html>\n"
    )


def _header(index: Index, active: str | None) -> str:
    links = []
    for name, shelf in index.shelves.items():
        current = ' aria-current="page"' if name == active else ""
        links.append(
            f'<a class="shelf-link" href="{_shelf_url(name)}"{current}>'
            f"{_esc(shelf.title)}</a>"
        )
    nav = (
        f'<nav class="shelf-nav" aria-label="Shelves">{"".join(links)}</nav>'
        if links
        else ""
    )
    return (
        '<header class="site-header">'
        '<div class="header-row">'
        '<a class="brand" href="/">lesvi</a>'
        f'<p class="site-counts">{_counts_text(index)}</p>'
        '<button class="theme-toggle" type="button" hidden>Dark mode</button>'
        "</div>"
        f"{nav}"
        "</header>"
    )


def _counts_text(index: Index) -> str:
    shelves = len(index.shelves)
    artifacts = len(index.visible_artifacts)
    return (
        f"{shelves} {_plural(shelves, 'shelf', 'shelves')} · "
        f"{artifacts} {_plural(artifacts, 'artifact', 'artifacts')}"
    )


def _plural(count: int, singular: str, plural: str) -> str:
    return singular if count == 1 else plural


def _shelf_url(name: str) -> str:
    """The shelf page URL for *name*; names are slugs but configs are hand-edited."""
    return f"/s/{quote(name, safe='')}/"


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


__all__ = [
    "HOME_PAGE_SIZE",
    "MAX_HOME_LIMIT",
    "RECENT_SORT",
    "relative_time",
    "render_home",
    "render_shelf",
]
