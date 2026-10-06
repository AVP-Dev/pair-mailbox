"""Власть человека над раундом: кто пишет, какой режим, чекпоинт, леджер задач.

Вынесено из `server.py` отдельной ответственностью: всё здесь МЕНЯЕТ состояние
пары, и ничего здесь не читает ради показа. Соседние модули устроены наоборот:
`pairctl_read.py` только читает, `pairctl_panes.py` работает с привязкой панелей.

Почему это отдельный файл, а не «ещё функция в `server.py`»: право записи —
ровно та граница, ради которой существует весь протокол. Код, который его
двигает, должен читаться целиком за один проход, без того чтобы продираться
через MCP-слой, где живут `pair_state` и `pair_send`. Когда он лежит рядом с
ними, «кто сейчас пишет» отвечается чтением всего файла.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys

from server import (
    AUTONOMIES,
    MODES,
    OK_PHASES,
    OPEN_TASK,
    PairSession,
    ProtocolError,
    Store,
    autonomy_of,
    global_roles_path,
    next_task_id,
    open_tasks,
    permissions_path,
    render_tasks_md,
    resolve_autonomy,
    utcnow,
    write_tasks_md,
)


def cmd_status(store: Store, args: argparse.Namespace) -> int:
    state = store.read()
    session = PairSession("pairctl", store)
    print(json.dumps(
        {
            "state_dir": str(store.root),
            "task": state.get("task", ""),
            "mode": state.get("mode", "fix"),
            "autonomy": autonomy_of(state),
            "writer": state.get("writer"),
            "policy": state.get("policy"),
            "phase": state.get("phase"),
            "round": state.get("round"),
            "human_ok": session.human_ok_for_round(state),
            "evidence_this_round": len(session.round_evidence(state)),
            "unresolved_patches": [item["path"] for item in session.unresolved_patches(state)],
            "closing_blocked_by": session._done_blockers(state),
            "permission_allowlist": str(permissions_path(store.root)),
            "roles": {name: info.get("last_seen") for name, info in state.get("roles", {}).items()},
        },
        indent=2,
        ensure_ascii=False,
    ))
    return 0


def cmd_assign(store: Store, args: argparse.Namespace) -> int:
    role = args.role
    known = set(store.read().get("roles", {}))
    if known and role not in known:
        print(f"warning: {role!r} has never connected; registered roles: {', '.join(sorted(known))}")

    def apply(state):
        state["writer"] = role
        # решение человека — повод поставить метку «выбрано человеком», чтобы
        # в переписке было видно, что это не гонка
        state["writer_reason"] = "назначено человеком"
        if state.get("closed_at") or state.get("phase") == "done":
            state.pop("closed_at", None)
            state["phase"] = "implement"
            state["round"] = int(state.get("round", 0)) + 1
        elif state.get("phase") == "scoping" and not state.get("task"):
            state["round"] = int(state.get("round", 0)) + 1
    store.mutate(apply)
    store.append_transcript("assign", "human", writer=role,
                            reason="назначено человеком")
    print(f"write authority assigned to {role}")
    return 0


def cmd_roles(store: Store, args: argparse.Namespace) -> int:
    # Постоянная расстановка: кто всегда пишет, кто всегда ревьюит.
    # Гонка остаётся как запасной вариант, но по умолчанию роли известны
    # заранее, и раунд начинается с работы, а не с торговли правами.
    policy_path = global_roles_path()
    state = store.read()
    current = state.get("policy") if isinstance(state.get("policy"), dict) else {}
    wanted = dict(current)

    if args.clear:
        wanted = {}
    else:
        if args.writer:
            wanted["writer"] = args.writer
        if args.critic:
            wanted["critic"] = args.critic
        if args.swap:
            was_writer, was_critic = wanted.get("writer"), wanted.get("critic")
            if was_writer or was_critic:
                wanted["writer"] = was_critic
                wanted["critic"] = was_writer

    if not wanted:
        store.mutate(lambda st: st.pop("policy", None))
        store.append_transcript("roles", "human", policy=None)
        print("постоянная расстановка снята, роли снова определяются гонкой")
        return 0

    if wanted.get("writer") == wanted.get("critic"):
        print("error: писатель и критик не могут быть одной ролью", file=sys.stderr)
        return 2

    store.mutate(lambda st: st.__setitem__("policy", wanted))
    store.append_transcript("roles", "human", policy=wanted, source=str(policy_path))

    # Расстановка сразу применяется к текущему раунду: назначенный писатель
    # получает право, иначе право остаётся у прошлого держателя и работа
    # стоит до вмешательства человека.
    holder = state.get("writer")
    if wanted.get("writer") and holder != wanted["writer"]:
        def apply(st):
            st["writer"] = wanted["writer"]
            st["writer_reason"] = "назначено человеком постоянной расстановкой"
        store.mutate(apply)

    if args.global_:
        policy_path.parent.mkdir(parents=True, exist_ok=True)
        policy_path.write_text(json.dumps(wanted, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"сохранено для любых проектов: {policy_path}")
    print(f"расстановка: писатель {wanted.get('writer') or '—'}, критик {wanted.get('critic') or '—'}")
    return 0


def cmd_release(store: Store, args: argparse.Namespace) -> int:
    def apply(state):
        state["writer"] = None
        state["writer_reason"] = ""
    store.mutate(apply)
    store.append_transcript("release", "human")
    print("write authority cleared; nobody holds it")
    return 0


def cmd_mode(store: Store, args: argparse.Namespace) -> int:
    mode = args.mode
    if mode not in MODES:
        print(f"error: unknown mode {mode!r}; expected one of {', '.join(MODES)}", file=sys.stderr)
        return 2
    previous = store.read().get("mode", "fix")
    store.mutate(lambda state: state.__setitem__("mode", mode))
    store.append_transcript("mode", "human", mode=mode, previous=previous)
    print(f"round mode is now {mode!r}")
    return 0


def cmd_autonomy(store: Store, args: argparse.Namespace) -> int:
    level = args.level
    if level not in AUTONOMIES:
        print(f"error: unknown autonomy {level!r}; expected one of {', '.join(AUTONOMIES)}",
              file=sys.stderr)
        return 2
    previous = autonomy_of(store.read())
    store.mutate(lambda state: state.__setitem__("autonomy", level))
    store.append_transcript("autonomy", "human", level=level, previous=previous)
    notes = {
        "observe": "сверхвизор только сообщает: упавшего не перезапускает, разрешения не подтверждает",
        "semi": "работа идёт сама, но закрыть раунд без доказательств и без 'pairctl ok' нельзя",
        "auto": "работа идёт сама до закрытия раунда, нужен только записанный факт",
    }
    print(f"autonomy is now {level!r}: {notes[level]}")
    return 0


def cmd_launch_autonomy(store: Store, args: argparse.Namespace) -> int:
    # Выбор режима при старте, ровно один раз и до того, как агенты получили
    # инструкции. Логика здесь, а не в pair-duo, потому что у bash-обёртки нет
    # тестов: правило, которое нечем проверить, мы уже находили.
    level, source = resolve_autonomy(
        store.root, task=args.task or "", flag=args.flag,
    )
    previous = autonomy_of(store.read())
    store.mutate(lambda state: state.__setitem__("autonomy", level))
    store.append_transcript(
        "autonomy", "launcher", autonomy=level, previous=previous, via=source,
    )
    print(f"autonomy {level!r} (via {source}): "
          f"{'pair works to the end' if level == 'auto' else level}")
    return 0


def cmd_ok(store: Store, args: argparse.Namespace) -> int:
    # Одно слово человека на чекпоинте. Именно это делает semi «полуавтоматом»:
    # пара работает сама, но закрыть раунд без него не может.
    #
    # --revoke отзывает сказанное. Отзыв нужен не для удобства: человек
    # смотрит на правки, говорит «ок» и видит, что после этого изменилось ещё
    # что-то. Без отзыва единственный выход — сменить настройку или reset.
    state = store.read()
    round_number = int(state.get("round", 0))
    revoke = bool(getattr(args, "revoke", False))

    def apply(st):
        entries = st.setdefault("human_ok", [])
        same_round = [e for e in entries if int(e.get("round", -1)) == round_number]
        if same_round and bool(same_round[-1].get("revoke")) == revoke:
            return False
        entries.append({"round": round_number, "at": utcnow(), "phase": st.get("phase"),
                        "revoke": revoke})
        del entries[:-20]
        return True

    fresh = store.mutate(apply)
    store.append_transcript("human_ok_revoke" if revoke else "human_ok", "human",
                            round=round_number, phase=state.get("phase"), changed=fresh)
    if not fresh:
        print(f"round {round_number}: {'already revoked' if revoke else 'already marked'}; "
              f"nothing changed")
    elif revoke:
        print(f"ok revoked for round {round_number}; it cannot close until you say ok again")
    else:
        print(f"ok: round {round_number} marked at phase {state.get('phase')!r}")
        if state.get("phase") not in OK_PHASES:
            print(f"  note: ok counts only at {', '.join(OK_PHASES)}; "
                  f"this one will not close the round from {state.get('phase')!r}")
    return 0


def _wake_writer(store: Store, reason: str, text: str) -> None:
    # Задача от человека — событие, а не запись: без пробуждения писатель
    # узнает о ней только следующим случайным чтением. Будим держателя
    # записи; если его нет (release), будить некого — человек назначит сам.
    writer = store.read().get("writer")
    if not writer:
        return
    try:
        from wake import record_wake
        record_wake(store.root, store.read(), writer, reason, text)
        store.append_transcript("wake_pending", "human", to=writer, reason=reason)
    except Exception:  # noqa: BLE001 - wake-файл не должен ронять команду:
        # задача уже записана выше через mutate, будим best-effort, как в server.py
        pass


def cmd_task(store: Store, args: argparse.Namespace) -> int:
    # Список, а не одна строка. Новая задача дописывается: старая не исчезает,
    # и раунд не закроется, пока что-то не помечено сделанным или брошенным с
    # причиной.
    if args.done or args.drop:
        target = int(args.done or args.drop)
        want = "done" if args.done else "dropped"

        def close(st):
            match = next((i for i in st.get("tasks") or []
                          if int(i.get("id", 0)) == target), None)
            if not match:
                raise ProtocolError(f"no task #{target}")
            if want == "dropped" and not str(args.reason or "").strip():
                raise ProtocolError("--drop needs --reason")
            match["status"] = want
            match["reason"] = str(args.reason or "").strip()
            match["closed_at"] = utcnow()
            match["closed_by"] = "human"
            return match

        item = store.mutate(close)
        store.append_transcript("task_closed", "human", task_id=item["id"], status=want)
        print(f"task #{item['id']} is {want}")
        _wake_writer(store, "задача", f"[pair] human: задача #{item['id']} {want}, прочитай pair_state")
    else:
        body = " ".join(str(args.text or "").split())
        if not body:
            print("task needs text", file=sys.stderr)
            return 2

        def apply(state):
            item = {"id": next_task_id(state), "text": body, "status": OPEN_TASK,
                    "added_at": utcnow(), "added_by": "human",
                    "round": int(state.get("round", 0)), "reason": "",
                    "closed_at": None, "closed_by": None}
            state.setdefault("tasks", []).append(item)
            state["task"] = body  # для старых читателей и отчёта
            return item

        item = store.mutate(apply)
        store.append_transcript("task_added", "human", task_id=item["id"], text=body[:200])
        print(f"task #{item['id']} added: {body}")
        _wake_writer(store, "задача", f"[pair] human: новая задача #{item['id']}, прочитай pair_state")
        still = [i["id"] for i in open_tasks(store.read())]
        if len(still) > 1:
            print(f"  open tasks now: {', '.join(str(i) for i in still)}")
    return 0


def cmd_tasks(store: Store, args: argparse.Namespace) -> int:
    current = store.read()
    path = write_tasks_md(store.root, current)
    print(f"# Журнал сессии -> {path}\n")
    print(render_tasks_md(current))
    return 0


def cmd_reset(store: Store, args: argparse.Namespace) -> int:
    if not args.yes:
        print("refusing to reset without --yes", file=sys.stderr)
        return 2

    for path in (store.mailbox_root, store.notes_root, store.patches_root):
        shutil.rmtree(path, ignore_errors=True)
    store.state_path.unlink(missing_ok=True)
    store.transcript_path.unlink(missing_ok=True)
    fresh = Store(store.root)
    # Стирание состояния не должно повышать права — но права измеряются
    # относительно дефолта, а дефолт теперь auto, то есть ровно то, что человек
    # задал как норму. Ставить здесь observe было бы противоречием: wipe
    # остановил бы пару вопреки «работайте сами, вернитесь с выводом» и
    # заставил бы человека это заметить. §12 вне оси, поэтому auto не значит
    # «разрешено разрушительное».
    fresh.mutate(lambda st: st.__setitem__("autonomy", "auto"))
    store.append_transcript("reset", "human", autonomy="auto")
    print("state reset; autonomy is now 'auto' — the pair works to the end and "
          "comes back with the result. Narrow it with `pairctl autonomy semi` "
          "or `pairctl autonomy observe`.")
    return 0