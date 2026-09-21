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

#: Shown by the script when a search leaves no card standing; hidden without JS.
NO_RESULTS = '<p class="no-results" hidden>No artifacts match your search.</p>'

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
    main.append(NO_RESULTS)
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
    main.append(NO_RESULTS)
    main.append("</main>")

    title = f"{shelf.title} · lesvi"
    return _document(title=title, index=index, active=shelf.name, main="".join(main))


def render_login(*, error: bool = False) -> str:
    """The ``/login`` page: a plain form that posts the shared token.

    Reads without JavaScript; the theme boot keeps a previously chosen theme.
    """
    message = (
        '<p class="login-error" role="alert">That token is not correct. '
        "Try again.</p>"
        if error
        else ""
    )
    main = (
        '<main id="main" class="wrap login">'
        '<h1 class="login-title">lesvi</h1>'
        '<p class="login-lede">Enter the access token to open the library.</p>'
        f"{message}"
        '<form class="login-form" method="post" action="/login">'
        '<label class="login-label" for="token">Access token</label>'
        '<input class="login-input" id="token" name="token" type="password"'
        ' autocomplete="current-password" spellcheck="false" required autofocus>'
        '<button class="login-button" type="submit">Sign in</button>'
        "</form></main>"
    )
    return _document(
        title="Sign in · lesvi", index=None, active=None, main=main, script=False
    )


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
        '<li class="card-item">'
        f'<article class="card" data-search="{_search_text(artifact)}">'
        '<p class="card-meta">'
        f'<span class="card-shelf">{_esc(artifact.shelf)}</span>'
        f"{number}{_pin_toggle(artifact)}</p>"
        f'<h3 class="card-title"><a class="card-link" href="{_esc(artifact.url)}">'
        f"{_esc(artifact.title)}</a></h3>"
        f"{description}{tags}"
        f'<time class="card-time" datetime="{moment}" title="{moment}">'
        f"{_esc(relative_time(artifact.mtime, now))}</time>"
        "</article></li>"
    )


def _search_text(artifact: Artifact) -> str:
    """The card text client-side search matches on: title, description, tags."""
    return _esc(" ".join((artifact.title, artifact.description, *artifact.tags)))


def _pin_toggle(artifact: Artifact) -> str:
    """The per-card pin button; hidden until the script wires it up."""
    action = "Unpin" if artifact.pinned else "Pin"
    return (
        '<button class="pin-toggle" type="button" hidden'
        f' data-shelf="{_esc(artifact.shelf)}"'
        f' data-path="{_esc(artifact.path)}"'
        f' aria-pressed="{"true" if artifact.pinned else "false"}"'
        f' aria-label="{_esc(f"{action} {artifact.title}")}">'
        f'<span class="pin-glyph" aria-hidden="true">'
        f"{'★' if artifact.pinned else '☆'}</span></button>"
    )


def _cards(artifacts: tuple[Artifact, ...], now: datetime | None) -> str:
    return (
        '<ul class="cards">'
        + "".join(_card(artifact, now) for artifact in artifacts)
        + "</ul>"
    )


def _document(
    *,
    title: str,
    index: Index | None,
    active: str | None,
    main: str,
    script: bool = True,
) -> str:
    header = f"{_header(index, active)}\n" if index is not None else ""
    script_tag = f'<script src="{_SCRIPT}" defer></script>\n' if script else ""
    return (
        "<!doctype html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{_esc(title)}</title>\n"
        f"{_THEME_BOOT}\n"
        f'<link rel="stylesheet" href="{_STYLESHEET}">\n'
        f"{script_tag}"
        "</head>\n"
        "<body>\n"
        '<a class="skip-link" href="#main">Skip to content</a>\n'
        f"{header}"
        f"{main}\n"
        '<p class="pin-status visually-hidden" id="pin-status" role="status"'
        ' aria-live="polite"></p>\n'
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
        f"{_search()}"
        "</header>"
    )


def _search() -> str:
    """The client-side search box; hidden until the script takes it over."""
    return (
        '<form class="search" role="search" method="get" action="" hidden>'
        '<label class="visually-hidden" for="search-input">Search artifacts</label>'
        '<input class="search-input" id="search-input" type="search" name="q"'
        ' placeholder="Search title, description, tags" autocomplete="off"'
        ' spellcheck="false">'
        '<p class="search-count" id="search-count" role="status"'
        ' aria-live="polite"></p></form>'
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
    "render_login",
    "render_shelf",
]
