# Link checher 🚀

[![Built with Cookieplone](https://img.shields.io/badge/built%20with-Cookieplone-0083be.svg?logo=cookiecutter)](https://github.com/plone/cookieplone-templates/)
[![Black code style](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)
[![Backend Tests](https://github.com/luxcas/link-checker/actions/workflows/backend.yml/badge.svg)](https://github.com/luxcas/link-checker/actions/workflows/backend.yml)

Add-on para **Plone 6 classic** que testa links do site de duas formas:

1. **`/@@link-checker`** — testa os `remoteUrl` de um content type (default: `Link`)
2. **`/@@link-checker-text`** — varre todos os campos rich text (`<a href>`) das páginas

Ambos mostram tabela com resultados, stats agregadas, export CSV/JSON,
e re-testar falhados.

Pensado para sítios com **centenas a milhares de Links**.

## Funcionalidades

- 🔍 **Modo 1**: lista todos os conteúdos do tipo configurável (default: `Link`)
- 📝 **Modo 2**: varre campos rich text de Document/News/Event/Folder/Collection e extrai `<a href>`
- ⚡ Testa em paralelo com `httpx` (concorrência configurável, até 200)
- ⏱️ Timeout por request configurável
- 📊 Estatísticas agregadas: vivos / redirecionam / quebrados / erros de rede
- 🔁 Re-testar só os falhados
- 📍 Modo 2 mostra em quantas páginas cada URL aparece e quais são
- 🔌 Filtro opcional para excluir links internos (relativos ao site)
- 🆔 **Resolve automaticamente links `../resolveuid/UID`** do Plone — testa o conteúdo real, não o resolveuid. Se o UID não existir (conteúdo apagado), marca como órfão
- 📥 Export CSV e JSON
- 🛡️ Permissão dedicada (`collective.linkchecker.Run`)
- 🚫 Sem JavaScript externo — funciona mesmo com JS desligado
- 🧵 Tudo server-side, sem precisar de Volto

## Quick Start 🏁
## Uso

Vai a `/@@link-checker` em qualquer contexto do site (ex.: `/Plone/@@link-checker`).

### `@@link-checker` — Link content

A página mostra:
1. Configuração (timeout, concorrência, método HTTP, regra "OK", tipo de conteúdo)
2. Botão **Testar todos** — corre o check em todos os Links
3. Tabela com resultados
4. Botão **Re-testar falhados** — só os que falharam
5. Botões **↓ CSV** e **↓ JSON** — exportam o resultado

### `@@link-checker-text` — Links em rich text

A página mostra:
1. Configuração (tipos de conteúdo a varrer, estado de workflow, concorrência, etc.)
2. Checkbox **Incluir links internos** (desligar para ignorar `/path` relativos)
3. Botão **Varrer e testar** — percorre todas as páginas, extrai `<a href>` e testa
4. Stats: páginas varridas / campos / hrefs totais / URLs únicos
5. Tabela com 1 linha por URL único + coluna "N página(s)"
6. Click em "N página(s)" expande a lista das páginas onde esse URL aparece
7. Re-testar falhados + export CSV/JSON

### Configuração recomendada

- **1000 links**: `concurrency=50`, `timeout=10s`, `method=HEAD` → ~20-30s
- **5000+ links**: `concurrency=100`, `timeout=8s`, considera correr fora de horas
- **Sites com firewalls**: usa `method=GET` se os servidores não suportarem `HEAD`
- **Para o modo texto**: começa com **apenas Document + News Item** publicados para estimar volume

## Performance e notas técnicas

- **Bloqueio do worker**: o check corre dentro do request HTTP via
  `asyncio.run`. Para volumes muito grandes (>10k) considera usar uma task
  queue (`collective.taskqueue` ou `plone.app.async`).
- **Re-entrância em `getObject()`**: o catalog não tem índice para
  `remoteUrl` por defeito, portanto o add-on lê `obj.remoteUrl` para cada
  brain. Para sítios com dezenas de milhar de Links, adiciona um índice:
  ```xml
  <object name="portal_catalog" meta_type="CatalogTool">
    <index name="remoteUrl" meta_type="FieldIndex">
      <indexed_attr name="remoteUrl"/>
    </index>
  </object>
  ```
  E reindexa: `portal_catalog.manage_reindexIndex(['remoteUrl'])`.
- **Sem CORS**: o teste é feito pelo servidor Plone, sem browser no meio.
- **Timeouts**: alguns sites bloqueiam bots — se tiveres muitos `Timeout`,
  reduz a concorrência ou aumenta o timeout.

### Prerequisites ✅

-   An [operating system](https://6.docs.plone.org/install/create-project-cookieplone.html#prerequisites-for-installation) that runs all the requirements mentioned.
-   [uv](https://6.docs.plone.org/install/create-project-cookieplone.html#uv)
-   [Make](https://6.docs.plone.org/install/create-project-cookieplone.html#make)
-   [Git](https://6.docs.plone.org/install/create-project-cookieplone.html#git)
-   [Docker](https://docs.docker.com/get-started/get-docker/) (optional)


### Installation 🔧

1.  Clone this repository, then change your working directory.

    ```shell
    git clone git@github.com:luxcas/link-checker.git
    cd link-checker
    ```

2.  Install this code base.

    ```shell
    make install
    ```


### Fire Up the Servers 🔥

1.  Create a new Plone site on your first run.

    ```shell
    make backend-create-site
    ```

2.  Start the backend at http://localhost:8080/.

    ```shell
    make backend-start
    ```

Voila! Your Plone site should be live and kicking! 🎉

### Local Stack Deployment 📦

Deploy a local Docker Compose environment that includes the following.

- Docker image for Backend 🖼️
- A stack with a Traefik router and a PostgreSQL database 🗃️
- Accessible at [http://link-checker.localhost](http://link-checker.localhost) 🌐

Run the following commands in a shell session.

```shell
make stack-create-site
make stack-start
```

And... you're all set! Your Plone site is up and running locally! 🚀

## Project structure 🏗️

This monorepo consists of the following distinct sections:

- **backend**: Houses the API and Plone installation, utilizing pip instead of buildout, and includes a policy package named link.checker.
- **devops**: Encompasses Docker stack, Ansible playbooks, and cache settings.
- **docs**: Scaffold for writing documentation for your project.

### Why this structure? 🤔

- All necessary codebases to run the site are contained within the repository (excluding existing add-ons for Plone).
- Specific GitHub Workflows are triggered based on changes in each codebase (refer to .github/workflows).
- Simplifies the creation of Docker images for each codebase.
- Demonstrates Plone installation/setup without buildout.

## Code quality assurance 🧐

To check your code against quality standards, run the following shell command.

```shell
make check
```

### Format the codebase

To format and rewrite the code base, ensuring it adheres to quality standards, run the following shell command.

```shell
make format
```

| Section | Tool | Description | Configuration |
| --- | --- | --- | --- |
| backend | Ruff | Python code formatting, imports sorting  | [`backend/pyproject.toml`](./backend/pyproject.toml) |
| backend | `zpretty` | XML and ZCML formatting  | -- |

### Linting the codebase
or `lint`:

 ```shell
make lint
```

| Section | Tool | Description | Configuration |
| --- | --- | --- | --- |
| backend | Ruff | Checks code formatting, imports sorting  | [`backend/pyproject.toml`](./backend/pyproject.toml) |
| backend | Pyroma | Checks Python package metadata  | -- |
| backend | check-python-versions | Checks Python version information  | -- |
| backend | `zpretty` | Checks XML and ZCML formatting  | -- |

## Internationalization 🌐

Generate translation files for Plone with ease:

```shell
make i18n
```

## Credits and acknowledgements 🙏

Generated using [Cookieplone (1.0.0)](https://github.com/plone/cookieplone) and [cookieplone-templates (103d811)](https://github.com/plone/cookieplone-templates/commit/103d811612845aa22b1096890801c7bddd8615fb) on 2026-06-03 23:27:52.624373. A special thanks to all contributors and supporters!
