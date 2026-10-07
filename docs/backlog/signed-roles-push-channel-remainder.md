---
title: подписи ролей и push-канал — остаток после ключей (п.2–6)
found: 2026-10-07
severity: high
parent: signed-roles-push-channel.md (п.1 done в раунде 34)
---

## Где

`mcp/pair_mailbox/server.py` — `_drain` (проверка подписи), `Store` (цепь
транскрипта), `on_resources_subscribe` / `publish_resource_update`
(push-ускоритель).

## Что осталось

Вынесено из родительской карточки после выполнения п.1 (ключи: `pairctl keys
gen`, `mcp/pair_mailbox/pairctl_authority.py`):

2. **Подписанный конверт.** Каждое письмо/вердикт/evidence —
   `{seq,from,to,ts,body_sha, key_id, sig}`. Чтение уже проверяет подпись
   fail-open (`server.py:2397`); довести до полного конверта на запись.
3. **Цепочка транскрипта.** Каждая строка — `prev_hash` предыдущей. Ротация уже
   продолжает цепь через `transcript-archive/` (`server.py:960`).
4. **Push-дочистка.** Ресурсы и подписка уже есть (`server.py:3817,3830`),
   сервер шлёт `notifications/resources/updated` на каждый `send`. Осталось:
   переподписка клиента на обрыв; push — только ускоритель, гарантия — mailbox.
5. **Singleton сервера на роль.** `flock` на `.pair/.server-<role>.lock`.
6. **Мультипроект.** Ключ и state строго per-project (walk-up в `serve.sh` уже
   есть): письмо чужого проекта своим ключом не верифицируется.

## Порядок

Конверт → chain → singleton → push-дочистка. Каждый пункт — свой коммит со
своими hermetic-тестами.

## Не делать

Свой сокет/демон/шину мимо MCP. Push — ускоритель, гарантия — mailbox (§7).
