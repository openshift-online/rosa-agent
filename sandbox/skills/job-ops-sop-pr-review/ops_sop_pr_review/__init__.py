"""ops_sop_pr_review - deterministic worklist builder for job-ops-sop-pr-review.

Everything in this package is judgment-free: enumerating PRs, computing age/
label/eligibility, detecting failing CI (excluding tide), finding who applied
a label, and templating ping-comment bodies. It performs no non-deterministic
inference - that stays with the invoking skill (an agent), which uses this
package's output as a precomputed worklist and only supplies the genuinely
non-deterministic parts: Section-5 source verification against upstream code
and the prose review recommendation.
"""

__version__ = "0.1.0"
