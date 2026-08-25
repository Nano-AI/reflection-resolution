# How It Works — code, data, why, how

Teaching reference for the analyzer. One section per stage of the pipeline. Each answers
the same four questions: **what it looks like in code · what data we get back · why we pull
it · how we do it.** Every data block is real output, not illustration.

Fixtures live in `examples/`. Run everything yourself:

```bash
cd ~/Documents/reflection_issue/examples
mkdir -p out && javac -g -d out Order.java Hack.java
```

---

## 1. The violation we're hunting

**In code it looks like this.** Three spellings of the *same* hack. A tool that only
handles the first one is useless in a legacy estate, so all three are in the fixture:

```java
// A: chained, class literal
Order.class.getDeclaredMethod("recalc", String.class).setAccessible(true);

// B: Class.forName into local variables
Class<?> c = Class.forName("com.x.Order");
Method m = c.getDeclaredMethod("recalc", String.class);
m.setAccessible(true);

// C: getClass() inside a nested block
Method mm; { mm = o.getClass().getDeclaredMethod("recalc", String.class); }
mm.setAccessible(true);
```

**The data we get back** — compile them and all three collapse to one shape:

```
 0: ldc           #2    // class com/x/Order
 2: ldc           #3    // String recalc
13: invokevirtual #6    // Method java/lang/Class.getDeclaredMethod:(...)
17: invokevirtual #7    // Method java/lang/reflect/Method.setAccessible:(Z)V
```

**Why we pull it.** Source syntax varies without limit; compiled form does not. Every
difference between A, B, and C disappears, which is what makes the analysis tractable.

**How we do it.**

```bash
javac -g -d out Order.java Hack.java          # -g keeps debug info -> line numbers
javap -p -c -l -classpath out com.x.Hack      # -p private, -c disassemble, -l tables
```

Then in Python, one regex turns disassembly into instruction tuples. The `// comment` is
the payload — javac already resolved the constant-pool entry for you:

```python
INSN = re.compile(r'^\s+(\d+): (\S+)(?:\s+#\d+)?(?:\s+// (.*))?$')
# a site is any instruction whose comment matches:
re.search(r'java/lang/reflect/\w+\.setAccessible', comment)
```

---

## 2. Which member is being forced open

**In code it looks like this:** `getDeclaredMethod("recalc", String.class)`.

**The data we get back** — for style B, the hard case, everything sits adjacent:

```
 0: ldc          // String com.x.Order      <- target class (via forName)
 2: invokestatic // Method Class.forName
 7: ldc          // String recalc           <- member name
10: anewarray    // class java/lang/Class   <- parameter array begins
15: ldc          // class java/lang/String  <- parameter type
18: invokevirtual// Method Class.getDeclaredMethod
24: invokevirtual// Method Method.setAccessible
```

**Why we pull it.** The whole decision hangs on *which* member is being opened. Without a
resolved target there is no visibility question to answer.

**How we do it.** Walk backwards from the site through a flat list. No dataflow analysis,
no scope tracking — the operands are neighbours:

```python
def resolve_target(insns, site_idx):
    lookup = None
    for j in range(site_idx - 1, -1, -1):                    # nearest getDeclaredMethod
        if any(f'java/lang/Class.{L}' in insns[j][2] for L in LOOKUPS):
            lookup = j; break
    if lookup is None:
        return {'case': 'opaque'}                            # never guess
    # then back from `lookup`: ldc "String X" = member, ldc "class Y" = param or target
```

Target class appears as `ldc // class com/x/Order` (a class literal) or as the string fed
to `Class.forName`. Style C uses `o.getClass()`, where neither exists — so it resolves to
`opaque` and goes to the human queue. That refusal is the feature.

---

## 3. Where it lives in source

**In code it looks like this:** nothing — this is metadata javac emitted.

**The data we get back:**

```
LineNumberTable:
  line 10: 6
  line 11: 22
  line 12: 27
```

Read as "source line 11 begins at bytecode offset 22."

**Why we pull it.** A finding you cannot point at in a file is useless for a PR, and the
patcher needs an anchor.

**How we do it.** Take the entry with the largest offset ≤ the instruction's offset. Our
site is at offset 24, so → line 11. Verified by hand-counting `Hack.java`: line 11 is
literally `m.setAccessible(true);`.

```python
def src_line(meth, offset):
    best = None
    for off, line in sorted(meth['lines']):
        if off <= offset:
            best = line
    return best
```

Pair it with `Compiled from "Hack.java"` at the top of the class for a full `file:line`.

---

## 4. What the target's visibility is today

**In code it looks like this:** `private void recalc(String a) {}`

**The data we get back:**

```
$ javap -p -classpath out com.x.Order
public class com.x.Order {
  private void recalc(java.lang.String);
  void other();
}
```

**Why we pull it.** You need the starting point to describe the change, and to know whether
a change is needed at all.

**How we do it.** One `javap -p` per target class, match the member by name. No source
parsing, no guessing at modifiers.

---

## 5. Who else calls it — the dependency edges

**In code it looks like this** — an ordinary call elsewhere in the codebase:

```java
void other() { recalc("z"); }
```

**The data we get back:**

```
3: invokespecial #3    // Method recalc:(Ljava/lang/String;)V
```

Note what is *missing*: no owner prefix. javac omits the owner for same-class calls. A
cross-class call instead reads `// Method com/x/Order.recalc:(Ljava/lang/String;)V`.

**Why we pull it.** This is the input to the entire decision, and the thing SonarQube cannot
give you. Minimal sufficient visibility is a function of who the callers are.

**How we do it.** Scan every class's instructions for method references and key them by
owner. Because bytecode carries fully-qualified owners and exact descriptors, overloads,
imports, wildcard imports, and superclass chains all stop being problems:

```python
m = re.match(r'Method (?:([\w/$]+)\.)?(\w+):(\(.*)', comment)
owner = m.group(1).replace('/', '.') if m.group(1) else current_class   # the gotcha
callers[f'{owner}#{m.group(2)}'].add(current_class)
```

---

## 6. What is ours versus what is a library

**In code it looks like this:** nothing in code — it's packaging.

**The data we get back**, from a real Spring Boot jar:

```
layout=boot   own_classes=27   dep_jars=194
setAccessible in OWN code: 0
dep jars containing setAccessible: 34 of 194
    18 classes  spring-core-6.2.19.jar
    12 classes  xstream-1.4.21.jar
    10 classes  guava-32.0.1-jre.jar
```

**Why we pull it.** Most `setAccessible` in any deployed Java artifact is legitimate
framework reflection you must never touch. Skip this split and your real findings drown in
library noise. It is also the strongest line in a leadership deck: *of everything in the
deployed artifact, only these are ours.*

**How we do it.** Probe zip entry names to detect layout, then classify on **location ×
package prefix** — both signals, because location fails for shaded uber-jars and prefix
fails to separate "this app" from "another team's library we bundle."

| probe | layout | own classes | bundled deps |
|---|---|---|---|
| `BOOT-INF/classes/` | Spring Boot fat jar | `BOOT-INF/classes/` | `BOOT-INF/lib/` |
| `WEB-INF/classes/` | WAR | `WEB-INF/classes/` | `WEB-INF/lib/` |
| contains `*.war` | EAR | recurse | `lib/` |
| otherwise | plain jar | zip root | none |

Trap: a Boot fat jar also has ~126 classes at its *root* — Spring Boot's loader, not app
code. Root classes count as "own" only for plain jars.

Nested jars read in memory, no extraction:

```python
inner = zipfile.ZipFile(io.BytesIO(outer.read('BOOT-INF/lib/foo.jar')))
```

---

## 7. Finding candidates without disassembling everything

**In code it looks like this:** nothing — it's an optimization.

**The data we get back:**

```
$ grep -ral setAccessible x/WEB-INF/classes
x/WEB-INF/classes/com/x/Hack.class
```

Only the class that actually uses it.

**Why we pull it.** A large estate × thousands of classes each is too much to disassemble. This
narrows it to a handful before any `javap` runs.

**How we do it.** `setAccessible` is stored as a UTF-8 string in each class file's constant
pool, so plain grep finds it. **`-a` is mandatory** — without it grep silently declines to
search binary files and returns nothing, which is indistinguishable from "no matches" unless
you check.

---

## 8. The verdict — what to actually do

**In code it looks like this** — the decision function, the part that is genuinely yours:

```python
who      = callers.get(f"{cls}#{member}", set()) - {site_owner}   # drop reflective site
external = who - {cls}                                            # drop declaring class

if not external:
    return "no visibility change needed — delete the reflection and call it directly."
if all(c.rsplit('.', 1)[0] == pkg for c in external):
    return f"reduce to package-private — callers all live in {pkg}."
return "needs a human decision — callers in other packages; exposing this commits to public API."
```

**The data we get back:**

```
Hack.java:6   target: com.x.Order#recalc('java.lang.String',)
  verdict: no visibility change needed — `private void recalc(java.lang.String)` has no
           callers outside com.x.Order; delete the reflection and call it directly.
Hack.java:11  (same verdict — style B resolves identically)
Hack.java:16  target: <opaque>
  verdict: needs a human decision — target class not statically resolvable
```

**Why we pull it.** Sonar answers *where*. This answers *what to do, in what order, and in
how few release waves* — the part no existing tool provides.

**How we do it.** Pure data processing over the three indexes; no parsing at this stage.
Note the third line: style C is refused, not guessed. **A tool that resolves everything is
lying**, and asserting that a case *must* fail is what makes the automatically-fixable set
trustworthy.

---

## 9. The patch

**In code it looks like this** — before, and the intended after:

```java
// before
Method m = Order.class.getDeclaredMethod("recalc", String.class);
m.setAccessible(true);
m.invoke(o, "x");

// after
o.recalc("x");
```

**The data we get back** from `patch.py`:

```
• delete: m.setAccessible(true);
• delete lookup: Method m = Order.class.getDeclaredMethod("recalc", String.class);
• rewrite invoke -> o.recalc("x")
• delete now-unreachable catch (IllegalAccessException|NoSuchMethodException)
• BLOCKER: try would have no catch/finally left — the try must be unwrapped too.
           Emit for human review; do not auto-apply.
```

**Why we pull it.** Bytecode decided *what* to change; only source can execute it. And the
cleanup is not optional — reflection throws checked exceptions, so deleting the throwing
call makes `catch (NoSuchMethodException e)` unreachable and **the file stops compiling**.

**How we do it.** tree-sitter gives byte ranges; edits are `(start, end, replacement)`
triples applied in **reverse offset order** so earlier offsets stay valid:

```python
def apply_edits(src: bytes, edits) -> bytes:
    for start, end, repl in sorted(edits, key=lambda e: -e[0]):
        src = src[:start] + repl.encode() + src[end:]
    return src
```

The modifier change is one node rewrite — keep every modifier that isn't a visibility
keyword:

```python
kept = [text(src, c) for c in mods.children
        if text(src, c) not in ('private', 'protected', 'public')]
```

When the transform cannot be proven safe, emit a BLOCKER instead of output. **A tool that
refuses is more useful than one that produces plausible breakage.**

*Known bug, left in as a lesson:* the unused-import check counts substrings, and
`getDeclaredMethod` contains `Method`, so the import is never removed. Fix by counting
`type_identifier` AST nodes rather than string occurrences — even the "easy" cleanup wants
the parser.

---

## Run the whole thing

```bash
cd ~/Documents/reflection_issue/examples
mkdir -p out && javac -g -d out Order.java Hack.java
javap -p -c -l -classpath out com.x.Hack        # sections 1-5 raw data
python3 decide.py out com.x.Hack com.x.Order    # section 8
../.venv/bin/python patch.py tc.java reflect 7  # section 9
```

## Principles worth keeping

1. **Build the hostile fixture before the tool** — three awkward variants, not one clean one.
2. **Prefer output that is a number.** "34 of 194" changes decisions; "seems common" doesn't.
3. **Read output literally.** The missing owner prefix and the 126 loader classes were both
   discovered by noticing, not predicting.
4. **Empty output has two meanings** — no match, or the tool declined. Check which.
5. **Assert the negative case.** Know what your tool must refuse to answer.
6. **Validate against the real judge.** For code edits, that judge is the compiler.
