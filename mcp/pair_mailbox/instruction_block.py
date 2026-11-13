"""Versioned installer-owned instructions; preserve the user's surrounding text."""
MARK = "<!-- pair-mailbox -->"
END = "<!-- /pair-mailbox -->"
NOTE = """
## Pair Work (explicit sessions only)

Ordinary CLI sessions work independently, including native subagents. An installed
MCP server, a .pair directory or inherited PAIR_* values do not make you a critic.
Apply the pair protocol only when this CLI was explicitly launched into a pair
and the MCP initialization confirms a paired session. In standalone mode, perform
the user's task normally; no pair_state, claim, peer or mailbox is required.
Native subagents perform their delegated task and report to their parent; they
do not claim a separate pair role or negotiate with the parent's peer.

In an enrolled pair, call pair_state then pair_claim and respect the assigned
write boundary. If the connection fails, repair it while retaining your role;
a disconnected critic does not gain write authority. Use pair_* to communicate
directly with the peer. The user never relays messages between agents.
"""

# Exact shipped v1 body: changing NOTE alone used to leave this rule behind.
LEGACY_NOTE = """
## Pair Work

A peer agent is available nearby via the `pair_*` tools.
Before taking on a task, call `pair_state`, then `pair_claim`.

- Claimed authority — you are the writer: edit files, run tests, commit, open PRs.
- Did not claim — you are the critic: read, argue, write findings to `pair_note`, never edit files.

The round can be reopened either way: `pair_advance("implement")` rolls the phase back.
That needs no write authority. If the writer went silent — tell the user.

If the peer is silent — work alone, that is not a blocker.
Full rules: `docs/PROTOCOL.md` in the pair-mailbox repository.
"""
LEGACY_RU_NOTE = """## Парная работа

Рядом доступен соседний агент через инструменты `pair_*`.
Прежде чем браться за задачу, вызови `pair_state`, затем `pair_claim`.

- Захватил авторитет — ты писатель: правь файлы, гоняй тесты, коммить, открывай PR.
- Не захватил — ты критик: читай, спорь, пиши находки в `pair_note`, файлы не правь.

Раунд можно переоткрыть в любую сторону: `pair_advance("implement")` откатит фазу назад.
Это не требует правы записи. Если писатель ушёл молча — скажи об этом пользователю.

Если сосед молчит — работай один, это не блокер.
Правила целиком: `docs/PROTOCOL.md` в репозитории pair-mailbox.
"""


def replace_owned_block(text: str, body: str = NOTE) -> str:
    head, marker, tail = text.partition(MARK)
    if marker:
        if END in tail:
            _, _, tail = tail.partition(END)
        elif tail.startswith(LEGACY_NOTE.rstrip("\n")):
            tail = tail[len(LEGACY_NOTE.rstrip("\n")):]
            if tail.lstrip("\n").startswith(LEGACY_RU_NOTE.rstrip("\n")):
                tail = tail.lstrip("\n")[len(LEGACY_RU_NOTE.rstrip("\n")):]
            elif tail.lstrip("\n").startswith("## Парная работа"):
                raise ValueError("modified legacy pair instructions: review the Russian block before migration")
        elif "pair_state" in tail and "pair_claim" in tail:
            raise ValueError("modified legacy pair instructions: preserve and review the block before migration")
    else:
        tail = ""
    prefix = head.rstrip() + "\n" if head.strip() else ""
    suffix = "\n\n" + tail.lstrip("\n") if tail.strip() else "\n"
    return prefix + MARK + body.rstrip() + "\n" + END + suffix
