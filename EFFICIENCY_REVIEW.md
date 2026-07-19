# Efficiency Review

## Статус выполнения

Все пункты текущего плана выполнены в этой ветке:

- [x] быстрые format/lint/hygiene checks перенесены в pre-commit, полный `just check` — в pre-push;
- [x] `pytest.ini` удалён, единственным источником pytest-конфигурации стал `sidecar/pyproject.toml`;
- [x] Dependabot переведён с `pip` на `uv`;
- [x] GitHub Actions обновлены и закреплены по immutable SHA с version comments;
- [x] лишний `actions/setup-node` удалён из package smoke matrix;
- [x] Biome ограничен реальными TypeScript и JSON paths проекта;
- [x] Python 3.13 закреплён в metadata, Ruff, mypy, ty, dev/package runtime и CI;
- [x] `bootstrap-sha`, `.npmignore` и неиспользуемые emit-параметры TypeScript удалены;
- [x] targets mypy и ty вынесены в конфигурацию и расширены на весь `tests/python` вместе с `sidecar`;
- [x] для `justfile` задан `indent_size = 4`;
- [x] Biome, Pi packages, Ruff, mypy, ty и prek обновлены в рамках patch/minor;
- [x] pytest отдельно обновлён до 9.1.1 и проверен полным Python gate;
- [x] независимые TypeScript и Python gates выполняются параллельно внутри единого `just check` с потоковым выводом.

Проверка реализации: `just check` проходит с 16 TypeScript и 44 Python тестами, `just release-check` проходит production package smoke, а uv 0.8.13 из CI успешно проверяет и синхронизирует lockfile. Отложенный follow-up по способу доставки uv и внутренняя версия sidecar намеренно не менялись.

## Краткий вывод

Репозиторий уже использует современный стек: Bun, uv, Ruff, ty, prek, just, Biome, Release Please и npm Trusted Publishing через OIDC.

Основные возможности для улучшения связаны не с заменой этих инструментов, а с устранением дублирования, сокращением локального feedback loop и выравниванием версий и источников конфигурации.

## Приоритетные улучшения

### 1. Перенести полный quality gate с pre-commit на pre-push

Сейчас `prek` выполняет весь `just check` при каждом commit.

Фактическое время:

```text
just check:             19.04s
Bun integration tests: ~13s
pytest:                 ~4.7s
Biome:                  ~0.05s
```

Рекомендуемая схема:

- pre-commit: быстрые format, lint и repository hygiene checks;
- pre-push: полный `just check`;
- CI: полный `just check`.

Это даст заметно больший выигрыш, чем замена Biome или формата pytest-конфигурации.

### 2. Устранить дублирование pytest-конфигурации

Сейчас настройки находятся одновременно в:

```text
pytest.ini
sidecar/pyproject.toml → [tool.pytest.ini_options]
```

Фактически pytest использует только `pytest.ini`.

Проверен вариант с единым источником конфигурации:

```bash
uv run --project sidecar pytest \
  -c sidecar/pyproject.toml tests/python -q
```

Результат:

```text
44 passed
```

Рекомендация: удалить `pytest.ini`, оставить настройки в `sidecar/pyproject.toml` и явно передавать `-c` через `justfile` и `package.json`.

### 3. Перевести Dependabot с pip на uv

Сейчас Python dependencies обновляются через:

```yaml
package-ecosystem: pip
```

GitHub и Astral уже поддерживают `uv.lock` напрямую:

```yaml
package-ecosystem: uv
```

Рекомендуется использовать uv ecosystem и при необходимости добавить cooldown, совместимый с lock policy проекта.

### 4. Обновить и закрепить GitHub Actions

Текущие workflows используют устаревшие major versions и показывают Node 20 deprecation warnings внутри Actions.

Dependabot уже предлагал обновления:

```text
actions/checkout:                v5 → v7
actions/setup-node:              v4 → v7
extractions/setup-just:          v3 → v4
googleapis/release-please-action v4 → v5
j178/prek-action:                2.0.1 → 2.0.5
```

Для write-capable release workflow рекомендуется закреплять Actions по immutable commit SHA, сохраняя version comment для Dependabot.

### 5. Удалить лишний Node setup из package smoke

После перехода `release-check` на Bun package matrix всё ещё выполняет `actions/setup-node`, хотя job больше не использует Node напрямую.

Этот шаг можно удалить. `Sync TypeScript environment` пока нужен, потому что package smoke импортирует project sources и их dependencies.

### 6. Ограничить Biome реальными project paths

Сейчас Biome использует:

```json
"includes": ["**"]
```

Из-за этого он заходил в generated/runtime directories, включая `.gitnexus` и `.operator`.

Нужно либо перечислить реальные source/config paths, либо явно исключить generated directories. Сам Biome менять не требуется: на этом репозитории полный `biome check` быстрее комбинации `oxlint + oxfmt`.

### 7. Зафиксировать Python 3.13 во всём проекте

Решение принято: проект поддерживает Python 3.13. Нужно выровнять под него всю конфигурацию:

```text
sidecar requires-python: >=3.13
Ruff target-version:     py313
mypy python_version:     3.13
ty python-version:       3.13
dev/package runtime:     3.13
CI tests:                3.13
```

Python 3.12 не нужно сохранять как заявленную support surface или добавлять для неё отдельную CI matrix.

## Дополнительный cleanup

- Удалить `bootstrap-sha` из Release Please configuration: первый release уже состоялся.
- Удалить `.npmignore`, если `package.json.files` полностью задаёт production artifact.
- Удалить из `tsconfig.json` неиспользуемые при `noEmit: true` параметры `outDir`, `rootDir` и `declaration`.
- Убрать дублирование mypy targets между `sidecar/pyproject.toml` и `justfile`.
- Расширить mypy и ty на все модули в `tests/python`, сохранив `sidecar` в coverage и устранив дублирование target lists.
- Установить `indent_size = 4` для `justfile` в `.editorconfig`.
- Обновить patch/minor tooling dependencies: Biome, Pi packages, Ruff, mypy, ty и prek.
- Проверить pytest 9 отдельным focused upgrade, не смешивая major migration с остальным cleanup.
- Параллелить независимые TypeScript и Python gates внутри `just`; `just check` должен остаться единым локальным и CI entrypoint.

## Отложенный follow-up

Локальный uv обновлён до `0.11.29`. В package и CI пока остаётся `0.8.13`, потому что используемый npm mirror `@manzt/uv` также остановился на этой версии. Этот вопрос исключён из текущего плана, но его нельзя забывать: отдельно выбрать актуальный способ доставки uv пользователям package, затем обновить pin и добавить `required-version` для contributor environment.

## Не требует изменений

Внутренняя версия `pi-codemcp-sidecar` `0.1.0` не обязана совпадать с npm package version. Sidecar не публикуется отдельно (`tool.uv.package = false`), поэтому это только внутреннее project metadata и не создаёт versioning contract для пользователей.

## Что стоит оставить

Без смены технологии стоит оставить:

- Bun test и Bun package management;
- uv;
- Ruff;
- pytest и pytest-asyncio;
- mypy вместе с ty, пока ty не стабилизируется;
- prek;
- just;
- Biome;
- Release Please;
- npm Trusted Publishing через OIDC;
- package smoke на Linux, macOS и Windows.

Главный принцип следующего этапа: сначала убирать дублирование и лишние последовательные прогоны, затем обновлять версии, и только после этого рассматривать замену инструментов.
