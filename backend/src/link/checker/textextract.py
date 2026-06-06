"""Extrator de links a partir de campos RichText.

Itera sobre os schemas de um conteúdo (incluindo behaviours) e recolhe
todos os hrefs dentro de <a> em cada campo RichText.

Suporta links internos do Plone no formato `../resolveuid/UID` (e variantes
`/resolveuid/UID`, `resolveuid/UID`, absoluto `http(s)://.../resolveuid/UID`):
tenta resolver o UID para o URL real do conteúdo e usa esse URL como chave
de dedup. Se o UID não existir (conteúdo apagado), marca o link como
"orfao" para aparecer nos resultados como quebrado.
"""
from __future__ import annotations

import logging
import re
from typing import Iterable
from urllib.parse import urljoin

import lxml.html
import lxml.etree
from plone import api
from plone.app.textfield import RichText
from plone.dexterity.utils import iterSchemata
from zope.schema import getFieldsInOrder

logger = logging.getLogger(__name__)

# Protocolos que ignoramos (não são URLs web testáveis)
_SKIP_RE = re.compile(r"^(?:mailto:|tel:|sms:|javascript:|data:|#)", re.I)

# Detecta links internos do Plone no formato resolveuid. Apanha:
#   ../resolveuid/UID       (relativo, o mais comum)
#   ./resolveuid/UID        (relativo)
#   /resolveuid/UID         (absoluto no site)
#   resolveuid/UID          (sem prefixo)
#   http(s)://host/.../resolveuid/UID  (URL absoluta)
# O uid captura caracteres alfanuméricos, _ e - (32 chars hex é o mais comum)
RESOLVEUID_RE = re.compile(
    r"(?P<sep>/|^)resolveuid/(?P<uid>[A-Za-z0-9_-]{6,})(?:[?#].*)?$",
    re.I,
)


def _is_testable_url(href: str) -> bool:
    """Filtra hrefs que não vale a pena testar (mailto, tel, âncoras...)."""
    if not href:
        return False
    href = href.strip()
    if not href:
        return False
    if _SKIP_RE.match(href):
        return False
    return True


def get_rich_text_fields(context) -> list[tuple[str, object]]:
    """Devolve [(fieldname, schema), ...] para todos os campos RichText."""
    fields: list[tuple[str, object]] = []
    for schema in iterSchemata(context):
        try:
            ordered = getFieldsInOrder(schema)
        except TypeError:
            continue
        for name, field in ordered:
            if isinstance(field, RichText):
                fields.append((name, schema))
    return fields


def extract_links_from_html(html: str) -> list[str]:
    """Extrai todos os hrefs de <a> num HTML. Tolerante a HTML malformado."""
    if not html or not html.strip():
        return []
    try:
        root = lxml.html.fragment_fromstring(html, create_parent="div")
    except (lxml.etree.ParserError, lxml.etree.XMLSyntaxError, ValueError):
        # Fallback: regex simples
        return re.findall(r'<a\s+[^>]*href=["\']([^"\']+)["\']', html, re.I)
    urls: list[str] = []
    for a in root.iter("a"):
        href = a.get("href")
        if href and _is_testable_url(href):
            urls.append(href.strip())
    return urls


def _get_field_raw(context, fieldname: str) -> str:
    """Lê o valor raw HTML de um campo RichText num contexto."""
    try:
        value = getattr(context, fieldname, None)
    except Exception:  # noqa: BLE001
        return ""
    if value is None:
        return ""
    # RichTextValue tem .raw; string é tratada como HTML directo
    raw = getattr(value, "raw", None)
    if raw is not None:
        return raw
    if isinstance(value, str):
        return value
    return ""


def _classify_href(href: str) -> str:
    """Classifica o tipo de href: 'resolveuid' / 'absolute' / 'site-relative'
    / 'page-relative' / 'skip'.

    Usado para decidir como resolver para URL absoluto.
    """
    s = href.strip()
    if not s:
        return "skip"
    if _SKIP_RE.match(s):
        return "skip"
    if RESOLVEUID_RE.search(s):
        return "resolveuid"
    if s.startswith(("http://", "https://", "//")):
        return "absolute"
    if s.startswith("/"):
        return "site-relative"
    return "page-relative"


def _resolve_to_absolute(
    href: str, page_url: str, site_root_url: str | None
) -> str | None:
    """Resolve qualquer href para um URL absoluto testável.

    Devolve None se o tipo é "skip" (mailto, tel, javascript, vazio).
    Caso contrário devolve o URL absoluto (ou, em fallback, o próprio href
    para urls que não conseguimos resolver mas queremos tentar mesmo assim).
    """
    kind = _classify_href(href)
    if kind == "skip":
        return None
    if kind == "absolute":
        return href
    if kind == "site-relative" and site_root_url:
        return site_root_url.rstrip("/") + href
    if kind == "site-relative":
        return href  # sem site_root_url — improvável
    # page-relative
    if page_url:
        return urljoin(page_url, href)
    return href  # sem page_url — improvável


def _try_resolve_uid(
    uid: str, _cache: dict[str, dict | None] | None = None
) -> dict | None:
    """Resolve um UID para o URL do conteúdo.

    Devolve dict com {url, title, path} ou None se o UID não existir.
    Usa _cache (passado pela função chamadora) para evitar queries repetidas.
    """
    if not uid:
        return None
    if _cache is not None and uid in _cache:
        return _cache[uid]
    try:
        catalog = api.portal.get_tool("portal_catalog")
        brains = catalog(UID=uid)
    except Exception as e:  # noqa: BLE001
        logger.warning("Erro a resolver UID %s: %s", uid, e)
        if _cache is not None:
            _cache[uid] = None
        return None
    if not brains:
        if _cache is not None:
            _cache[uid] = None
        return None
    brain = brains[0]
    info = {
        "url": brain.getURL(),
        "title": (brain.Title or "").strip(),
        "path": brain.getPath(),
    }
    if _cache is not None:
        _cache[uid] = info
    return info


def _try_resolve_path(
    plone_path: str, _cache: dict[str, dict | None] | None = None
) -> dict | None:
    """Resolve um path Plone (ex: '/Plone/folder/o-livro') para o conteúdo.

    Usado para verificar que links relativos apontam para conteúdo que existe.
    Devolve dict com {url, title, path} ou None.
    """
    if not plone_path:
        return None
    # normalizar path
    p = "/" + plone_path.strip("/")
    if _cache is not None and p in _cache:
        return _cache[p]
    try:
        catalog = api.portal.get_tool("portal_catalog")
        brains = catalog(path=p)
    except Exception as e:  # noqa: BLE001
        logger.warning("Erro a resolver path %s: %s", p, e)
        if _cache is not None:
            _cache[p] = None
        return None
    if not brains:
        if _cache is not None:
            _cache[p] = None
        return None
    brain = brains[0]
    info = {
        "url": brain.getURL(),
        "title": (brain.Title or "").strip(),
        "path": brain.getPath(),
    }
    if _cache is not None:
        _cache[p] = info
    return info


def _extract_plone_path(test_url: str, site_root_url: str | None) -> str | None:
    """Extrai o path Plone de um URL absoluto, removendo o prefixo site_root."""
    if not site_root_url or not test_url:
        return None
    # site_root_url termina tipicamente com /
    base = site_root_url.rstrip("/")
    if test_url.startswith(base + "/"):
        rest = test_url[len(base) :]
        # strip query/fragment
        rest = rest.split("?", 1)[0].split("#", 1)[0]
        # normalizar trailing slash (catalog path não usa)
        rest = rest.rstrip("/") or "/"
        return rest
    return None


def _split_internal(href: str) -> tuple[str, str]:
    """Separa fragmento. Devolve (url_base, fragmento)."""
    if "#" in href:
        url, _, frag = href.partition("#")
        return url, "#" + frag
    return href, ""


def collect_text_links(
    brains: Iterable,
    *,
    include_internal: bool = True,
    site_root_url: str | None = None,
) -> tuple[dict[str, list[dict]], dict[str, int]]:
    """Para cada brain, extrai os hrefs de todos os RichText fields.

    Resolve links `../resolveuid/UID` (e variantes) para o URL real do
    conteúdo, e usa esse URL como chave de dedup. Se o UID não existir,
    guarda o source_url para aparecer como link orfão nos resultados.

    Devolve:
      - url_pages: {url: [{uid, title, path, field, source_url?, resolved?}, ...]}
      - stats: {
          'pages_scanned': int,
          'fields_scanned': int,
          'urls_total': int,
          'urls_unique': int,
          'urls_skipped_internal': int,
          'resolveuid_resolved': int,
          'resolveuid_orphans': int,
        }
    """
    url_pages: dict[str, list[dict]] = {}
    stats = {
        "pages_scanned": 0,
        "fields_scanned": 0,
        "urls_total": 0,
        "urls_unique": 0,
        "urls_skipped_internal": 0,
        "resolveuid_resolved": 0,
        "resolveuid_orphans": 0,
        "relative_resolved": 0,
        "relative_orphans": 0,
    }
    uid_cache: dict[str, dict | None] = {}
    path_cache: dict[str, dict | None] = {}

    for brain in brains:
        try:
            obj = brain.getObject()
        except Exception:  # noqa: BLE001
            logger.warning("Não consegui obter objeto para %s", brain.getPath())
            continue

        stats["pages_scanned"] += 1
        # URL base para resolver hrefs page-relative (ex: "o-livro" → "https://site/folder/o-livro")
        try:
            page_url = obj.absolute_url() + "/"
        except Exception:  # noqa: BLE001
            page_url = ""
        page_entry = {
            "uid": brain.UID,
            "title": (brain.Title or obj.id or "").strip(),
            "path": "/".join(obj.getPhysicalPath()[2:]),
        }

        for fieldname, _schema in get_rich_text_fields(obj):
            stats["fields_scanned"] += 1
            raw = _get_field_raw(obj, fieldname)
            if not raw:
                continue
            for href in extract_links_from_html(raw):
                stats["urls_total"] += 1

                kind = _classify_href(href)
                if kind == "skip":
                    continue

                # Determinar o URL absoluto a testar + info de origem
                source_href = ""  # só preenchido se o href original não era absoluto
                resolved = None  # info de resolveuid (se aplicável)
                uid = None
                path_resolved = None  # info de path-resolved (para relativos)
                from_resolveuid = False
                from_relative = False

                if kind == "resolveuid":
                    from_resolveuid = True
                    m = RESOLVEUID_RE.search(href.strip())
                    uid = m.group("uid")
                    resolved = _try_resolve_uid(uid, _cache=uid_cache)
                    if resolved:
                        stats["resolveuid_resolved"] += 1
                        test_url = resolved["url"]
                    else:
                        stats["resolveuid_orphans"] += 1
                        # órfão: constrói URL absoluto do resolveuid no site
                        if site_root_url:
                            test_url = site_root_url + "resolveuid/" + uid
                        else:
                            test_url = href
                    source_href = href
                else:
                    # absolute / site-relative / page-relative → todos para URL absoluto
                    test_url = _resolve_to_absolute(href, page_url, site_root_url)
                    if test_url is None:
                        continue
                    # guardar o href original se for relativo (útil para debug)
                    if kind in ("site-relative", "page-relative"):
                        from_relative = True
                        source_href = href
                        # Tentar resolver via catalog (mesma técnica que resolveuid)
                        plone_path = _extract_plone_path(test_url, site_root_url)
                        if plone_path is not None:
                            path_resolved = _try_resolve_path(
                                plone_path, _cache=path_cache
                            )
                            if path_resolved:
                                stats["relative_resolved"] += 1
                            else:
                                stats["relative_orphans"] += 1

                # preservar fragmento
                if "#" in href and "#" not in test_url:
                    test_url = test_url + "#" + href.split("#", 1)[1]

                # É interno?
                is_internal = kind != "absolute" or (
                    site_root_url and test_url.startswith(site_root_url)
                )
                if not include_internal and is_internal:
                    stats["urls_skipped_internal"] += 1
                    continue

                # Chave de dedup = URL sem fragmento
                key = test_url.split("#", 1)[0] or test_url

                # Registar
                if key not in url_pages:
                    url_pages[key] = []
                already = any(
                    p["uid"] == page_entry["uid"] and p.get("field") == fieldname
                    for p in url_pages[key]
                )
                if not already:
                    entry = {**page_entry, "field": fieldname}
                    if source_href:
                        entry["source_url"] = source_href
                    if from_resolveuid:
                        entry["from_resolveuid"] = True
                        entry["resolved"] = bool(resolved)
                        if resolved:
                            entry["target_title"] = resolved["title"]
                            entry["target_path"] = resolved["path"]
                    elif from_relative:
                        entry["from_relative"] = True
                        entry["resolved_via_path"] = bool(path_resolved)
                        if path_resolved:
                            entry["target_title"] = path_resolved["title"]
                            entry["target_path"] = path_resolved["path"]
                    url_pages[key].append(entry)

    stats["urls_unique"] = len(url_pages)
    return url_pages, stats