"""Ralph orchestrator — autonomous self-improvement loop driving OpenCode.

Submodules:
  runner       async subprocess runner (spawn opencode, hard timeout, parse JSON events)
  verify       orchestrator-owned verification gate (runs tests, not the agent)
  git_ops      git isolation: work branch, snapshot, revert (never touches base branch)
  budget       token/$ governor (per-cycle + per-day)
  guardrails   loop/no-progress/diminishing-returns detectors + kill switch
  artifacts    manage backlog.md, reflections.md, skill index in the target repo
  prompts      prompt templates for reflect/plan/execute phases
  controller   the state machine tying it all together
"""
