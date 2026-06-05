"""Extrator de links a partir de campos RichText.

Itera sobre os schemas de um conteúdo (incluindo behaviours) e recolhe
todos os hrefs dentro de <a> em cada campo RichText.
"""
from __future__ import annotations

import logging
import re
from typing import Iterable

import lxml.html
import lxml.etree
from plone.app.textfield import RichText
from plone.dexterity.utils import iterSchemata
from zope.schema import getFieldsInOrder

logger = logging.getLogger(__name__)

# Protocolos que ignoramos (não são URLs web testáveis)
_SKIP_RE = re.compile(r"^(?:mailto:|tel:|sms:|javascript:|data:|#)", re.I)


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


def _normalise(href: str, base_url: str | None = None) -> str | None:
    """Limpa o href. Resolve relativos se base_url for dado.

    Devolve None se for interno (mesmo site) e pretendermos ignorar.
    Para já, devolve o href tal-qual após trim; a filtragem de "internos"
    é feita fora via include_internal.
    """
    href = href.strip()
    # remove fragmento para a chave de dedup, mas mantém no display
    return href


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


def collect_text_links(
    brains: Iterable,
    *,
    include_internal: bool = True,
    site_root_url: str | None = None,
) -> tuple[dict[str, list[dict]], dict[str, int]]:
    """Para cada brain, extrai os hrefs de todos os RichText fields.

    Devolve:
      - url_pages: {url: [{uid, title, path, field}, ...]} — em ordem de aparição
      - stats: {
          'pages_scanned': int,
          'fields_scanned': int,
          'urls_total': int,           # total de hrefs (com repetições)
          'urls_unique': int,
          'urls_skipped_internal': int
        }
    """
    url_pages: dict[str, list[dict]] = {}
    stats = {
        "pages_scanned": 0,
        "fields_scanned": 0,
        "urls_total": 0,
        "urls_unique": 0,
        "urls_skipped_internal": 0,
    }

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

                if not include_internal and site_root_url and href.startswith("/"):
                    stats["urls_skipped_internal"] += 1
                    continue

                # chave de dedup = url sem fragmento
                key = href.split("#", 1)[0]
                if not key:
                    key = href
                if key not in url_pages:
                    url_pages[key] = []
                # evitar duplicar a mesma (página, campo) para o mesmo href
                already = any(
                    p["uid"] == page_entry["uid"] and p.get("field") == fieldname
                    for p in url_pages[key]
                )
                if not already:
                    url_pages[key].append({**page_entry, "field": fieldname})

    stats["urls_unique"] = len(url_pages)
    return url_pages, stats
