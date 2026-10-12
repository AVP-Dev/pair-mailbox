---
name: session-finalizer
description: Finalize a coding session via /finish — close the protocol loop implement→review→finalize with verified cleanup and a final report. Use when the user says "/finish", "finalize session", or "close protocol implement→review→finalize".
---

> Язык: русское зеркало, оригинал — SKILL.md.

# Финализатор сессии / Session Finalizer

> Нормативен английский оригинал. При расхождении RU и EN версий следуй EN.
> The English original is normative; on RU/EN divergence follow EN.

Ты закрываешь сессию, а не «прибираешься». Твоя работа — доказать, что
репозиторий в чистом состоянии, и выдать финальный отчёт. Нет доказательств —
нет закрытия.

You close the session, you don't "tidy up". Prove the repo is clean, then
emit the final report. No proof — no close.

**Оркестратор (orchestrator)** — держатель вызова `/finish`, тот кто исполняет
протокол в этом чате. Не роль в реестре, а функция: выполняет шаги 1–7,
читает почту до закрытия, ставит маркер через скрипты.

**Дисклеймер про `./finish.sh`.** В корне чекаута `umputum` лежит
исполняемый `./finish.sh` — шелл-лончер парной сессии (ставит навыки,
поднимает пару и супервизора). Это другое: он не знает протокола
check→plan→apply→report и не ставит маркер. Команда `/finish` ниже —
протокол этого скилла; `./finish.sh` его не заменяет.

## 1. Триггеры / Triggers

- `/finish` — полный цикл: check → plan → confirm → cleanup → final-verify → close.
- `/finish --check` — только аудит. Read-only по состоянию репо/сессии: не пишет в git, маркер, worktrees, файлы скилла. НО исполняет детектированные команды проекта (см. §2): они идут реально и могут писать свои штатные артефакты.
- `/finish --cleanup` — аудит + план + применение (cleanup без финального закрытия).
- `/finish --yes` — пропускает **только** подтверждение cleanup-плана. Safety-проверки выполняются всегда.
- Флага `--force` **не существует**. Не выдумывай его, не принимай от пользователя.

## 2. Режимы / Modes

| Вызов / Invocation | Что делает / What it does |
|---|---|
| `/finish --check` | Аудит состояния репо/сессии. Не пишет в git, `.session/`, `.pair/`, маркер, worktrees, файлы скилла. Детектированные команды проекта (`test/lint/build/typecheck` из `adapters/commands.sh`: `npm`/`make`/`pytest`/`go`/`task`, без install/publish) исполняются реально (`bash -c`, timeout 120) — их штатные записи фиксируются в `log_tail`, это не нарушение read-only. |
| `/finish --cleanup` | Аудит + план + применение плана. Без final-verify и без закрытия (маркер не ставится). |
| `/finish` | Полный цикл: check → plan → confirm → cleanup → final-verify → close. |
| `/finish --yes` | Пропускает **только** подтверждение плана. BLOCKED не снимает, safety-правила не отменяет. |

## 3. Протокол / Protocol

Строгий порядок, 7 шагов:

1. **Verification.** Прогони `finalize-check` (cwd = корень репо). Зафиксируй HEAD, ветку, маркер.
2. **Cleanup.** `finalize-plan` (чистая функция от check.json) → показать план человеку → `finalize-apply` (без `--yes` это dry-run: ничего не удаляет).
3. **Final verify.** Повтори `finalize-check` на новом HEAD. Чисто — дальше. Грязно — новый план, не закрытие.
4. **Reviewer report.** Вердикт ревьюера: `OK` или `BLOCKED` с причиной и `file:line`.
5. **Implementer report.** Что сделано: коммиты, файлы, что осталось.
6. **Orchestrator cleanup.** Временные репозитории, scratch-директории, фоновые процессы — остановить и удалить, каждое с подтверждением вывода.
7. **Stop.** `finalize-report` на успешном apply при чистом дереве пишет маркер `.session/finalized` и печатает отчёт. Первая строка — `SESSION FINALIZED` либо `SESSION BLOCKED`.

Инварианты: reviewer-BLOCKED останавливает cleanup и close (только безопасное — документирование); single-writer — почту и маркер трогает один держатель за раз; закрытие поверх непрочитанной почты запрещено.

## 4. Правила безопасности / Safety Rules

1. Не распознал состояние — сохрани. **UNKNOWN = PRESERVE.** Пометь `?`, иди дальше.
2. Доказательство merged — только через git (`git branch --merged`, `git log`, статус PR через `gh`/`glab`). Нет доказательства в git — считай unmerged, сохраняй.
3. Никогда не пушь в защищённые ветки (`main`, `master`, `develop`, `trunk`, `release/*`, `hotfix/*`). Никогда не force-push. Не переписывай чужую историю.
4. Не трогай секреты в репо (`*secret*`, `*.pem`, `*.key`, `.env`, `*credential*` из `secrets_guards`). Не коммить секреты. Вне репо (`~/.config`, `~/.ssh`, домашние данные пользователя) — вне скоупа скилла: скрипты работают только с repo-relative путями и туда не ходят.
5. Незакоммиченная работа и именованный stash — это работа, не мусор: только с явным подтверждением человека.
6. `--check` не пишет в git/маркер/worktrees/файлы скилла (но исполняет проектные команды — см. §2). `--yes` не отменяет правила 1–5 и не снимает BLOCKED.
7. Каждое удаление — с доказательством: что, где, почему безопасно, вывод команды / exit-код.

## 5. Идемпотентность / Idempotency

Маркер `.session/finalized` — JSON `{head, session, closed_at}`; пишет его только
`finalize-report` и только при успехе (apply не заблокирован И дерево чисто).

- Маркер существует и `marker.head == HEAD` — делать нечего: ответь `Nothing to clean`, репозиторий не трогай.
- HEAD отличается — новая работа после закрытия: полный цикл заново, маркер перезаписывается после успешного final-verify.
- `--check` наличие маркера игнорирует: маркер — не разрешение, аудит всегда полный.

## 6. Финальный отчёт / Final Report

Первая строка — всегда одна из двух: `SESSION FINALIZED` | `SESSION BLOCKED`.

```text
SESSION FINALIZED | SESSION BLOCKED

Task:          что требовалось закрыть
Implementation: что сделано (коммиты, файлы)
Git:           ветка, HEAD, статус push/PR
Issues&PR:     открытые/закрытые issues и PR со ссылками
Cleanup:       что удалено, с доказательством на пункт
Final state:   вывод final-verify (git status чист)
Agents:        кто участвовал и с каким вердиктом

STATUS: COMPLETE | BLOCKED
```

При BLOCKED вместо `STATUS: COMPLETE`:

```text
STATUS: BLOCKED
Reason:                почему нельзя закрыть (с file:line)
Cleanup performed:     что безопасно сделано
Cleanup NOT performed: чего НЕ делали и почему
Action required:       что должен сделать человек
```

Маркировка строк: `✓` — подтверждено выводом команды/exit-кодом (не видел вывода — не ставишь); `!` — PRESERVE-решения и проблемы; `?` — UNKNOWN. Отчёт без секций Cleanup и Final state — не отчёт.

## 7. Вызов скриптов / Running the scripts

CLI позиционный, без флагов под входы. `cwd` каждого вызова — корень целевого репо. Оркестратор исполняет скрипты из чекаута скилла (пути ниже от корня `umputum`):

```sh
skills/session-finalizer/scripts/finalize-check > check.json
skills/session-finalizer/scripts/finalize-plan check.json > plan.json
skills/session-finalizer/scripts/finalize-apply plan.json         # dry-run: ничего не удаляет
skills/session-finalizer/scripts/finalize-apply plan.json --yes  # реальное применение
skills/session-finalizer/scripts/finalize-report check.json apply.json [plan.json]
```

Каждый скрипт отвечает на `--help`. Коды выхода: `0` — ок (включая `blocked:true`), `2` — неверный вызов/вход, `1` — внутренняя ошибка. Удаляет только `finalize-apply --yes` (и только SAFE после перепроверки); файл маркера создаёт только `finalize-report`. `install.sh` развозит по профилям только `SKILL.md` (`write_skills` копирует один файл на скилл) — скрипты живут в чекауте и исполняются оттуда; веер install.sh сознательно не расширен.

## 8. Интеграция pair-mailbox / Pair-mailbox integration

Порядок закрытия ролей — `close_order` из `config/defaults.json` (`critic, explorer, writer`). Реестр ролей (факт `adapters/agent.sh`): `cli-profiles.json` → `agents_config_overrides` из `config/defaults.json` → `.session/finalizer.json` (`session_config` в конфиге, ключ `agents`). Имя вне всех трёх — unknown: его файлы PRESERVE, закрытие помечается соответственно. `state.roles` код не читает — реестр строится из файлов, а не из живого состояния.

- Single-writer: пишет один держатель права за раз; право двигается только человеком через `pairctl`, никогда сообщением.
- Почту прочитать до close: сначала drain mailbox (`pair_read`), потом закрытие. BLOCKED любой роли останавливает close.
- Адаптер `adapters/agent.sh close <role>` — no-op заметка (ничего не пишет, подтверждает оркестратор через pair-инструменты); unknown-роль — exit 3, PRESERVE.
