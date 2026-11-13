# pstack model configuration. One line per role. Delete a line to fall back to the skill default.
# `inherit-parent` or `auto` as a value: the role runs on the parent chat model (omit Agent `model`). Alias entries in a panel list still count toward its fan-out.
# budget: small (medium)
# Implementation roles run on opus medium. If a change is hard (cross-cutting design, concurrency, subtle algorithms), escalate it to the `hardest tasks` role (fable), or consult the fable advisor when the session has one.
feature, refactoring: opus medium
bug-fix: opus medium
perf-issue: opus medium
hillclimb: opus medium
judgment and prose: opus medium
hardest tasks: fable medium
how explorer: inherit-parent
how explainer: opus medium
why investigators: inherit-parent
why synthesizer: opus medium
reflect tooling: sonnet medium
reflect judgment, divergent, synthesizer: sonnet medium
arena runners: opus medium, inherit-parent
arena cross-judge pool: opus medium, inherit-parent
swarm workers: inherit-parent
architect runners: opus medium, inherit-parent
interrogate reviewers: opus medium, inherit-parent
