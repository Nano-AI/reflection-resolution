# Build Plan — setAccessible Analyzer

Single source of truth. Supersedes every earlier version in this file (OpenRewrite route,
C++/Go debate, phase plans P0–P5). Scope: **`setAccessible` only.**

## REVISION 2026-08-19 — resolve against bytecode, not source

Source parsing loses the name-resolution fight; that pain is real and does not shrink with
effort. Bytecode does not have the problem at all, because javac already resolved
everything. Verified on a three-style fixture (`scratchpad/bc/`):

- Three different source spellings of the same hack — chained call, `Class.forName` +
  local variable, `o.getClass()` in a nested block — all compile to the **same linear
  instruction pattern**: `ldc "recalc"` … `invokevirtual Class.getDeclaredMethod` …
  `invokevirtual Method.setAccessible`. Recovering the target is a scan of a few adjacent
  instructions. No dataflow analysis, no syntax variability.
- Member modifiers come out exact: `private void recalc(java.lang.String)`.
- Caller edges are exact and fully qualified with descriptors —
  `invokespecial Method recalc:(Ljava/lang/String;)V` — so overloads, wildcard imports,
  superclass chains, and receiver-type inference all stop being problems.
- `-l` gives `LineNumberTable` + `SourceFile`, so every finding still maps back to
  `file:line` for the PR, and `LocalVariableTable` gives declared types.

On the toy fixture the pipeline already produces the real verdict: the only true caller of
`Order.recalc` is `Order` itself, so `private` suffices and the reflection is simply
unnecessary — delete it, call directly.

**Tooling:** `javap -p -c -l` ships with every JDK (zero install at a Java shop); parse its
text output in Python. Move to ASM later only if the text format becomes limiting.

### Research findings (2026-08-19) — check these BEFORE building anything

**1. SonarQube already has this rule: `java:S3011`**, "Reflection should not be used to
increase accessibility of classes, methods, or fields." It detects `setAccessible` on
`AccessibleObject` plus `Field.set*` mutators. If the org runs SonarQube — likely at this
size — **the inventory may already exist**, and pulling it is one API call rather than a
scanner build:

```
GET /api/issues/search?rules=java:S3011&ps=500&p=1        # paginate; facets cap at 100
GET /api/projects/search                                   # enumerate the 42
```

Check this first. It can delete milestones M0–M3 outright.

**2. This changes what the tool should be.** Sonar (or Error Prone) answers *where*.
It does not answer *who else calls the target* or *what minimal visibility removes the
need*. So the tool's value is the **decision + ordering layer on top of an existing
signal**, not a second linter. That is less work and a better story: not rebuilding a
linter, building what the linter can't do.

**3. Authoritative citation for the deck:** SEI CERT **SEC05-J**, "Do not use reflection to
increase accessibility of classes, methods, or fields." Better than an internal opinion.

**4. The workflow shape is validated by documented practice.** Google's Rosie (SWE at
Google, ch. 22 "Large-Scale Changes"): pattern-based tooling generates one large change,
splits it into small per-site patches, auto-tests, auto-assigns reviewers, global reviewers
approve mechanically and inspect only anomalies — plus a formal LSC review committee.
Tool proposes → split per site → human approves is the industry pattern, not an invention.

**5. Where prod differs from our design:** the detection layer in production is usually an
analyzer already deployed in CI (SonarQube, Error Prone), not a bespoke bytecode scanner.
Bytecode analysis is real but more typically used for dependency/compat work (`jdeps`,
jQAssistant). Keep the bytecode engine for the caller graph; do not rebuild detection if
Sonar covers it.

**6. Effort benchmark:** published estimates for JDK 8→17 style migrations run 4–8 weeks
for a medium enterprise app and 3–6 months for a large legacy monolith. Useful for setting
expectations with leadership on an estate this size.

### Safety rule — widening visibility can silently change behavior

**Demonstrated, not theoretical.** A `private` method is non-virtual, so a subclass method
with the same signature is an independent method, not an override. Widening the parent's
visibility turns it into a real override and redirects dispatch:

```java
class Parent { private String who() { return "PARENT"; }   // <- only this line changes
               public  String call() { return who(); } }
class Child extends Parent { public String who() { return "CHILD"; } }

new Child().call()   // private -> "PARENT"     public -> "CHILD"
```

No compile error, no warning — just different behavior at runtime. Fixture in
`scratchpad/trap/`.

**Therefore, before proposing any visibility widening the tool MUST check the corpus for a
same-signature method in any subclass of the target's declaring class.** Bytecode makes
this cheap: the class file records its superclass, so build a subclass index once and look
for a matching `name + descriptor`. If one exists → refuse, escalate to human review.

This also reinforces the ordering: prefer *deleting the reflection* over *widening
visibility*. When the target has no callers outside its declaring class, no modifier change
happens at all, and this entire failure mode is bypassed.

### Wave planning — the actual time-to-deployment lever

Finding sites was never the bottleneck; the team's baseline for a rule change is a
three-week release cycle, so the metric is **number of release waves**. Nothing in this
project touches the deploy path — guardrails run at PR time in seconds — but the tool can
minimize waves:

- **Wave 0, zero-coordination subset:** targets with no callers outside the declaring
  class need *no visibility change at all* — delete the reflection, call directly. No
  cross-repo release, minimal risk. Biggest single lever; Sonar cannot compute it.
- **One PR per team, not per site:** batch a team's sites into one change; N review cycles
  collapse to one.
- **Evidence attached per site** (caller list, "nothing external touches this") turns a
  30-minute review into a 2-minute one.
- **Topological ordering** of cross-repo sites says which releases must ship together —
  computed up front, not discovered in staging.

Deck framing: converts "145 violations" into "three release waves."

### Ingest layer — one abstraction for JAR / WAR / EAR / exploded dirs

Normalize every artifact to the same triple, then all downstream code is packaging-blind:

```
artifact → (own_classes[], dependency_jars[], layout)
```

Layout detection by probing zip entry names (verified on real artifacts):

| Probe | Layout | own classes | bundled deps |
|---|---|---|---|
| `BOOT-INF/classes/` present | Spring Boot fat jar | `BOOT-INF/classes/` | `BOOT-INF/lib/` |
| `WEB-INF/classes/` present | WAR | `WEB-INF/classes/` | `WEB-INF/lib/` (+ `lib-provided/`) |
| contains `*.war` entries | EAR | — recurse into each | `lib/` |
| otherwise | plain/uber jar | zip root | none bundled |

**Boot-jar trap:** a Boot fat jar also has ~126 classes at its *root* — that is Spring
Boot's loader, not app code. Never treat root classes as "own" when layout is `boot`.

**Classification is location × package prefix (2×2), not either alone:**

| | company package | other package |
|---|---|---|
| **in own-classes area** | this app's code → **fix queue** | shaded/relocated third-party → ALLOWLIST |
| **in a bundled dep jar** | internal shared library → **cross-repo target**, fix in its home repo | third-party → ALLOWLIST |

Location alone fails for uber/shaded jars; prefix alone fails to separate "this app" from
"another team's library we bundle." Both signals, always.

**Cross-artifact resolution is a dictionary lookup, not inference.** Build one global index
over every ingested artifact keyed by `owner#name:descriptor`; bytecode gives fully
qualified owners and exact descriptors, so a caller edge in artifact A resolves against a
declaration in artifact B with no ambiguity. Cross-repo edges appear precisely because all
42 artifacts share one index. Dedupe identical classes by `(FQN, sha256(bytes))`; the same
FQN with *different* bytes across artifacts is version skew — flag it, don't merge it.

**Recursion, in memory.** Python `zipfile` reads a nested jar without extracting:
`zipfile.ZipFile(io.BytesIO(outer.read('BOOT-INF/lib/foo.jar')))`. EARs recurse the same
way. Extract to a temp dir only the handful of classes that survive the prefilter, since
`javap` needs a real path.

**Validation run** (real Boot jar, `~/Documents/kogito`): layout detected as `boot`,
27 own classes, 194 dependency jars; **0 setAccessible in own code, 34 of 194 dep jars
contain it** — spring-core (18 classes), xstream (12), guava (10), snakeyaml (8),
commons-lang3 (8), spring-boot (5). Lesson: most `setAccessible` in any deployed Java
artifact is legitimate framework reflection you must never touch, which is why the
own/dep split has to happen before counting anything. It is also an exec talking point:
"of N in the deployed artifact, only M are ours."

### Extraction algorithm — what to parse out of `javap` (all verified)

**Step 0, prefilter.** `setAccessible` sits in the constant pool as a UTF8 string, so raw
grep finds candidate classes without disassembling anything. `-a` is required (binary).

```bash
grep -ral setAccessible x/WEB-INF/classes                  # your code: the real queue
for j in x/WEB-INF/lib/*.jar; do
  unzip -p "$j" | grep -qa setAccessible && echo "CANDIDATE $j"; done   # deps
```

This yields the classes-vs-lib split (how much is actually yours) in seconds, and means
you disassemble ~20 classes per app instead of thousands.

**Step 1, find sites.** Instruction lines have the form
`      24: invokevirtual #7    // Method java/lang/reflect/Method.setAccessible:(Z)V`.
Parse with `^\s*(\d+): (\S+)\s+(#\d+)?\s*(?://\s*(.*))?$` → offset, opcode, cp ref,
**resolved comment**. The comment is the payload — javac already did the resolution. A site
is any comment matching `java/lang/reflect/\w+\.setAccessible`.

**Step 2, resolve the target by backward scan** within the same method's instruction list.
Worked example (styleB, the case that is hard in source): site at offset 24 →
nearest preceding `Class.getDeclaredMethod` at 18 → nearest preceding
`ldc // String recalc` at 7 = member name → the `anewarray // class java/lang/Class` block
at 10–17 whose `ldc // class java/lang/String` entries give the parameter descriptor →
continue back to `ldc // String com.x.Order` at 0 feeding `Class.forName` at 2 = target
class. Variants: a class literal appears directly as `ldc // class com/x/Order`;
`Object.getClass()` means take the receiver's declared type from `LocalVariableTable`.
Nothing found before the method start → opaque → HUMAN. No dataflow analysis — the list is
linear and the operands are adjacent.

**Step 3, source line.** `LineNumberTable` maps source line → starting offset:
`line 10: 6`, `line 11: 22`, `line 12: 27`. For an instruction at offset 24, take the entry
with the largest offset ≤ 24 → line 11 (verified correct). With `Compiled from "Hack.java"`
at the top of the class, that is your `file:line` for the patch.

**Step 4, target modifiers.** `javap -p <target class>` prints
`private void recalc(java.lang.String);` — exact, no source needed.

**Step 5, caller edges.** Scan every class for comments matching
`Method com/x/Order.recalc:(...)`. Fully qualified with descriptor = zero ambiguity.
**Gotcha:** javac omits the owner for same-class calls (`// Method recalc:(...)V` inside
`Order` itself), so when parsing class C an unqualified reference means C.

**Performance:** pass many class names to one `javap` invocation; process startup dominates.

### WAR/EAR artifacts — verified recipe

A `.war` is the best possible input: it holds the app's own compiled code *and* its whole
dependency closure, so no build and no dependency resolution is needed.

```
WEB-INF/classes/**.class   the app's own code  → the real work queue
WEB-INF/lib/*.jar          every dependency    → company-prefixed jars are internal shared
                                                 libs (cross-repo targets, visible without
                                                 cloning); the rest are third-party = ALLOWLIST
```

Bucketing therefore falls out of *file location* — third-party classification is free.
Spring Boot packaging uses `BOOT-INF/classes` and `BOOT-INF/lib` instead; an `.ear` wraps
WARs plus a shared `lib/`, one extra layer.

Verified commands (tested on a constructed WAR):

```bash
unzip -l app.war | grep -E "WEB-INF/(classes|lib)"       # inspect without extracting
unzip -q app.war -d x                                     # extract
find x/WEB-INF/classes -name '*.class' \
  | sed 's|x/WEB-INF/classes/||; s|/|.|g; s|\.class$||'    # enumerate FQNs to scan
javap -p -c -l -classpath "x/WEB-INF/classes:x/WEB-INF/lib/*" com.x.Hack
```

The wildcard classpath works. Output confirmed to carry `line N:` entries (so findings map
to source for patches), plus `ldc class com/x/Order` (target class), `ldc String recalc`
(target member), and `ldc class java/lang/String` (parameter types → exact overload match).

**The one requirement:** compiled `.class`/`.jar` artifacts. Get them from Artifactory
(the company already publishes there — no build at all), from CI outputs, or by building
once per repo. This is a *weaker* requirement than OpenRewrite's, which needs full
dependency resolution per repo on top of a build-file hookup.

**Correction on OpenRewrite:** the "writing on top of someone else's code" objection was
answerable — a Gradle init script (`gradle --init-script x.gradle rewriteDryRun`) injects
the plugin without touching any repo file. It was rejected on a wrong premise. It stays in
reserve for the *fix* phase; it is not needed for analysis. The fix itself is a one-token
modifier change at a known `file:line`, which a small Python patcher can emit as a diff.

**IntelliJ:** the right tool for working the HUMAN queue by hand (Find Usages, safe
refactors). Do not write a plugin; it is not the automation backbone.

**tree-sitter is demoted, not discarded:** still the best way to find sites in repos with
no artifacts, and to locate the exact source construct when generating a patch. It is no
longer the resolver.

**What would flip this back:** artifacts unavailable *and* repos won't build. Then return
to the source-parsing plan below and accept a larger HUMAN queue.

Revised pass structure: bytecode scan → sites + exact targets → exact caller edges →
visibility decision → source patch at `file:line`. Milestones M4–M8 below are unchanged;
M1–M3 are replaced by bytecode extraction, which is substantially less code.

## What we're building and why this shape

A read-only static analyzer that, across N Java repos, answers per `setAccessible` call:
**what member is being forced open, who else touches that member, and what visibility
would make the reflection unnecessary.**

Why source parsing and not OpenRewrite/Maven-plugin route (rejected 2026-08-19): that
route needs a build-file edit per repo and a repo that fully compiles with resolved
dependencies. Across a large legacy estate that's one build-file edit and one team
conversation per repo, plus an unknown fraction that won't build. It buys type-accurate *rewriting*, which is not the
bottleneck. Parsing source read-only needs nothing but a `git clone`, tolerates repos
that don't compile, and scales by looping folders.

Cost accepted: name-based resolution is approximate (overloads, same-named classes,
inheritance). **Governing rule: never guess. Every unresolved or ambiguous site is
emitted to a HUMAN queue with its evidence.** Precision on the AUTO tier is what
matters; recall can be imperfect.

## Stack

- Python 3 + `tree-sitter` + `tree-sitter-java` (venv at `.venv/`, already created here).
- tree-sitter chosen over `javalang` because it is error-tolerant (parses files with
  syntax it doesn't fully know — important for a legacy estate) and tracks modern Java.
- Output: JSON (machine), DOT/Graphviz (the exec visual), text report (the dev-facing one).
- If pip can't reach PyPI on a locked-down network: an internal mirror proxies it, or
  download the two wheels anywhere and copy them over — both are self-contained.

## Architecture — four passes over the corpus

```
repos/*.java
   │
   ├─ PASS 1  declaration index    pkg.Class#member → {file, line, modifiers, arity, kind, is_override}
   ├─ PASS 2  setAccessible sites  site → {target class, target member, resolution case}
   ├─ PASS 3  reference index      member name → [callers: class, package, repo]
   │
   └─ PASS 4  join → per site: minimal sufficient visibility, tier (AUTO/HUMAN/ALLOWLIST), risk
                     → JSON + DOT + report
```

Passes 1 and 3 walk every file once. Pass 2 is the hard one. Pass 4 is pure data
processing over the three indexes — no parsing.

## Milestones

Each is independently verifiable. Don't start the next until the current one's check passes.

### M0 — See the tree (30 min)
Parse one Java file, print the node tree. Goal is fluency in the vocabulary, nothing else.
Already verified for you on a sample containing all the patterns — findings in
"AST facts" below. **Check:** you can point at any line of Java and predict its node type.

### M1 — Declaration index (half a day)
Walk every `.java`. For each file capture `package_declaration`, then every
`class_declaration` / `interface_declaration` / `enum_declaration`, then within each,
every `method_declaration`, `field_declaration`, `constructor_declaration`.

Record per member: fully-qualified owner, name, kind, arity (count `formal_parameter`
children), modifiers (the `modifiers` node's text — `private`/`protected`/`public`/none),
`@Override` presence (a `marker_annotation` inside `modifiers`), file, line
(`node.start_point[0] + 1`).

Hard parts: nested and inner classes (owner is `Outer.Inner`); multiple top-level classes
per file; a `field_declaration` can declare several names (iterate `variable_declarator`s).

**Check:** total member count is plausible; `grep -c "private "` in a few files roughly
matches; spot-check one nested class resolves to `Outer.Inner`.

### M2 — setAccessible sites + target resolution (the hard milestone, 1–2 days)
Find every `method_invocation` whose `name` is `setAccessible`. Then resolve what its
`object` refers to. Four cases, in this order:

1. **Chained** — the `object` is itself a `method_invocation` named
   `getDeclaredMethod` / `getMethod` / `getDeclaredField` / `getField` /
   `getDeclaredConstructor`. Read its first argument: a `string_literal` gives the member
   name; its own `object` gives the target class (`class_literal` → the `type_identifier`).
   Fully resolved. Easiest case, do it first.
2. **Local variable** — the `object` is an `identifier`. Search the enclosing method body
   for a `local_variable_declaration` whose `variable_declarator` name matches, and read
   its value — if that value is one of the lookup calls from case 1, resolve as above.
3. **Loop binding** — the `identifier` is bound by an `enhanced_for_statement` whose
   iterable is `getDeclaredFields()` / `getDeclaredMethods()`. No single target exists;
   this is generic plumbing. Bucket `plumbing`, tier `ALLOWLIST`. **This is almost
   certainly what TypeUtil.java's 8 cloned lines are** — confirm, and it removes ~8–15 of
   the 25 sites from the work queue automatically.
4. **Anything else** — receiver is a parameter, a field, a method return, or the member
   name is not a string literal. Bucket `opaque`, tier `HUMAN`. Do not guess.

Then map the target's simple class name to an FQN using the file's `import` declarations,
its own package, then the M1 index. Unresolvable → `HUMAN`.

Hard parts: "enclosing method body" means walking up parents until `method_declaration`;
reassignment of the variable (if the name is assigned more than once in the body, mark
`HUMAN`); `Class.forName("...")` held in a variable is case 2 with an extra hop.

**Check:** run against the beachhead repo, total sites equals the 25 your regex scanner
found. Any discrepancy is a bug in one of the two — reconcile before moving on.

### M3 — Reference index (half a day)
Walk every file again. Record every `method_invocation` name and every `field_access`
field name, with the enclosing class FQN, package, and repo. This is a name-keyed
multimap, not a real call graph — it over-approximates.

Ambiguity rule: when a name maps to more than one M1 declaration, mark every site
targeting it `ambiguous: true` → forced to `HUMAN`.

**Check:** pick a member you know is called from exactly one place; the index says so.

### M4 — Decide visibility (2–3 hours, pure data)
For each resolved site, take the target member's callers from M3, minus the reflective
site itself, minus the declaring class:

- no external callers → **`private` already suffices**: the reflection is unnecessary,
  delete it and call directly. Best case, and the easiest PR.
- all callers in the declaring class's package → **package-private**.
- callers only in subclasses → **protected**.
- callers in other packages → **public**.
- any caller in a different repo → `cross_repo: true`, risk **H** (needs coordinated release).

Blockers that force `HUMAN` regardless: member has `@Override`, member implements an
interface method, member is `ambiguous`, target unresolved.

Tier `AUTO` = resolved, unambiguous, no blockers, and the required visibility change is a
single modifier token.

**Check:** by hand, verify 3 sites' verdicts. Wrong verdict = bug in M3 or M4; wrong-but-
conservative (says HUMAN when a human would say AUTO) is acceptable.

### M5 — Output (2–3 hours)
Three emitters over the same in-memory result:
- `--json` — the durable inventory; also the fleet dashboard's input.
- `--dot` — nodes = classes, edges = reflective access; render with
  `dot -Tsvg`. This is the leadership slide.
- default text report — per repo: counts by bucket and tier, top files, the AUTO list
  (the work queue), the HUMAN list with its evidence.

### M6 — Patch generation (optional, half a day)
For AUTO sites only, emit a unified diff: one modifier token changed on the declaration
line, plus the suggested direct-call replacement at the reflective site as a comment for
the reviewer. **Emit, never apply.** A human opens the PR.

### M7 — Fleet driver (half a day)
Loop over a list of repo paths; run passes 1–5 per repo but keep one shared index so
cross-repo edges are visible; write aggregate JSON. Add `--check` mode: exit non-zero when
a non-allowlisted `setAccessible` exists, so the same script can serve as a CI gate on
repos that don't yet inherit an ArchUnit rule.

### M8 — Guardrail
ArchUnit rule (PLAYBOOK step 5) in the shared parent build so all repos inherit it, with
the allowlist populated from the `plumbing` bucket. This is what makes the number
permanently one-directional, and it's the sentence leadership remembers.

## How linking works (the two-phase rule)

Never resolve anything during a tree walk — when parsing `Foo.java`, the file declaring
`Order` may not be parsed yet. Phase A walks each file once and emits flat facts. Phase B
runs after all files are parsed and is pure dictionary lookups, no tree-sitter.

Phase A must capture, per file: `package_declaration`; every `import_declaration` as a
`simple name → FQN` map (wildcard imports tracked separately); all declarations; all
references with their *enclosing* class FQN (walk parents up to `class_declaration`); and
setAccessible sites with the target's still-unresolved simple name.

Phase B resolves a simple name to an FQN in this order: explicit import → same package as
the file → wildcard import matched against the declaration index (more than one hit =
ambiguous) → `java.lang` → unresolved (HUMAN, never guess).

**Key simplification:** no general call graph is needed. After pass 2 there are ~25 target
members; pass 3 only searches references to *those* names. Match on name + arity: exactly
one matching declaration corpus-wide = confident edge; two or more = ambiguous = HUMAN.
Later refinement if the ambiguous pile is large: resolve the receiver variable's declared
type from the enclosing method (local var / parameter / field) through the same import
rules. ~60 lines, disambiguates most real cases.

Record shapes emitted by phase A:

```
Decl:  {fqn_owner, name, kind, arity, modifiers[], is_override, file, line, superclass}
Ref:   {name, arity, enclosing_fqn, package, repo, file, line}
Site:  {enclosing_fqn, target_class_simple, target_member, case, file, line}
File:  {path, package, imports{simple:fqn}, wildcard_imports[]}
```

Edges: `Site.enclosing_fqn → resolved target owner`, plus each matching
`Ref.enclosing_fqn → target owner`. Nodes = classes. That's the DOT graph.

Capture `class_declaration`'s `superclass` field in phase A: a target member declared in a
superclass won't be found by a name lookup on the subclass, so phase B must walk up the
chain. Refinement, not a blocker.

Test order: phase A on one file → dump records as JSON and eyeball → run over
`~/Documents/kogito` and sanity-check declaration counts → only then write phase B, and
verify it against one member you can check by hand.

## AST facts (verified against a real parse, don't re-derive)

- `method_invocation` has fields `object`, `name`, `arguments`. `.child_by_field_name("object")`.
- **Chained case:** `setAccessible`'s `object` *is* the `getDeclaredMethod` `method_invocation`
  node. Nothing else needed.
- `class_literal` (`Order.class`) contains a `type_identifier` child = the class name.
- `string_literal` wraps a `string_fragment` child holding the text.
- `enhanced_for_statement` children in order: `type_identifier`, `identifier` (the loop
  variable — this is what to match against the receiver), then the iterable expression.
- `formal_parameters` contains `formal_parameter` children — count them for arity.
- Line number: `node.start_point[0] + 1`.

## Order of attack

1. M2 case 3 first if you want the fastest win: it classifies TypeUtil and likely deletes
   half the queue before you've built anything else.
2. Otherwise straight M1 → M2 → M3 → M4.
3. Develop against `scratchpad/S.java`-style fixtures you write yourself, plus any
   open-source Java repo — no private source needed to build the whole tool.
4. Only then point it at the real estate.

## Resources

| Resource | Use |
|---|---|
| tree-sitter Python bindings | https://github.com/tree-sitter/py-tree-sitter |
| tree-sitter Java grammar (node names) | https://github.com/tree-sitter/tree-sitter-java/blob/master/src/node-types.json |
| tree-sitter playground (paste Java, see tree live) | https://tree-sitter.github.io/tree-sitter/playground |
| `java.lang.reflect.AccessibleObject` | https://docs.oracle.com/en/java/javase/21/docs/api/java.base/java/lang/reflect/AccessibleObject.html |
| ArchUnit (M8 guardrail) | https://www.archunit.org/userguide/html/000_Index.html |
| Graphviz DOT (M5) | https://graphviz.org/doc/info/lang.html |
| Feathers, *Working Effectively with Legacy Code* | characterization tests before each fix |

## Organizational notes

Another team may already own this problem. Talk to them before doing anything
org-facing, frame the work as help rather than takeover, and credit them in any
write-up. Source never leaves its own machine — this repo holds tooling only.
