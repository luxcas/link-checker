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
    }
    uid_cache: dict[str, dict | None] = {}

    for brain in brains:
        try:
            obj = brain.getObject()
        except Exception:  # noqa: BLE001
            logger.warning("Não consegui obter objeto para %s", brain.getPath())
            continue

        stats["pages_scanned"] += 1
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

                # 1) Detectar resolveuid — tentar resolver o UID
                resolved = None
                is_resolveuid = False
                m = RESOLVEUID_RE.search(href.strip())
                if m:
                    is_resolveuid = True
                    uid = m.group("uid")
                    resolved = _try_resolve_uid(uid, _cache=uid_cache)
                    if resolved:
                        stats["resolveuid_resolved"] += 1
                    else:
                        stats["resolveuid_orphans"] += 1

                # 2) Determinar a chave (URL a testar)
                if is_resolveuid and resolved:
                    # usa o URL real resolvido + fragmento original se houver
                    if "#" in href:
                        frag = "#" + href.split("#", 1)[1]
                    else:
                        frag = ""
                    key = resolved["url"] + frag
                elif is_resolveuid and not resolved:
                    # UID orfão — construir URL absoluto do resolveuid no site
                    # para que o Plone retorne 404 (em vez de httpx dar InvalidURL)
                    if "#" in href:
                        frag = "#" + href.split("#", 1)[1]
                    else:
                        frag = ""
                    if site_root_url:
                        key = site_root_url + "resolveuid/" + uid + frag
                    else:
                        # sem site_root_url (não deve acontecer) — usa o source
                        key = href
                else:
                    # href normal — usa como está (sem fragmento na chave)
                    key = href.split("#", 1)[0] or href

                # 3) Filtro de internos
                # Trata como interno se:
                #   - começa por / (relativo ao site)
                #   - começa por site_root_url (absoluto interno)
                #   - é resolveuid (qualquer variante)
                is_internal = (
                    is_resolveuid
                    or (site_root_url and key.startswith(site_root_url))
                    or key.startswith("/")
                )
                if not include_internal and is_internal:
                    stats["urls_skipped_internal"] += 1
                    continue

                # 4) Registar
                if key not in url_pages:
                    url_pages[key] = []
                already = any(
                    p["uid"] == page_entry["uid"] and p.get("field") == fieldname
                    for p in url_pages[key]
                )
                if not already:
                    entry = {**page_entry, "field": fieldname}
                    if is_resolveuid:
                        entry["source_url"] = href
                        entry["resolved"] = bool(resolved)
                        if resolved:
                            entry["target_title"] = resolved["title"]
                            entry["target_path"] = resolved["path"]
                    url_pages[key].append(entry)

    stats["urls_unique"] = len(url_pages)
    return url_pages, stats
