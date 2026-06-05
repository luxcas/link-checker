"""View: @@link-checker-text

Varre campos RichText de todos os conteúdos, extrai os hrefs e testa-os.
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
from .linkchecker import _form_value, DEFAULT_TIMEOUT, DEFAULT_CONCURRENCY, DEFAULT_METHOD, DEFAULT_OK_RULE

logger = logging.getLogger(__name__)

# Tipos por defeito que costumam ter rich text
DEFAULT_PORTAL_TYPES = ["Document", "News Item", "Event", "Folder", "Collection", "File"]


def _collect_brains(portal_types: list[str], review_state: str | None = None):
    catalog = api.portal.get_tool("portal_catalog")
    query: dict = {"portal_type": portal_types, "sort_on": "sortable_title"}
    if review_state and review_state != "all":
        query["review_state"] = review_state
    return catalog(query)


def _site_root_url() -> str:
    portal = api.portal.get()
    return portal.absolute_url() + "/"


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

    def __call__(self):
        # lê config do form
        pts = self.request.form.get("portal_types")
        if pts:
            self.config["portal_types"] = [p.strip() for p in pts.split(",") if p.strip()]
        else:
            self.config["portal_types"] = DEFAULT_PORTAL_TYPES
        self.config["review_state"] = _form_value(self.request, "review_state", "all")
        self.config["timeout"] = _form_value(self.request, "timeout", DEFAULT_TIMEOUT, float)
        self.config["concurrency"] = _form_value(self.request, "concurrency", DEFAULT_CONCURRENCY, int)
        self.config["method"] = _form_value(self.request, "method", DEFAULT_METHOD)
        self.config["ok_rule"] = _form_value(self.request, "ok_rule", DEFAULT_OK_RULE)
        self.config["include_internal"] = _form_value(
            self.request, "include_internal", "1"
        ) in ("1", "true", "on", "yes")

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
                urls = list(self.url_pages.keys())
                checks = asyncio.run(check_links(
                    urls,
                    concurrency=self.config["concurrency"],
                    timeout=self.config["timeout"],
                    method=self.config["method"],
                ))
            except Exception as e:  # noqa: BLE001
                self.error = f"Erro no checker: {e}"
                return self.index()

            merged: list[dict] = []
            for chk in checks:
                pages = self.url_pages.get(chk.url, [])
                row = {**chk.to_dict(), "pages": pages, "n_pages": len(pages)}
                row["category"] = classify(chk, self.config["ok_rule"])
                merged.append(row)

            if action == "retest_failed":
                merged = [r for r in merged if r["category"] not in ("ok",)]

            self.results = merged
            self.check_stats = summarize(checks, self.config["ok_rule"])
        else:
            # estado inicial: pendente
            self.results = [
                {
                    "url": url,
                    "status": 0, "status_text": "", "time_ms": None,
                    "final_url": "", "error": "", "redirected": False,
                    "method": "", "category": "pending",
                    "pages": pages, "n_pages": len(pages),
                }
                for url, pages in self.url_pages.items()
            ]
            self.check_stats = {"total": len(self.url_pages), "ok": 0,
                                "redirect": 0, "broken": 0, "error": 0}

        return self.index()


class TextLinkCheckerExport(BrowserView):
    """Exporta o resultado como CSV ou JSON."""

    def __call__(self):
        fmt = self.request.form.get("format", "csv").lower()
        config = {
            "timeout": _form_value(self.request, "timeout", DEFAULT_TIMEOUT, float),
            "concurrency": _form_value(self.request, "concurrency", DEFAULT_CONCURRENCY, int),
            "method": _form_value(self.request, "method", DEFAULT_METHOD),
            "ok_rule": _form_value(self.request, "ok_rule", DEFAULT_OK_RULE),
            "include_internal": _form_value(
                self.request, "include_internal", "1"
            ) in ("1", "true", "on", "yes"),
            "portal_types": [
                p.strip() for p in (
                    self.request.form.get("portal_types")
                    or ",".join(DEFAULT_PORTAL_TYPES)
                ).split(",") if p.strip()
            ],
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
            self.request.response.setHeader("Content-Disposition", f'attachment; filename="link-checker-text.{fmt}"')
            return body

        try:
            urls = list(url_pages.keys())
            checks = asyncio.run(check_links(
                urls,
                concurrency=config["concurrency"],
                timeout=config["timeout"],
                method=config["method"],
            ))
        except Exception as e:  # noqa: BLE001
            self.request.response.setStatus(500)
            return f"Erro: {e}"

        merged: list[dict] = []
        for chk in checks:
            pages = url_pages.get(chk.url, [])
            row = {**chk.to_dict(), "pages": pages, "n_pages": len(pages)}
            row["category"] = classify(chk, config["ok_rule"])
            merged.append(row)

        if fmt == "json":
            self.request.response.setHeader("Content-Type", "application/json; charset=utf-8")
            self.request.response.setHeader(
                "Content-Disposition", 'attachment; filename="link-checker-text.json"'
            )
            return json.dumps(
                {
                    "config": config,
                    "stats": summarize(checks, config["ok_rule"]),
                    "results": merged,
                },
                indent=2,
                ensure_ascii=False,
            )

        # CSV
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            "url", "status", "status_text", "time_ms", "method",
            "redirected", "final_url", "error", "category",
            "n_pages", "page_titles", "page_paths",
        ])
        for row in merged:
            pages = row.get("pages", [])
            writer.writerow([
                row.get("url", ""),
                row.get("status", 0),
                row.get("status_text", ""),
                row.get("time_ms", ""),
                row.get("method", ""),
                row.get("redirected", False),
                row.get("final_url", ""),
                row.get("error", ""),
                row.get("category", ""),
                row.get("n_pages", 0),
                " | ".join(p.get("title", "") for p in pages),
                " | ".join(p.get("path", "") for p in pages),
            ])
        self.request.response.setHeader("Content-Type", "text/csv; charset=utf-8")
        self.request.response.setHeader(
            "Content-Disposition", 'attachment; filename="link-checker-text.csv"'
        )
        return buf.getvalue()
