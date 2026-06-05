"""Views: @@link-checker (UI) e @@link-checker-export (CSV/JSON)."""
from __future__ import annotations

import asyncio
import csv
import io
import json
import logging

from plone import api
from Products.Five import BrowserView

from ..checker import check_links, classify, summarize

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 10.0
DEFAULT_CONCURRENCY = 50
DEFAULT_METHOD = "HEAD"
DEFAULT_OK_RULE = "2xx-3xx"


def _collect_links(portal_type: str = "Link") -> list[dict]:
    """Pergunta ao catalog e devolve uma lista de {uid,title,url,path}."""
    catalog = api.portal.get_tool("portal_catalog")
    brains = catalog(
        portal_type=portal_type,
        sort_on="sortable_title",
    )
    out: list[dict] = []
    for brain in brains:
        try:
            obj = brain.getObject()
        except Exception:  # noqa: BLE001
            logger.warning("Não consegui obter objeto para brain %s", brain.getPath())
            continue
        url = getattr(obj, "remoteUrl", None) or ""
        if not url.strip():
            continue
        out.append({
            "uid": brain.UID,
            "title": (brain.Title or obj.id or "").strip(),
            "url": url.strip(),
            "path": "/".join(obj.getPhysicalPath()[2:]),  # sem /Plone/portal
            "review_state": brain.review_state or "",
        })
    return out


def _form_value(request, name, default, cast=str):
    raw = request.form.get(name)
    if raw is None or raw == "":
        return default
    try:
        return cast(raw)
    except (TypeError, ValueError):
        return default


class LinkCheckerView(BrowserView):
    """Página principal: lista os Links e (se pedido) testa-os."""

    def __init__(self, context, request):
        super().__init__(context, request)
        self.results = []
        self.items = []
        self.stats = {"total": 0, "ok": 0, "redirect": 0, "broken": 0, "error": 0}
        self.error = ""
        self.config = {
            "timeout": DEFAULT_TIMEOUT,
            "concurrency": DEFAULT_CONCURRENCY,
            "method": DEFAULT_METHOD,
            "ok_rule": DEFAULT_OK_RULE,
            "portal_type": "Link",
        }
        self.last_action = ""

    def __call__(self):
        # lê config do form (sempre, para o template mostrar)
        self.config = {
            "timeout": _form_value(self.request, "timeout", DEFAULT_TIMEOUT, float),
            "concurrency": _form_value(self.request, "concurrency", DEFAULT_CONCURRENCY, int),
            "method": _form_value(self.request, "method", DEFAULT_METHOD),
            "ok_rule": _form_value(self.request, "ok_rule", DEFAULT_OK_RULE),
            "portal_type": _form_value(self.request, "portal_type", "Link"),
        }
        action = self.request.form.get("action", "")
        self.last_action = action

        # Recolhe items
        try:
            self.items = _collect_links(self.config["portal_type"])
        except Exception as e:  # noqa: BLE001
            self.error = f"Erro a ler o catalog: {e}"
            return self.index()

        if action in ("check", "retest_failed"):
            try:
                urls = [it["url"] for it in self.items]
                checks = asyncio.run(check_links(
                    urls,
                    concurrency=self.config["concurrency"],
                    timeout=self.config["timeout"],
                    method=self.config["method"],
                ))
            except Exception as e:  # noqa: BLE001
                self.error = f"Erro no checker: {e}"
                return self.index()

            # junta items + checks
            merged: list[dict] = []
            for item, chk in zip(self.items, checks):
                row = {**item, **chk.to_dict()}
                row["category"] = classify(chk, self.config["ok_rule"])
                merged.append(row)

            if action == "retest_failed":
                merged = [r for r in merged if r["category"] not in ("ok",)]

            self.results = merged
            self.stats = summarize(checks, self.config["ok_rule"])
        else:
            # estado inicial: tudo "pendente"
            self.results = [
                {**it, "status": 0, "status_text": "", "time_ms": None,
                 "final_url": "", "error": "", "redirected": False,
                 "method": "", "category": "pending"}
                for it in self.items
            ]
            self.stats = {"total": len(self.items), "ok": 0, "redirect": 0,
                          "broken": 0, "error": 0}

        return self.index()


class LinkCheckerExport(BrowserView):
    """Exporta os resultados como CSV ou JSON. Re-corre o check."""

    def __call__(self):
        fmt = self.request.form.get("format", "csv").lower()
        config = {
            "timeout": _form_value(self.request, "timeout", DEFAULT_TIMEOUT, float),
            "concurrency": _form_value(self.request, "concurrency", DEFAULT_CONCURRENCY, int),
            "method": _form_value(self.request, "method", DEFAULT_METHOD),
            "ok_rule": _form_value(self.request, "ok_rule", DEFAULT_OK_RULE),
            "portal_type": _form_value(self.request, "portal_type", "Link"),
        }
        items = _collect_links(config["portal_type"])
        if not items:
            body = "" if fmt == "csv" else "[]"
            ct = "text/csv" if fmt == "csv" else "application/json"
            self.request.response.setHeader("Content-Type", f"{ct}; charset=utf-8")
            self.request.response.setHeader("Content-Disposition", f'attachment; filename="link-checker.{fmt}"')
            return body

        try:
            urls = [it["url"] for it in items]
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
        for item, chk in zip(items, checks):
            row = {**item, **chk.to_dict()}
            row["category"] = classify(chk, config["ok_rule"])
            merged.append(row)

        if fmt == "json":
            self.request.response.setHeader("Content-Type", "application/json; charset=utf-8")
            self.request.response.setHeader(
                "Content-Disposition",
                'attachment; filename="link-checker.json"',
            )
            return json.dumps(
                {"config": config, "stats": summarize(checks, config["ok_rule"]), "results": merged},
                indent=2,
                ensure_ascii=False,
            )

        # CSV (default)
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            "uid", "title", "url", "path", "review_state",
            "status", "status_text", "time_ms", "method",
            "redirected", "final_url", "error", "category",
        ])
        for row in merged:
            writer.writerow([
                row.get("uid", ""), row.get("title", ""), row.get("url", ""),
                row.get("path", ""), row.get("review_state", ""),
                row.get("status", 0), row.get("status_text", ""),
                row.get("time_ms", ""), row.get("method", ""),
                row.get("redirected", False), row.get("final_url", ""),
                row.get("error", ""), row.get("category", ""),
            ])
        self.request.response.setHeader("Content-Type", "text/csv; charset=utf-8")
        self.request.response.setHeader(
            "Content-Disposition",
            'attachment; filename="link-checker.csv"',
        )
        return buf.getvalue()
