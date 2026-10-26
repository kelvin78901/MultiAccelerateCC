"""MultiAccelerateCC (macc): a deterministic task-graph scheduler for Claude Code workers.

Design rule: the scheduler is a plain program. LLMs are called only as
functions (write a spec, write code, review a diff). Every decision about
*what runs next, when, and whether something is stale* is made by the graph.
"""

__version__ = "0.2.4"
