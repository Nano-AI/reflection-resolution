# Reflection Resolution — Legacy Java Cleanup

Audit and fix reflection abuse (`setAccessible` etc.) across a large legacy
Java estate: two repos to start, eventually the rest.

Package roots are configured in `.env` (gitignored) -- see `.env.example`.
Nothing environment-specific is committed.

## Language decision (2026-08-19)

Signal ranking against [[Quant Dev Prep]] goals:

1. **C++** — pillar 3 of quant prep explicitly wants a performance-flavored C++ project.
   Reserved for the *real* tool (call-graph / visibility analyzer), only if the audit
   proves it's worth building. Doubles as warmup before the CSE 333 order book.
2. **Python** — fine for disposable analysis scripts. No interview signal, maximum speed.
3. **Go** — fun, but appears nowhere in the goals docs. Cut.

Rule: throwaway analysis = Python. Portfolio-grade tool = C++.

## Files

- `ROADMAP.md` — **the build plan**: architecture, milestones, verified AST/bytecode facts,
  research findings. Read this first; it supersedes the older strategy in this README.
- `METHOD.md` — **teaching reference**: nine pipeline stages, each as code → data → why → how.
- `PITFALLS.md` — **every trap with a runnable example** and the rule it implies.
- `examples/` — `decide.py` (bytecode → verdict) and `patch.py` (tree-sitter → source edit),
  both tested, plus the Java fixtures they run against.
- `.env.example` — config template. Copy to `.env` and set `COMPANY_PREFIX`.
- `.venv/` — Python env with tree-sitter + tree-sitter-java already installed.
- `reflection_audit.py` — corpus scanner, two modes.
  - Inventory: `python3 reflection_audit.py /path/to/repo-a /path/to/repo-b`
    Reports per repo: pattern totals, top suspect files, copy-paste clone check,
    reflectively-looked-up method names.
  - Java 21 triage: add `--java21 [--json sites.json]`. Resolves each site's
    target class (imports → same-package → wildcard, matching Java's own
    precedence, checked against a type index built from every repo passed) and
    buckets it as blocking / not blocking the 8 → 21 move. Pass both repos on
    one command line so cross-repo targets resolve.
  - `examples/java21/` — fixture covering all eight buckets. Run
    `python3 reflection_audit.py --java21 --company com.example examples/java21`
    to see expected output: 4 blockers, 3 in the human queue.

## Plan

1. [ ] Run audit on both repos, save reports here (`audit-repo-a.txt`, `audit-repo-b.txt`)
2. [ ] Read the top suspect file end to end; classify into buckets
       (one-off hack / serialization plumbing / third-party workaround / config factory)
3. [ ] Pick beachhead repo (smaller suspect surface), classify all its sites in a table
4. [ ] First fix PR: one trivial one-off hack, characterization test first
5. [ ] ArchUnit guardrail in CI: ban `setAccessible` outside allowlisted packages
6. [ ] Decide: is the C++ call-graph/visibility tool worth building? (only after 1–5)

## Scale plan (programmatic fix across the estate)

Three-layer pipeline — tool proposes, human approves, CI enforces:

1. **Scanner/classifier** (extend `reflection_audit.py`): emit structured JSON
   inventory per repo — site, target member, bucket guess, confidence. Heuristics:
   loops over `getDeclaredFields` = plumbing; target class outside company
   packages = third-party; string-literal target resolving in corpus = one-off
   hack candidate; argument from config/variable = opaque, human-only.
2. **OpenRewrite recipes** (Java — team can own it) for the mechanical bucket:
   type-aware source-to-source rewrite of `getDeclaredMethod("x") +
   setAccessible + invoke` chains into direct calls + modifier change.
   OpenRewrite is built for org-wide migrations; solves name resolution by
   riding the real compiler. Output = one proposed PR per site, never auto-merge.
3. **Guardrail rollout**: ArchUnit/Error Prone ban on `setAccessible` outside
   allowlist, in the shared parent build config so every app inherits it.

Auto-fixable tier (recipe eligible): target is string literal + resolves in
same repo + not an override + not implementing an interface + all callers
visible statically. Everything else → human queue with tool-computed evidence
(caller list, packages, cross-repo hits).

## Key facts learned

- Typical legacy corpus: reflection is real but spread thin across ~150 sites,
  with no central plumbing file to fix in one place.
- 0 hits in test paths → reflection lives in production code, not a test cheat.
- Only `setAccessible` bypasses `private`; `forName`/`newInstance` on public members
  respect modifiers. The visibility problem is the `setAccessible` sites, not the rest.
- Java rules that bound any fix: can't reduce visibility of overridden methods;
  interface implementations stay public.
