# session-finalizer

Указатель по скиллу финализации сессии. Контракты живут в `SKILL.md`,
скриптах и тестах — здесь их нет, этот файл их не дублирует.

## Как вызвать

- Пользователь: `/finish`, `/finish --check`, `/finish --cleanup`, `/finish --yes`.
  Флага `--force` не существует. Подробнее — `SKILL.md`, разделы 1–2.
- Не путать с `./finish.sh` в корне чекаута: это шелл-лончер парной сессии,
  к протоколу закрытия отношения не имеет (`SKILL.md`, дисклеймер в шапке).

## Где что лежит

Все пути — от корня чекаута `umputum`:

| Путь | Что это |
|---|---|
| `skills/session-finalizer/SKILL.md` | Контракт: триггеры, режимы, протокол, safety, идемпотентность, отчёт, скрипты, pair-mailbox |
| `skills/session-finalizer/scripts/finalize-check` | Аудит состояния репо/сессии: `cwd=<repo>`, без аргументов, stdout = check.json. Не пишет в git/маркер/worktrees/файлы скилла; детектированные команды проекта исполняет реально (см. `SKILL.md` §2) |
| `skills/session-finalizer/scripts/finalize-plan` | Чистая функция: `finalize-plan <check.json>`, stdout = plan.json |
| `skills/session-finalizer/scripts/finalize-apply` | Единственный писатель: `finalize-apply <plan.json> [--yes]`, stdout = apply.json |
| `skills/session-finalizer/scripts/finalize-report` | Отчёт и маркер: `finalize-report <check.json> <apply.json> [plan.json]` |
| `skills/session-finalizer/lib/git.sh` | Read-only git-хелперы (sourced, не исполнять) |
| `skills/session-finalizer/lib/json.sh` | JSON-однострочники на python3 stdlib (без jq) |
| `skills/session-finalizer/adapters/agent.sh` | Реестр агентов, артефакты сессии, порядок закрытия (`list\|artifacts\|close\|order`) |
| `skills/session-finalizer/adapters/hosting.sh` | Доказательства base-ветки и PR через `gh`, затем `glab`, иначе UNKNOWN |
| `skills/session-finalizer/adapters/commands.sh` | Детект команд `test/lint/build/typecheck` (`detect <repo>`) |
| `skills/session-finalizer/config/defaults.json` | Конфиг: маркер, protected-ветки, temp-паттерны, слоты проверок, close_order |
| `skills/session-finalizer/tests/run.sh` | Раннер сюиты; `tests/test_*.sh` — кейсы, `tests/lib.sh` — общие хелперы, `tests/fixtures/` — фикстуры |

## Проверка

```sh
skills/session-finalizer/tests/run.sh
```

Зелёный прогон: все тесты зелёные (факт — в выводе раннера `tests/run.sh`;
число растёт с сюитой, поэтому здесь не фиксируется). Раннер охраняет рабочую копию: `git status` и `HEAD`

## Как подключить агента / провайдера / команду

Ядро править не нужно:

- Новый агент: добавить ключ в `cli-profiles.json` (верхний уровень, без
  префикса `_`) либо в `agents_config_overrides` в `config/defaults.json`.
  Неизвестные имена консервируются автоматически (PRESERVE).
- Новый хостинг-провайдер: расширить `adapters/hosting.sh` (контракт:
  отсутствие/падение провайдера = UNKNOWN, никогда успех).
- Новая команда проверки: расширить матрицу детекта в `adapters/commands.sh`;
  слоты перечислены в `checks_slots` в `config/defaults.json`.
- Порядок закрытия ролей: `close_order` в `config/defaults.json`.

## Install-конвенция

`install.sh` (`write_skills`) развозит по профилям только `SKILL.md` — по
одному файлу на скилл. Скрипты, адаптеры, конфиг и тесты живут в чекауте и
исполняются оттуда; оркестратор вызывает их по путям из таблицы выше.
Расширение веера install.sh — сознательное «нет» (архитектурное решение).

## Допущения

Закрыты решением архитектора:

- UNKNOWN = PRESERVE; merged-доказательства только через git; protected-ветки
  никогда не трогаем; `--check` трёхуровнево честен (grep-gate без write-глаголов в коде +
  tree_sha-guard чистоты дерева в `tests/run.sh`/`test_check.sh` + проектные команды только
  из детект-матрицы `adapters/commands.sh`); `--yes` не пробивает PRESERVE;
  `--force` не существует; маркер пишет только `finalize-report` (при успехе) — не `apply`.

Остаются открытыми:

- Детект проектных команд покрывает `package.json` / `Makefile` / python /
  `go.mod` / `Taskfile`; экзотические сборочные системы дадут UNKNOWN-слоты,
  их проверки оркестратор выполняет вручную.
- Хостеры кроме `gh`/`glab` дают UNKNOWN — PR-доказательства тогда ручные.
- Почтовый drain и перенос write-права — ручная работа оркестратора через
  pair-инструменты (`pair_read`, `pairctl`); адаптер `close` только фиксирует
  заметку.
