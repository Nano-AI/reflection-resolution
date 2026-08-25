# Pitfalls — every trap found, with a runnable example

Catalog of the failure modes this project has to handle. Each entry: what goes wrong, a
minimal example, the actual evidence, and what the tool must do about it. Everything here
was reproduced on a real JDK — the error text is quoted verbatim, not paraphrased.

Fixtures: `examples/` (analyzer) and the throwaway cases below, all small enough to retype.

---

## Java semantics — silent behavior changes

### 1. Widening a private method creates an override

The one that matters most, because nothing warns you. A `private` method is non-virtual, so
a subclass method with the same signature is an *independent* method. Widen the parent's
visibility and it becomes a genuine override, redirecting dispatch.

```java
public class Parent {
    private String who() { return "PARENT"; }   // <- only this line changes
    public  String call() { return who(); }
}
public class Child extends Parent {
    public String who() { return "CHILD"; }     // legal; overrides nothing while parent is private
}
```

**Evidence** — the only edit was `private` → `public`:

```
BEFORE (who is private):   new Child().call() -> PARENT
AFTER  (who is public):    new Child().call() -> CHILD
```

No compile error, no warning, different production behavior. This is why "just make it
public" is unsafe as a blanket rule.

**Tool must:** build a subclass index (each class file records its superclass), and before
proposing any widening, look for a matching `name + descriptor` in any subclass of the
declaring class. If one exists, refuse and escalate. Reference: JLS §8.4.8.1 — private
methods are not inherited and therefore not overridden.

### 2. Prefer deleting the reflection over widening anything

When the target has no callers outside its declaring class, the correct fix changes no
modifier at all — delete the reflective chain, call the member directly. Pitfall #1 is then
structurally impossible. Both resolvable sites in the fixture landed here, so this is the
common case, not the exotic one.

---

## Compile breakage — all three verified

### 3. Removing reflection orphans its catch clause

Reflection lookups throw checked exceptions. Delete the throwing call and the handler
becomes illegal.

```java
try { System.out.println("x"); }
catch (java.lang.NoSuchMethodException e) {}
```

```
error: exception NoSuchMethodException is never thrown in body of corresponding try statement
```

**Tool must:** when deleting a reflective chain, also delete catch clauses whose caught
types are *entirely* within the reflection exception set (`NoSuchMethodException`,
`NoSuchFieldException`, `IllegalAccessException`, `InvocationTargetException`,
`ClassNotFoundException`, `ReflectiveOperationException`). A multi-catch that also names a
non-reflection exception must be narrowed, not deleted.

### 4. …which can leave a `try` with nothing attached

```java
try { System.out.println("x"); }
```

```
error: 'try' without 'catch', 'finally' or resource declarations
```

**Tool must:** detect that removing the last catch empties the `try`, and emit a blocker
rather than output. Unwrapping requires knowing whether anything else in the block throws —
that is a human call. `patch.py` already does this.

### 5. An overriding method cannot reduce visibility

Relevant when the proposed fix is package-private rather than public.

```java
class A { public void f() {} }
class B extends A { void f() {} }
```

```
error: f() in B cannot override f() in A
  attempting to assign weaker access privileges; was public
```

**Tool must:** never propose narrowing a member that overrides something, and never narrow
an interface implementation — those stay public by rule.

---

## Bytecode and tooling traps

### 6. `grep` silently declines binary files

The prefilter depends on `setAccessible` being a UTF-8 constant-pool string, which it is.
But without `-a`, grep returns nothing — indistinguishable from "no matches."

```bash
grep -rl  setAccessible x/WEB-INF/classes    # returns nothing (wrong)
grep -ral setAccessible x/WEB-INF/classes    # returns the class (right)
```

**Rule:** empty output has two meanings. Narrow to the smallest case (`grep -ac` on one
file) before concluding the idea failed.

### 7. javac omits the owner prefix for same-class calls

```
3: invokespecial #3    // Method recalc:(Ljava/lang/String;)V          <- same class
                       // Method com/x/Order.recalc:(...)              <- cross class
```

**Tool must:** when parsing class C, treat an unqualified method reference as `C.member`.
Miss this and every same-class caller silently disappears from the graph — which would flip
verdicts from "has callers" to "has none," the most dangerous possible error.

### 8. Spring Boot fat jars carry loader classes at the root

The real Boot jar reported ~126 classes at its root. Those are Spring Boot's own loader,
not application code.

**Tool must:** treat root classes as "own code" only when the layout is a plain jar. When
`BOOT-INF/classes/` or `WEB-INF/classes/` exists, that directory is the sole source of own
code.

### 9. One project emits several artifacts with different layouts

`kogito-rules-1.0.0-SNAPSHOT-plain.jar` has classes at the root; `kogito-rules-1.0.0-SNAPSHOT.jar`
uses `BOOT-INF/`. Same build, same version.

**Tool must:** detect layout per artifact, never per project, and record which artifact a
finding came from.

### 10. A target may not be statically resolvable at all

`o.getClass().getDeclaredMethod("recalc", …)` produces no `ldc // class …` and no
`Class.forName` string, so the target class is genuinely unknown.

**Tool must:** return "needs a human decision" rather than guess. A tool that resolves
every input is not precise, it is lying. Optional later refinement: read the receiver's
declared type from `LocalVariableTable`.

### 11. Generic loops have no single target

```java
for (Field f : c.getDeclaredFields()) { f.setAccessible(true); }
```

There is no member to widen — this is serialization/mapper plumbing (very likely what
TypeUtil's eight cloned lines are). "Make it public" is undefined here.

**Tool must:** classify loop-bound receivers as plumbing and allowlist them, not queue them
for a fix.

### 12. Most reflection is in code you cannot edit

The real Boot artifact: **0 `setAccessible` in own code, 34 of 194 dependency jars contain
it** — spring-core, xstream, guava, snakeyaml, commons-lang3.

**Tool must:** split own-code from bundled dependencies before counting anything, using
location × package prefix. Otherwise real findings drown in framework noise.

---

## Editing mechanics

### 13. Byte-range edits must be applied in reverse offset order

Every tree-sitter edit is `(start_byte, end_byte, replacement)`. Applying them front-to-back
invalidates every later offset and corrupts the file quietly.

```python
for start, end, repl in sorted(edits, key=lambda e: -e[0]):
    src = src[:start] + repl.encode() + src[end:]
```

### 14. Substring matching breaks import cleanup

Checking whether `Method` is still used by counting string occurrences fails, because
`getDeclaredMethod` contains `Method`. Live bug in `examples/patch.py`, left as a lesson.

**Fix:** count `type_identifier` AST nodes, not string occurrences. Even trivial cleanup
wants the parser.

---

## Process

### 15. A failed command's later lines still run

While building the override demo, a `javac` step failed but the following `sed` still
executed and mutated the fixture, so the "before" and "after" compiles used identical
source and the demo appeared to show nothing. Shell chains continue past failures unless
you join them with `&&`.

**Rule:** when a demo shows no difference, suspect the fixture before the hypothesis.
Regenerate inputs from scratch and print them before drawing a conclusion.

---

## Rules the tool enforces, in priority order

1. Never guess a target — refuse instead (#10).
2. Prefer deleting reflection over widening visibility (#2).
3. Never widen when a subclass has a matching signature (#1).
4. Never narrow an override or interface implementation (#5).
5. Clean up catch clauses and imports, and stop with a blocker when the `try` can't be
   resolved automatically (#3, #4).
6. Split own code from dependencies before counting (#12).
7. Emit diffs for review; never auto-apply (all of the above).
