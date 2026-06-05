"""Motor de teste de links — assíncrono, com concorrência limitada.

Usa httpx.AsyncClient (já vem com plone.restapi em Plone 6) com semáforo
para limitar concorrência. Para 1000 links a 50 conexões em paralelo
demora ~20-30s, dependendo do tempo médio de resposta.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, asdict
from typing import Iterable

import httpx

logger = logging.getLogger(__name__)

USER_AGENT = "collective.linkchecker/1.0 (+Plone Link Checker)"


@dataclass
class CheckResult:
    url: str
    status: int
    status_text: str
    time_ms: int
    final_url: str
    error: str
    redirected: bool
    method: str

    def to_dict(self) -> dict:
        return asdict(self)


async def _check_one(
    client: httpx.AsyncClient,
    url: str,
    timeout: float,
    method: str,
    follow: bool = True,
) -> CheckResult:
    """Faz um único request. Trata timeout e erros de rede."""
    t0 = time.perf_counter()
    try:
        resp = await client.request(
            method,
            url,
            timeout=timeout,
            follow_redirects=follow,
        )
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        return CheckResult(
            url=url,
            status=resp.status_code,
            status_text=resp.reason_phrase or "",
            time_ms=elapsed_ms,
            final_url=str(resp.url) if str(resp.url) != url else "",
            error="",
            redirected=300 <= resp.status_code < 400,
            method=method,
        )
    except httpx.TimeoutException:
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        return CheckResult(
            url=url, status=0, status_text="Timeout", time_ms=elapsed_ms,
            final_url="", error="Timeout", redirected=False, method=method,
        )
    except httpx.ConnectError as e:
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        return CheckResult(
            url=url, status=0, status_text="ConnectError", time_ms=elapsed_ms,
            final_url="", error=str(e)[:200], redirected=False, method=method,
        )
    except httpx.RequestError as e:
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        return CheckResult(
            url=url, status=0, status_text=type(e).__name__, time_ms=elapsed_ms,
            final_url="", error=str(e)[:200], redirected=False, method=method,
        )
    except Exception as e:  # noqa: BLE001 — apanha mesmo o inesperado
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        logger.exception("Erro inesperado ao testar %s", url)
        return CheckResult(
            url=url, status=0, status_text="Error", time_ms=elapsed_ms,
            final_url="", error=f"{type(e).__name__}: {e}"[:200],
            redirected=False, method=method,
        )


async def check_links(
    urls: Iterable[str],
    *,
    concurrency: int = 50,
    timeout: float = 10.0,
    method: str = "HEAD",
    follow_redirects: bool = True,
) -> list[CheckResult]:
    """Testa uma lista de URLs em paralelo com concorrência limitada.

    Devolve uma lista de CheckResult na MESMA ORDEM dos urls de input.
    URLs duplicadas são testadas apenas uma vez (a posição é preservada
    via mapeamento).

    Implementação: dois passes — primeiro descobre URLs únicas, depois
    testa-as em paralelo com semáforo. Finalmente mapeia os resultados
    para a ordem original.
    """
    urls_list = list(urls)
    if not urls_list:
        return []

    # dedup mantendo a primeira ocorrência
    seen: set[str] = set()
    unique: list[str] = []
    for u in urls_list:
        if u not in seen:
            seen.add(u)
            unique.append(u)

    sem = asyncio.Semaphore(max(1, concurrency))
    limits = httpx.Limits(
        max_connections=concurrency,
        max_keepalive_connections=concurrency,
    )

    async with httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
        timeout=timeout,
        limits=limits,
        follow_redirects=follow_redirects,
        http2=False,
        verify=True,
    ) as client:
        async def run(u: str) -> CheckResult:
            async with sem:
                return await _check_one(client, u, timeout, method, follow_redirects)

        # progresso (logger) — útil para debugging
        results_unique: list[CheckResult] = []
        total = len(unique)
        for i, coro in enumerate(asyncio.as_completed([run(u) for u in unique])):
            res = await coro
            results_unique.append(res)
            if (i + 1) % 50 == 0 or (i + 1) == total:
                logger.info("Link checker: %d/%d", i + 1, total)

    # Mapear para a ordem original (incluindo duplicados)
    result_by_url: dict[str, CheckResult] = {r.url: r for r in results_unique}
    return [result_by_url[u] for u in urls_list]


def classify(check: CheckResult, ok_rule: str = "2xx-3xx") -> str:
    """Classifica o resultado: 'ok', 'redirect', 'broken', 'error'."""
    if check.status == 0:
        return "error"
    if check.redirected:
        # 3xx conta como redirect (e como OK no 2xx-3xx)
        if ok_rule == "2xx":
            return "redirect"  # fora da regra, não é "ok"
        return "redirect"  # é redirect, mesmo que também seja OK
    if ok_rule == "2xx" and 200 <= check.status < 300:
        return "ok"
    if ok_rule == "2xx-3xx" and 200 <= check.status < 400:
        return "ok"
    if ok_rule == "any" and check.status > 0:
        return "ok"
    return "broken"


def summarize(results: list[CheckResult], ok_rule: str = "2xx-3xx") -> dict:
    """Devolve contadores: total, ok, redirect, broken, error."""
    s = {"total": len(results), "ok": 0, "redirect": 0, "broken": 0, "error": 0}
    for r in results:
        cat = classify(r, ok_rule)
        s[cat] += 1
    return s
