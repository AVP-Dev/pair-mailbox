# server-py-still-a-monolith

`server.py` 3567 строк — вынос `wake`/`review`/`panes`/`digest`/`liveness`/`roster_heal`
снял ~600, но ядро `PairSession` (send/note/advance, obligations, verdicts, patches)
по-прежнему монолит. `test_protocol.py` 3740 — 268 инвариантов в одном файле.
`bin/pairpilot` 1440 — тик-цикл, доставка, эскалация, профили в одной куче.

Разбить дальше:
1. `server.py` → вынести obligations (`_open_obligation`, `_answer_obligations`,
   `pending_answers`) в `obligations.py`, verdict-pin в `review.py` рядом со snapshot;
2. `test_protocol.py` → разнести по выносимым модулям (как в раунде 15 ушли CLI-тесты);
3. `bin/pairpilot` → разрез тик-цикла и доставки (долг с раунда 21, зафиксирован
   в baseline: «План разделения по-прежнему нет, и это долг»).
