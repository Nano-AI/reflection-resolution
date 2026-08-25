# Reflection Cleanup Playbook

Self-contained. Everything needed to run the cleanup locally without copying
source anywhere. Scope for a first pass: a few dozen suspect sites.

---

## Step 1 — Build the site table (one afternoon)

Open every suspect site. For each, record one row:

| # | repo | file:line | API used | target (Class#member) | ours or 3rd-party? | current visibility | why reflection? (best guess) | bucket | proposed fix | risk (L/M/H) |
|---|------|-----------|----------|----------------------|--------------------|--------------------|------------------------------|--------|--------------|--------------|

Keep it in a spreadsheet or markdown file. This table is the deliverable the
team sees, and the thing to review before touching any code.

## Step 2 — Classify with this decision tree

For each site, first question: **what class does the reflection target?**

1. **Target is a 3rd-party / vendor class** (library jar, not your code)
   → bucket: `third-party workaround`.
   Fix: isolate the hack in ONE adapter class, comment why, pin the library
   version. Allowlist it. You cannot fix code you don't own.

2. **Code loops over fields/methods generically** (`getDeclaredFields()`,
   `for (Field f : ...)`, builds getter names like `"get" + prop`)
   → bucket: `serialization/mapper plumbing`.
   Fix: none now. Legitimate reflection (Jackson does the same). Allowlist.
   Optional future project: replace with Jackson/MapStruct. Do NOT couple
   that migration to this cleanup.

3. **`Class.forName(stringFromConfig)` + `newInstance()` through an interface**
   → bucket: `config factory`.
   Fix: pattern is fine IF target classes have a public no-arg constructor and
   implement a public interface. If any setAccessible is involved to reach a
   private constructor — fix that constructor to public, keep the factory.
   Also: these classes must never be flagged "unused/dead" by any later analysis.

4. **Hardcoded single member of OUR OWN class forced open**
   (`getDeclaredMethod("specificBusinessMethod")` + `setAccessible(true)`)
   → bucket: `one-off hack`. THE REAL PROBLEM. Fix priority order:
   a. Target class grows a proper public method exposing the *operation*
      (caller wanted `order.recalculate()`, not the internal field).
   b. Caller and target belong together → same package, member goes
      package-private (Java's actual "visible to friends" tool).
   c. Member genuinely is cross-app API → make it `public`, deliberately,
      with a comment saying who calls it.

Rules that bound every fix:
- Cannot reduce visibility of an overridden method (Java compile error).
- Interface implementations stay public.
- If the member is called from the OTHER repo, changing it needs both repos
  released together — mark risk H.

## Step 3 — Get buy-in BEFORE the first PR

Show the finished table to whoever owns the code. Frame: "found N reflection
sites that bypass encapsulation; here's the classification and a proposed fix
order; I'd like to fix the low-risk ones." Visibility changes in legacy code
need a sign-off, so this step is not optional. The table is worth keeping as a
work inventory even if the answer is "don't touch it."

## Step 4 — Fix loop (repeat per site, lowest risk first)

1. Write a characterization test FIRST: call the class with realistic inputs,
   assert whatever it currently returns — even if the output looks wrong.
   You are pinning behavior, not asserting correctness. (Feathers, *Working
   Effectively with Legacy Code*.)
2. Apply the bucket's fix. Replace the reflective call with a direct call —
   compiler now enforces types.
3. Run the characterization test. Green = behavior preserved.
4. One site per PR. Small diff, easy review, easy revert.
5. Start with the single most trivial one-off hack to establish the pattern
   and get the review template agreed.

## Step 5 — Guardrail (turns cleanup into permanent invariant)

ArchUnit test in CI once the one-off hacks are gone:

```java
@ArchTest
static final ArchRule noReflectiveAccess =
    noClasses()
        .that().resideOutsideOfPackage("com.company.mapper..")  // allowlist from step 2
        .should().callMethod(AccessibleObject.class, "setAccessible", boolean.class)
        .because("reflection hacks bypass compile-time safety; expose a proper API instead");
```

Without this, someone re-adds setAccessible in six months and the count creeps
back. With it, the number only goes down.

## What to bring back for help

No code needed — describe sites in your own words:
"utility class forces open private constructor of X to build test fixtures" or
"job scheduler looks up method named in a database column" is enough to get
the right fix pattern. Ambiguous bucket calls are the useful ones to discuss.
