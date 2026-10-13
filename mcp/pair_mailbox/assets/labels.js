/* Journal event-kind dictionary: machine name -> human word.
 *
 * A separate file, not an object inside `board.js`, because this is data, not
 * behaviour: the dict changes when a new event kind lands in the journal, and the
 * render code must never be touched for that. The snapshot knows no words on
 * principle — it owns facts, and translating facts into human language is the
 * client job.
 *
 * Order runs by human importance, not alphabet: the human looks for the pane
 * failure first. No unknown kind lives in the dict, and the client shows its name
 * as is: inventing meaning for an undescribed event is banned.
 */
window.PAIRBOARD_LABELS = {
  pane_collision: "pane collision",
  pane_dead: "pane dead",
  pane_address_canonical: "pane: canonical address",
  role_collision: "role collision",
  deliver_refused_foreign_pane: "delivery refused: foreign pane",
  ring_refused_foreign_pane: "ring refused: foreign pane",
  refusal: "refusal",
  pilot_restart_failed: "pilot: restart failed",
  pilot_restart_impossible: "pilot: restart impossible",
  pilot_restart_throttled: "pilot: restart throttled",
  pilot_pane_dead: "pilot: pane dead",
  pilot_pane_missing: "pilot: no pane",
  pilot_pane_shared: "pilot: pane shared",
  pilot_stale_role: "pilot: role stale",
  pilot_wake_failed: "pilot: wake failed",
  pilot_wake_pending: "pilot: waits for wake-up",
  pilot_wake_damaged: "pilot: wake file damaged",
  pilot_wake_acknowledged: "pilot: wake acknowledged",
  pilot_unblock: "pilot: unblocked",
  pilot_unblock_refused: "pilot: unblock refused",
  pilot_report: "pilot: report",
  pilot_deliver: "pilot: delivered",
  pilot_own_pane: "pilot: own pane",
  state_corrupt: "state corrupt",
  damaged: "journal line damaged",
  transcript_rotated: "journal archived",
  evidence: "evidence",
  human_ok: "human ok",
  advance: "phase",
  claim: "authority claim",
  release: "authority release",
  assign: "assignment",
  mode: "mode",
  autonomy: "autonomy",
  task_added: "task added",
  task_closed: "task closed",
  task: "tasks",
  patch_reserve: "patch reserved",
  patch_seal: "patch sealed",
  patch_verify: "patch verified",
  patch_applied: "patch applied",
  patch_dropped: "patch dropped",
  patch_release: "patch released",
  obligation_opened: "obligation opened",
  obligation_answered: "obligation answered",
  deliver_busy: "delivery: composer busy",
  deliver: "pane insert",
  send: "letter",
  note_rotated: "note rotated",
  note: "note",
  doorbell: "doorbell",
  roles: "roster",
  roster_pruned: "roster pruned"
};