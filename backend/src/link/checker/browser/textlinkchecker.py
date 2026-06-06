"""View: @@link-checker-text

Varre campos RichText de todos os conteúdos, extrai os hrefs e testa-os.

Estratégia para links resolveuid (links internos do Plone):
- Se o UID resolveu no catalog → o conteúdo existe, marco como OK sem HTTP
  (HEAD anónimo em URLs internas do Plone pode dar 404/403 mesmo com
  conteúdo válido — autenticação, view em falta, etc.)
- Se o UID não resolveu → órfão, marco como broken sem HTTP
- HTTP só é feito para URLs externas
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import logging

from plone import api
from Products.Five import BrowserView

from ..checker import check_links, classify, summarize
from ..textextract import collect_text_links
from .linkchecker import (
    _form_value,
    DEFAULT_TIMEOUT,
    DEFAULT_CONCURRENCY,
    DEFAULT_METHOD,
    DEFAULT_OK_RULE,
)

logger = logging.getLogger(__name__)

# Tipos por defeito que costumam ter rich text
DEFAULT_PORTAL_TYPES = [
    "Document",
    "News Item",
    "Event",
    "Folder",
    "Collection",
    "File",
]


def _collect_brains(portal_types: list[str], review_state: str | None = None):
    catalog = api.portal.get_tool("portal_catalog")
    query: dict = {"portal_type": portal_types, "sort_on": "sortable_title"}
    if review_state and review_state != "all":
        query["review_state"] = review_state
    return catalog(query)


def _site_root_url() -> str:
    portal = api.portal.get()
    return portal.absolute_url() + "/"


def _categorize_urls(
    url_pages: dict[str, list[dict]],
) -> tuple[set[str], set[str], list[str]]:
    """Divide URLs em 3 grupos:
      - internal_resolved: resolveuid OU relativo que resolveu (conteúdo existe) — OK sem HTTP
      - internal_orphans:  resolveuid OU relativo que NÃO resolveu — broken sem HTTP
      - external:          tudo o resto (URLs absolutas) — testar via HTTP

    Um URL é "internal" se TODAS as suas ocorrências vieram de resolveuid ou
    de link relativo (page/site), E foram resolvidas (ou todas não resolvidas).
    URLs absolutas vão sempre para external.
    """
    internal_resolved: set[str] = set()
    internal_orphans: set[str] = set()
    external: list[str] = []

    for url, pages in url_pages.items():
        if not pages:
            continue
        all_internal_source = all(
            p.get("from_resolveuid") or p.get("from_relative") for p in pages
        )
        all_resolved = all(
            p.get("resolved") or p.get("resolved_via_path") for p in pages
        )
        if all_internal_source and all_resolved:
            internal_resolved.add(url)
        elif all_internal_source and not all_resolved:
            internal_orphans.add(url)
        else:
            external.append(url)

    return internal_resolved, internal_orphans, external


def _make_internal_row(url: str, pages: list[dict], category: str) -> dict:
    """Cria uma row sintética para um URL interno (resolveuid/path resolvido ou órfão)."""
    source_url = (pages[0].get("source_url") if pages else "") or url
    from_resolveuid = any(p.get("from_resolveuid") for p in pages)
    from_relative = any(p.get("from_relative") for p in pages)
    if category == "ok":
        return {
            "url": url,
            "status": 200,
            "status_text": "OK (catalog)",
            "time_ms": 0,
            "final_url": "",
            "error": "",
            "redirected": False,
            "method": "catalog",
            "category": "ok",
            "pages": pages,
            "n_pages": len(pages),
            "from_resolveuid": from_resolveuid,
            "from_relative": from_relative,
            "has_orphan": False,
            "display_url": source_url,
        }
    # orphan
    detail = ""
    if pages:
        first_src = pages[0].get("source_url", "")
        if "resolveuid/" in first_src:
            detail = (
                first_src.split("resolveuid/", 1)[-1].split("?", 1)[0].split("#", 1)[0]
            )
        else:
            detail = first_src
    error_msg = (
        f"UID órfão: {detail}" if from_resolveuid else f"Path não existe: {detail}"
    )
    return {
        "url": url,
        "status": 0,
        "status_text": "Órfão",
        "time_ms": 0,
        "final_url": "",
        "error": error_msg,
        "redirected": False,
        "method": "catalog",
        "category": "broken",
        "pages": pages,
        "n_pages": len(pages),
        "from_resolveuid": from_resolveuid,
        "from_relative": from_relative,
        "has_orphan": True,
        "display_url": source_url,
    }


def _make_external_row(chk, pages: list[dict], ok_rule: str) -> dict:
    """Cria row a partir de um CheckResult (teste HTTP)."""
    row = {**chk.to_dict(), "pages": pages, "n_pages": len(pages)}
    row["category"] = classify(chk, ok_rule)
    row["from_resolveuid"] = any(p.get("source_url") for p in pages)
    row["has_orphan"] = any(
        p.get("source_url") and not p.get("resolved") for p in pages
    )
    row["display_url"] = chk.url
    return row


def _build_merged_results(
    url_pages: dict[str, list[dict]],
    *,
    concurrency: int,
    timeout: float,
    method: str,
    ok_rule: str,
) -> tuple[list[dict], dict]:
    """Faz o trabalho de categorizar, testar, e fundir resultados.

    Devolve (merged_rows, check_stats).
    """
    internal_resolved, internal_orphans, external = _categorize_urls(url_pages)

    merged: list[dict] = []
    for url in internal_resolved:
        merged.append(_make_internal_row(url, url_pages[url], "ok"))
    for url in internal_orphans:
        merged.append(_make_internal_row(url, url_pages[url], "broken"))

    check_stats = {
        "total": len(internal_resolved) + len(internal_orphans) + len(external),
        "ok": len(internal_resolved),
        "redirect": 0,
        "broken": len(internal_orphans),
        "error": 0,
    }

    if external:
        try:
            checks = asyncio.run(
                check_links(
                    external,
                    concurrency=concurrency,
                    timeout=timeout,
                    method=method,
                )
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("Erro no checker")
            raise
        for chk in checks:
            pages = url_pages.get(chk.url, [])
            merged.append(_make_external_row(chk, pages, ok_rule))
        # stats dos externos
        ext_stats = summarize(checks, ok_rule)
        check_stats["ok"] += ext_stats["ok"]
        check_stats["redirect"] += ext_stats["redirect"]
        check_stats["broken"] += ext_stats["broken"]
        check_stats["error"] += ext_stats["error"]

    return merged, check_stats


class TextLinkCheckerView(BrowserView):
    """Página principal: @@link-checker-text"""

    def __init__(self, context, request):
        super().__init__(context, request)
        self.url_pages: dict[str, list[dict]] = {}
        self.results: list[dict] = []
        self.collect_stats: dict = {}
        self.check_stats: dict = {}
        self.error = ""
        self.config = {
            "timeout": DEFAULT_TIMEOUT,
            "concurrency": DEFAULT_CONCURRENCY,
            "method": DEFAULT_METHOD,
            "ok_rule": DEFAULT_OK_RULE,
            "portal_types": DEFAULT_PORTAL_TYPES,
            "review_state": "all",
            "include_internal": True,
        }
        self.last_action = ""

    def export_query(self, fmt: str) -> str:
        """Devolve a query string para os botões de export (CSV/JSON).

        Junta os portal_types com vírgula, e serializa include_internal como 1/0.
        """
        return (
            f"format={fmt}"
            f"&portal_types={','.join(self.config['portal_types'])}"
            f"&review_state={self.config['review_state']}"
            f"&timeout={self.config['timeout']}"
            f"&concurrency={self.config['concurrency']}"
            f"&method={self.config['method']}"
            f"&ok_rule={self.config['ok_rule']}"
            f"&include_internal={'1' if self.config['include_internal'] else '0'}"
        )

    def __call__(self):
        # lê config do form
        pts = self.request.form.get("portal_types")
        if pts:
            # pode vir como string (comma-separated) ou lista (multi-value form)
            if isinstance(pts, str):
                self.config["portal_types"] = [
                    p.strip() for p in pts.split(",") if p.strip()
                ]
            else:
                self.config["portal_types"] = [
                    str(p).strip() for p in pts if str(p).strip()
                ]
        else:
            self.config["portal_types"] = DEFAULT_PORTAL_TYPES
        self.config["review_state"] = _form_value(self.request, "review_state", "all")
        self.config["timeout"] = _form_value(
            self.request, "timeout", DEFAULT_TIMEOUT, float
        )
        self.config["concurrency"] = _form_value(
            self.request, "concurrency", DEFAULT_CONCURRENCY, int
        )
        self.config["method"] = _form_value(self.request, "method", DEFAULT_METHOD)
        self.config["ok_rule"] = _form_value(self.request, "ok_rule", DEFAULT_OK_RULE)
        self.config["include_internal"] = _form_value(
            self.request, "include_internal", "1"
        ) in ("1", "true", "on", "yes", "True", True)

        action = self.request.form.get("action", "")
        self.last_action = action

        # 1) recolhe brains e extrai hrefs
        try:
            brains = _collect_brains(
                self.config["portal_types"],
                review_state=self.config["review_state"],
            )
            self.url_pages, self.collect_stats = collect_text_links(
                brains,
                include_internal=self.config["include_internal"],
                site_root_url=_site_root_url(),
            )
        except Exception as e:  # noqa: BLE001
            self.error = f"Erro a varrer páginas: {e}"
            return self.index()

        if not self.url_pages:
            return self.index()

        # 2) se pediu check, corre
        if action in ("check", "retest_failed"):
            try:
                self.results, self.check_stats = _build_merged_results(
                    self.url_pages,
                    concurrency=self.config["concurrency"],
                    timeout=self.config["timeout"],
                    method=self.config["method"],
                    ok_rule=self.config["ok_rule"],
                )
            except Exception as e:  # noqa: BLE001
                self.error = f"Erro no checker: {e}"
                return self.index()

            if action == "retest_failed":
                # mantém só os que não estão OK
                self.results = [r for r in self.results if r["category"] not in ("ok",)]
        else:
            # estado inicial: pendente
            self.results = [
                {
                    "url": url,
                    "status": 0,
                    "status_text": "",
                    "time_ms": None,
                    "final_url": "",
                    "error": "",
                    "redirected": False,
                    "method": "",
                    "category": "pending",
                    "pages": pages,
                    "n_pages": len(pages),
                    "from_resolveuid": any(p.get("source_url") for p in pages),
                    "has_orphan": any(
                        p.get("source_url") and not p.get("resolved") for p in pages
                    ),
                    "display_url": (
                        pages[0].get("source_url")
                        if pages and pages[0].get("source_url")
                        else url
                    ),
                }
                for url, pages in self.url_pages.items()
            ]
            self.check_stats = {
                "total": len(self.url_pages),
                "ok": 0,
                "redirect": 0,
                "broken": 0,
                "error": 0,
            }

        return self.index()


class TextLinkCheckerExport(BrowserView):
    """Exporta o resultado como CSV ou JSON."""

    def _parse_portal_types(self) -> list[str]:
        """Lê portal_types do form. Aceita string comma-separated ou lista."""
        raw = self.request.form.get("portal_types")
        if raw is None:
            return list(DEFAULT_PORTAL_TYPES)
        if isinstance(raw, (list, tuple)):
            return [str(p).strip() for p in raw if str(p).strip()]
        # string: pode ser "A,B,C" ou "['A', 'B', 'C']" (Python repr) — normalizamos
        s = str(raw).strip()
        # se parece repr de lista, extrair itens
        if s.startswith("[") and s.endswith("]"):
            inner = s[1:-1]
            parts = [p.strip().strip("'").strip('"') for p in inner.split(",")]
        else:
            parts = s.split(",")
        return [p.strip() for p in parts if p.strip()]

    def _parse_bool(self, name: str, default: bool = True) -> bool:
        """Lê um booleano do form, tolerante a '1', 'true', True, etc."""
        raw = self.request.form.get(name)
        if raw is None:
            return default
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("1", "true", "on", "yes")

    def __call__(self):
        fmt = self.request.form.get("format", "csv").lower()
        config = {
            "timeout": _form_value(self.request, "timeout", DEFAULT_TIMEOUT, float),
            "concurrency": _form_value(
                self.request, "concurrency", DEFAULT_CONCURRENCY, int
            ),
            "method": _form_value(self.request, "method", DEFAULT_METHOD),
            "ok_rule": _form_value(self.request, "ok_rule", DEFAULT_OK_RULE),
            "include_internal": self._parse_bool("include_internal", True),
            "portal_types": self._parse_portal_types(),
            "review_state": _form_value(self.request, "review_state", "all"),
        }

        try:
            brains = _collect_brains(
                config["portal_types"],
                review_state=config["review_state"],
            )
            url_pages, _ = collect_text_links(
                brains,
                include_internal=config["include_internal"],
                site_root_url=_site_root_url(),
            )
        except Exception as e:  # noqa: BLE001
            self.request.response.setStatus(500)
            return f"Erro: {e}"

        if not url_pages:
            body = "" if fmt == "csv" else "[]"
            ct = "text/csv" if fmt == "csv" else "application/json"
            self.request.response.setHeader("Content-Type", f"{ct}; charset=utf-8")
            self.request.response.setHeader(
                "Content-Disposition", f'attachment; filename="link-checker-text.{fmt}"'
            )
            return body

        try:
            merged, stats = _build_merged_results(
                url_pages,
                concurrency=config["concurrency"],
                timeout=config["timeout"],
                method=config["method"],
                ok_rule=config["ok_rule"],
            )
        except Exception as e:  # noqa: BLE001
            self.request.response.setStatus(500)
            return f"Erro: {e}"

        if fmt == "json":
            self.request.response.setHeader("Content-Type", "application/json; charset=utf-8")
            self.request.response.setHeader(
                "Content-Disposition", 'attachment; filename="link-checker-text.json"'
            )
            return json.dumps(
                {"config": config, "stats": stats, "results": merged},
                indent=2,
                ensure_ascii=False,
            )

        # CSV
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            "url",
            "display_url",
            "status",
            "status_text",
            "time_ms",
            "method",
            "redirected",
            "final_url",
            "error",
            "category",
            "from_resolveuid",
            "has_orphan",
            "n_pages",
            "page_titles",
            "page_paths",
        ])
        for row in merged:
            pages = row.get("pages", [])
            writer.writerow([
                row.get("url", ""),
                row.get("display_url", ""),
                row.get("status", 0),
                row.get("status_text", ""),
                row.get("time_ms", ""),
                row.get("method", ""),
                row.get("redirected", False),
                row.get("final_url", ""),
                row.get("error", ""),
                row.get("category", ""),
                row.get("from_resolveuid", False),
                row.get("has_orphan", False),
                row.get("n_pages", 0),
                " | ".join(p.get("title", "") for p in pages),
                " | ".join(p.get("path", "") for p in pages),
            ])
        self.request.response.setHeader("Content-Type", "text/csv; charset=utf-8")
        self.request.response.setHeader(
            "Content-Disposition", 'attachment; filename="link-checker-text.csv"'
        )
        return buf.getvalue()