"""Task graph: the single source of truth for *what* exists and *what state* it is in.

Borrowed ideas
--------------
* Build systems (Make/Bazel): every node has an input hash; if an upstream
  output changes, downstream nodes become STALE automatically. No LLM decides this.
* Kahn's algorithm: the ready set is "PENDING and every dependency DONE".
* Critical-path scheduling (CPM / HEFT list scheduling): priority = longest
  downstream path in estimated minutes, so the thing that gates the most work runs first.
* Module ownership: two tasks that own overlapping paths never run at the same time.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Iterable, Optional


class State(str, Enum):
    PENDING = "pending"        # waiting on deps
    READY = "ready"            # deps satisfied, not yet dispatched
    RUNNING = "running"        # a worker owns it
    REVIEW = "review"          # worker finished; independent reviewer / gates pending
    DONE = "done"              # merged into integration branch, gates passed
    STALE = "stale"            # was DONE, but an upstream input changed; needs revalidation
    FAILED = "failed"          # exhausted attempts; needs planner or human
    BLOCKED = "blocked"        # explicitly waiting on a human decision
    CANCELLED = "cancelled"


class Kind(str, Enum):
    CODE = "code"              # implement something
    CONTRACT = "contract"      # define interfaces / schema / test plan; serialised before fan-out
    SPIKE = "spike"            # answer a question (hardware ok? lib works?); output is a conclusion
    REVIEW = "review"          # independent review of a diff
    INTEGRATE = "integrate"    # merge + run full gates
    MERGE = "merge"            # resolve conflict markers (synthetic, spawned by the scheduler)
    PLAN = "plan"              # planner call (synthetic, read-only)
    MANUAL = "manual"          # a human does it


TERMINAL = {State.DONE, State.CANCELLED}
ACTIVE = {State.RUNNING, State.REVIEW}


@dataclass
class Task:
    id: str
    title: str
    spec: str = ""
    deps: list[str] = field(default_factory=list)
    owns: list[str] = field(default_factory=list)       # path globs this task may write
    kind: str = Kind.CODE.value
    effort: str = "medium"                              # low|medium|high|xhigh|max
    model: Optional[str] = None                          # None -> worker default
    est_tokens: int = 50_000
    est_minutes: float = 10.0
    done_when: list[str] = field(default_factory=list)  # shell commands that must exit 0
    allow_stubs: bool = False
    max_attempts: int = 2
    # --- mutable runtime fields ---
    state: str = State.PENDING.value
    attempts: int = 0
    input_hash: str = ""
    output_hash: Optional[str] = None
    preempt: bool = False                                # set when inputs changed mid-run
    worker: Optional[str] = None
    branch: Optional[str] = None
    last_error: Optional[str] = None

    # -- helpers --
    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Task":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


class GraphError(Exception):
    pass


def _paths_overlap(a: str, b: str) -> bool:
    """Two ownership globs overlap if either matches the other or one is a prefix dir of the other."""
    if a == b:
        return True
    if fnmatch.fnmatch(a, b) or fnmatch.fnmatch(b, a):
        return True
    a_dir = a.rstrip("/*")
    b_dir = b.rstrip("/*")
    return a_dir.startswith(b_dir + "/") or b_dir.startswith(a_dir + "/")


class TaskGraph:
    def __init__(self, tasks: Iterable[Task] = ()):
        self.tasks: dict[str, Task] = {}
        for t in tasks:
            self.add(t)

    # ------------------------------------------------------------------ build
    def add(self, task: Task) -> None:
        if task.id in self.tasks:
            raise GraphError(f"duplicate task id {task.id}")
        self.tasks[task.id] = task

    def validate(self) -> None:
        for t in self.tasks.values():
            for d in t.deps:
                if d not in self.tasks:
                    raise GraphError(f"{t.id} depends on unknown task {d}")
        self.topo_order()  # raises on cycle
        # a CONTRACT task must precede (be a dep of) everything that shares its ownership
        for t in self.tasks.values():
            if t.kind == Kind.CONTRACT.value:
                for u in self.tasks.values():
                    if u.id != t.id and self.owns_overlap(t, u) and t.id not in self.ancestors(u.id):
                        raise GraphError(
                            f"contract {t.id} overlaps {u.id} but is not an ancestor of it; "
                            "contracts must be serialised before fan-out"
                        )

    # --------------------------------------------------------------- queries
    def topo_order(self) -> list[str]:
        indeg = {k: 0 for k in self.tasks}
        for t in self.tasks.values():
            for d in t.deps:
                indeg[t.id] += 1
        queue = sorted(k for k, v in indeg.items() if v == 0)
        order: list[str] = []
        children = self.children_map()
        while queue:
            n = queue.pop(0)
            order.append(n)
            for c in sorted(children[n]):
                indeg[c] -= 1
                if indeg[c] == 0:
                    queue.append(c)
        if len(order) != len(self.tasks):
            raise GraphError("cycle detected in task graph")
        return order

    def children_map(self) -> dict[str, set[str]]:
        m: dict[str, set[str]] = {k: set() for k in self.tasks}
        for t in self.tasks.values():
            for d in t.deps:
                m[d].add(t.id)
        return m

    def ancestors(self, tid: str) -> set[str]:
        seen: set[str] = set()
        stack = list(self.tasks[tid].deps)
        while stack:
            n = stack.pop()
            if n not in seen:
                seen.add(n)
                stack.extend(self.tasks[n].deps)
        return seen

    def descendants(self, tid: str) -> set[str]:
        children = self.children_map()
        seen: set[str] = set()
        stack = list(children[tid])
        while stack:
            n = stack.pop()
            if n not in seen:
                seen.add(n)
                stack.extend(children[n])
        return seen

    def deps_done(self, tid: str) -> bool:
        return all(self.tasks[d].state == State.DONE.value for d in self.tasks[tid].deps)

    def ready(self) -> list[Task]:
        """Kahn-style ready set, ordered by priority (critical path desc, cheap first)."""
        out = [
            t for t in self.tasks.values()
            if t.state in (State.PENDING.value, State.READY.value) and self.deps_done(t.id)
        ]
        out.sort(key=lambda t: (-self.critical_path(t.id), t.est_tokens, t.id))
        return out

    def critical_path(self, tid: str) -> float:
        """Longest path (in est_minutes) from this node to any sink, including itself."""
        children = self.children_map()
        memo: dict[str, float] = {}

        def rec(n: str) -> float:
            if n in memo:
                return memo[n]
            best = 0.0
            for c in children[n]:
                best = max(best, rec(c))
            memo[n] = self.tasks[n].est_minutes + best
            return memo[n]

        return rec(tid)

    def owns_overlap(self, a: Task, b: Task) -> bool:
        return any(_paths_overlap(x, y) for x in a.owns for y in b.owns)

    def conflicts_with_active(self, t: Task) -> list[str]:
        """Ids of RUNNING/REVIEW tasks whose ownership overlaps t (cannot run concurrently)."""
        return [
            u.id for u in self.tasks.values()
            if u.id != t.id and u.state in {s.value for s in ACTIVE} and self.owns_overlap(t, u)
        ]

    # ----------------------------------------------------------------- hashes
    def compute_input_hash(self, tid: str) -> str:
        t = self.tasks[tid]
        payload = {
            "spec": t.spec,
            "owns": sorted(t.owns),
            "done_when": t.done_when,
            "deps": sorted((d, self.tasks[d].output_hash or "") for d in t.deps),
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]

    def refresh_hashes(self) -> None:
        for tid in self.topo_order():
            self.tasks[tid].input_hash = self.compute_input_hash(tid)

    # ------------------------------------------------------------ transitions
    def set_state(self, tid: str, state: State) -> None:
        self.tasks[tid].state = state.value

    def mark_done(self, tid: str, output_hash: str) -> list[str]:
        """Mark done. If the output differs from the previous one, downstream becomes stale.
        Returns ids whose state changed as a consequence."""
        t = self.tasks[tid]
        changed: list[str] = []
        prev = t.output_hash
        t.output_hash = output_hash
        t.state = State.DONE.value
        t.preempt = False
        if prev is not None and prev != output_hash:
            changed = self.propagate_stale(tid)
        return changed

    def propagate_stale(self, tid: str) -> list[str]:
        """Upstream input changed. Build-system rule:
        DONE -> STALE (revalidate), RUNNING/REVIEW -> preempt flag, PENDING/READY unchanged,
        FAILED -> PENDING (worth retrying with new inputs)."""
        changed: list[str] = []
        for d in self.descendants(tid):
            u = self.tasks[d]
            new_hash = self.compute_input_hash(d)
            if new_hash == u.input_hash:
                continue
            u.input_hash = new_hash
            if u.state == State.DONE.value:
                u.state = State.STALE.value
                changed.append(d)
            elif u.state in {s.value for s in ACTIVE}:
                u.preempt = True
                changed.append(d)
            elif u.state == State.FAILED.value:
                u.state = State.PENDING.value
                u.attempts = 0
                changed.append(d)
        return changed

    def update_spec(self, tid: str, new_spec: str, **fields) -> list[str]:
        """A requirement / design change. The node itself is invalidated and so is everything downstream."""
        t = self.tasks[tid]
        t.spec = new_spec
        for k, v in fields.items():
            setattr(t, k, v)
        t.input_hash = self.compute_input_hash(tid)
        # the requirement itself changed: this node must be re-done, not merely re-validated
        if t.state in {s.value for s in ACTIVE}:
            t.preempt = True
        else:
            t.state = State.PENDING.value
            t.attempts = 0
        # its output will change, so everything below is stale too
        affected = [tid] + self.propagate_stale(tid)
        return affected

    def add_subtasks(self, parent_id: str, subtasks: list[Task]) -> None:
        """Dynamic decomposition: parent becomes an INTEGRATE node that depends on its children.
        Children inherit the parent's deps."""
        parent = self.tasks[parent_id]
        for s in subtasks:
            s.deps = sorted(set(s.deps) | set(parent.deps))
            self.add(s)
        parent.kind = Kind.INTEGRATE.value
        parent.deps = sorted(set(parent.deps) | {s.id for s in subtasks})
        parent.state = State.PENDING.value
        self.validate()
        self.refresh_hashes()

    # ------------------------------------------------------------- reporting
    def summary(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for t in self.tasks.values():
            out[t.state] = out.get(t.state, 0) + 1
        return out

    def is_finished(self) -> bool:
        return all(t.state in {s.value for s in TERMINAL} for t in self.tasks.values())

    def to_dicts(self) -> list[dict]:
        return [self.tasks[i].to_dict() for i in self.topo_order()]

    def to_mermaid(self) -> str:
        icon = {
            State.DONE.value: "✅", State.RUNNING.value: "🔄", State.REVIEW.value: "🔍",
            State.STALE.value: "⚠️", State.FAILED.value: "❌", State.BLOCKED.value: "⛔",
        }
        lines = ["graph TD"]
        for t in self.tasks.values():
            label = f"{icon.get(t.state, '⬜')} {t.id}: {t.title}"
            lines.append(f'  {t.id}["{label}"]')
            for d in t.deps:
                lines.append(f"  {d} --> {t.id}")
        return "\n".join(lines)
