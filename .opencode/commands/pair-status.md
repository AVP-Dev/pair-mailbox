---
description: Show who holds write authority, the task, the phase, and any unread peer messages
---

Report the two-agent collaboration state to the user. Read this, then call `pair_state` for the
authoritative values and the unread count.

Shared state:

!`bin/pairctl status 2>/dev/null || echo "pair-mailbox state not initialised — run bin/pairctl task and bin/pairctl assign"`

Recent activity:

!`bin/pairctl log --limit 8 2>/dev/null || true`

Then, in three or four lines for the user:

1. Who holds write authority, and whether that is you.
2. The task and the current phase.
3. Whether the peer has unread messages or has sent anything recent.

If authority is `null`, say plainly that neither agent may edit until the user assigns it, and give
them the exact command. Do not assign it yourself — that is the user's call.