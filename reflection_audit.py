#!/usr/bin/env python3
"""Audit reflection usage in a Java codebase.

Usage:
    python3 reflection_audit.py /path/to/repo [/path/to/another/repo ...]

Two modes.

DEFAULT -- inventory. Walks every .java file, counts reflection-related
patterns, and reports:
  1. total hits per pattern (per repo)
  2. which files have the most suspect hits
  3. duplicate-line analysis: is setAccessible usage copy-pasted or ad hoc?
  4. which method names are looked up via getMethod("...") and how often

--java21 -- migration triage. Resolves each site's target class, then answers
the only question that matters for the Java 8 -> 21 move: does this site
break on 21, and why? See the big comment block further down for the module
rules behind the classification.

    python3 reflection_audit.py --java21 --json sites.json /path/to/repo
"""

import argparse
import collections
import os
import re
import sys
from collections import Counter
from pathlib import Path

# Each entry: label -> compiled regex. re.compile turns the pattern string
# into a reusable matcher object (faster than re-parsing it per line).
PATTERNS = {
    'servlet getMethod() [benign]':   re.compile(r'\.getMethod\(\)'),
    'reflective getMethod("name")':   re.compile(r'getMethod\("'),
    'getDeclaredMethod [suspect]':    re.compile(r'getDeclaredMethod'),
    'Factory.newInstance [benign]':   re.compile(r'Factory\.newInstance'),
    'Class.forName("literal")':       re.compile(r'Class\.forName\("'),
    'Class.forName(dynamic)':         re.compile(r'Class\.forName\([^")]'),
    'setAccessible [the cheat]':      re.compile(r'setAccessible'),
}

# Patterns that indicate real reflection worth investigating.
SUSPECT = {
    'reflective getMethod("name")',
    'getDeclaredMethod [suspect]',
    'Class.forName(dynamic)',
    'setAccessible [the cheat]',
}

# Captures the string literal inside getMethod("...") / getDeclaredMethod("...").
# (?:...) = group that matches but isn't captured; ([^"]*) = capture everything
# up to the closing quote.
METHOD_NAME = re.compile(r'get(?:Declared)?Method\("([^"]*)"')


def audit_repo(root: Path) -> None:
    pattern_totals = Counter()      # label -> total hit count
    suspect_per_file = Counter()    # file -> count of suspect hits
    accessible_lines = Counter()    # stripped code line -> count (clone detector)
    method_names = Counter()        # looked-up method name -> count
    examples = {}                   # label -> first (file, lineno, code) seen

    java_files = list(root.rglob('*.java'))  # rglob = recursive glob

    for path in java_files:
        # Legacy code often has non-UTF8 encodings; errors='replace' swaps
        # bad bytes for a placeholder instead of crashing.
        text = path.read_text(encoding='utf-8', errors='replace')
        rel = str(path.relative_to(root))

        for lineno, line in enumerate(text.splitlines(), start=1):
            for label, regex in PATTERNS.items():
                if regex.search(line):
                    pattern_totals[label] += 1
                    examples.setdefault(label, (rel, lineno, line.strip()))
                    if label in SUSPECT:
                        suspect_per_file[rel] += 1

            if 'setAccessible' in line:
                accessible_lines[line.strip()] += 1

            for name in METHOD_NAME.findall(line):
                method_names[name] += 1

    # ---- report ----
    print(f'\n{"=" * 70}')
    print(f'REPO: {root}   ({len(java_files)} java files)')
    print('=' * 70)

    print('\n-- Pattern totals --')
    for label in PATTERNS:
        print(f'  {label:<38} {pattern_totals[label]:>5}')

    print('\n-- Top files by suspect hits --')
    for file, count in suspect_per_file.most_common(15):
        print(f'  {count:>4}  {file}')

    print('\n-- setAccessible clone check (identical lines appearing 2+ times) --')
    clones = [(line, n) for line, n in accessible_lines.most_common() if n > 1]
    if clones:
        for line, n in clones[:10]:
            print(f'  {n:>4}x  {line[:90]}')
    else:
        print('  none — every setAccessible line is unique (ad hoc, not copy-paste)')

    print('\n-- Method names looked up via getMethod/getDeclaredMethod --')
    for name, n in method_names.most_common(20):
        print(f'  {n:>4}x  "{name}"')
    if not method_names:
        print('  none found (lookups may build names dynamically, e.g. "get" + prop)')

    print('\n-- One example per pattern --')
    for label, (file, lineno, code) in examples.items():
        print(f'  [{label}]')
        print(f'    {file}:{lineno}: {code[:100]}')


# ===========================================================================
# Java 21 migration mode  (--java21)
# ===========================================================================
#
# QUESTION THIS MODE ANSWERS
#   Of all our reflection sites, which ones actually BREAK when we move from
#   Java 8 to Java 21? (As opposed to merely being ugly, which is a separate,
#   later cleanup.)
#
# BACKGROUND -- why most reflection survives the migration
#   Java 9 split the JDK into modules. JEP 396 (Java 16) made strong
#   encapsulation the default; JEP 403 (Java 17) deleted the --illegal-access
#   escape hatch entirely. Deep reflection -- setAccessible(true) on a member
#   that isn't public -- now throws InaccessibleObjectException UNLESS the
#   target's package is *open* to the calling module.
#
#   The saving grace: classes loaded from the CLASSPATH live in the "unnamed
#   module", and the unnamed module opens every one of its packages. We have
#   no module-info.java, so all of our own packages plus every third-party jar
#   lands there. Reflection from our code into our own code therefore behaves
#   on Java 21 exactly as it did on Java 8.
#
#   What breaks is reflection that reaches into the JDK's own modules. Those
#   modules *export* their packages (so plain public reflection is fine) but
#   they do not *open* them (so setAccessible on a private member fails).
#
# SO THE CLASSIFICATION IS BASICALLY TWO QUESTIONS PER SITE
#   1. Does the target class live in the JDK, or in ours / a third-party jar?
#   2. Is the access "deep" (paired with setAccessible) or plain public?
#   Blocker = JDK target AND deep access.  Plus a few special cases below.
#
# WHY THIS EXISTS ALONGSIDE jdeps
#   `jdeps --jdk-internals` reads compiled bytecode and finds *static*
#   references to JDK internals. It cannot see Class.forName("sun.misc.X"),
#   because that target is a string in the constant pool, not a class
#   reference. This scanner covers exactly that blind spot. Run both.
#
# KNOWN LIMITS (deliberate -- see escalation note at the bottom of the file)
#   Line-based, so a reflective chain split across lines may resolve as
#   OPAQUE rather than resolving wrongly. It fails toward the human queue,
#   never toward a false "safe".

import json

def load_env(path: Path) -> dict:
    """Minimal .env reader -- no dependency, no shell semantics.

    KEY=value per line; blank lines and # comments skipped; surrounding
    quotes stripped. Anything fancier belongs in a real config format.
    """
    values = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, _, val = line.partition('=')
        values[key.strip()] = val.strip().strip('"').strip("'")
    return values


# Which package root counts as "our code" is deployment-specific, so it lives
# in .env (gitignored) rather than in this file -- nothing company-identifying
# gets committed. See .env.example for the template.
#
# Precedence, highest first:
#   --company on the command line
#   COMPANY_PREFIX in the real environment
#   COMPANY_PREFIX in .env next to this script
#   DEFAULT_COMPANY_PREFIX below
DEFAULT_COMPANY_PREFIX = 'com.example'
ENV_FILE = Path(__file__).resolve().parent / '.env'


def resolve_company_prefixes(cli_value: str = None) -> tuple:
    """Return the package roots that mean "our code", as a tuple.

    A tuple because str.startswith() accepts one directly, so supporting
    several roots costs nothing:
        COMPANY_PREFIX=com.example,org.example.internal
    Anything under these is in the unnamed module -> open -> safe on 21.
    """
    raw = (cli_value
           or os.environ.get('COMPANY_PREFIX')
           or load_env(ENV_FILE).get('COMPANY_PREFIX')
           or DEFAULT_COMPANY_PREFIX)
    return tuple(part.strip() for part in raw.split(',') if part.strip())

# Package roots that live inside JDK modules. Exported, but NOT open, so deep
# reflection into them is what Java 17+ rejects.
JDK_PREFIXES = ('java.', 'javax.', 'sun.', 'com.sun.', 'jdk.')

# Exception to the rule above. The jdk.unsupported module deliberately OPENS
# these two packages so decades-old hacks (chiefly sun.misc.Unsafe) keep
# working. Deep reflection here still succeeds on 21 -- but it is deprecated
# for removal, so we surface it as a ticket, not a blocker.
OPEN_JDK_PREFIXES = ('sun.misc.', 'sun.reflect.')

# Types deleted outright between Java 9 and 21. These fail with
# ClassNotFoundException rather than InaccessibleObjectException: a different
# crash, but the same migration blocker.
REMOVED_TYPES = {
    'sun.misc.BASE64Encoder',
    'sun.misc.BASE64Decoder',
    'sun.misc.Cleaner',          # moved to jdk.internal.ref.Cleaner in Java 9
    'sun.misc.Service',
}

# Whole package trees deleted from the JDK. Most were Java EE modules dropped
# in Java 11 (JEP 320) and now only exist as separate Maven artifacts.
REMOVED_PREFIXES = (
    'javax.xml.bind.',            # JAXB      -> jakarta.xml.bind
    'javax.xml.ws.',              # JAX-WS    -> jakarta.xml.ws
    'javax.xml.soap.',
    'javax.activation.',
    'javax.jws.',
    'javax.transaction.',
    'javax.annotation.',          # @Resource, @PostConstruct, ...
    'com.sun.image.codec.jpeg.',  # removed in Java 9
    'com.sun.xml.internal.bind.',
    'sun.audio.',
)

# Narrow carve-outs: these survived even though their parent prefix above was
# removed, because they belong to a different module that still ships.
STILL_PRESENT = (
    'javax.annotation.processing.',   # module java.compiler
    'javax.transaction.xa.',          # module java.transaction.xa
)

# java.lang is imported implicitly, so `String.class.getDeclaredField(...)`
# has no import line to resolve against. Without this set those sites would
# look like same-package company code -- i.e. a false "safe".
JAVA_LANG = {
    'Object', 'String', 'Class', 'Integer', 'Long', 'Short', 'Byte', 'Double',
    'Float', 'Boolean', 'Character', 'Number', 'Math', 'System', 'Thread',
    'ThreadLocal', 'ClassLoader', 'Runtime', 'Process', 'Enum', 'Iterable',
    'Comparable', 'Runnable', 'StringBuilder', 'StringBuffer', 'Throwable',
    'Exception', 'RuntimeException', 'Error',
}

# --- source-shape regexes -------------------------------------------------

PACKAGE = re.compile(r'^\s*package\s+([\w.$]+)\s*;')

# `import a.b.C;` or `import a.b.*;`. Static imports name a *member*, not a
# type, so they're skipped -- they can't be the receiver of a .class literal.
IMPORT = re.compile(r'^\s*import\s+(?!static\b)([\w.$]+(?:\.\*)?)\s*;')

# Any reflective member lookup. This is what marks a line as a "site".
#
# Singular Method/Field lookups must be passed a name, so a zero-arg
# `.getMethod()` is NOT reflection -- it is HttpServletRequest.getMethod()
# returning "GET"/"POST", which is why the inventory mode lists it as benign.
# Constructor lookups and the plural enumerations legitimately take no
# arguments (`getDeclaredConstructor()`, `getDeclaredFields()`), so those
# still count.
LOOKUP = re.compile(
    r'\.get(?:Declared)?(?:'
    r'(?:Methods|Fields|Constructors)\s*\('       # bulk enumeration
    r'|Constructor\s*\('                          # no-arg ctor lookup is real
    r'|(?:Method|Field)\s*\(\s*[^)\s]'            # named lookup: needs an argument
    r')')

# `Foo.class.getDeclaredMethod("bar")` -> receiver simple name "Foo".
CLASS_LITERAL_TARGET = re.compile(
    r'(\w+)\.class\.get(?:Declared)?(?:Method|Field|Constructor)s?\s*\(')

# `Class.forName("com.x.Foo")` -> exact target, no guessing needed.
FORNAME_TARGET = re.compile(r'Class\.forName\(\s*"([\w.$]+)"')

# `obj.getClass().getDeclaredField(...)` -> target is the runtime type, which
# source alone cannot tell us.
RUNTIME_TARGET = re.compile(
    r'getClass\(\)\s*\.\s*get(?:Declared)?(?:Method|Field|Constructor)s?\s*\(')

# The classic "strip final off a field" trick. Dead since Java 12, which added
# java.lang.reflect.Field to the reflection filter: the lookup itself now
# throws NoSuchFieldException. Highest-confidence blocker we can detect.
MODIFIERS_HACK = re.compile(r'getDeclaredField\(\s*"modifiers"\s*\)')

SET_ACCESSIBLE = re.compile(r'\.setAccessible\s*\(')

# How many lines after a lookup we'll look for its setAccessible call. Covers
# the usual two-liner (`Method m = ...;` then `m.setAccessible(true);`) and a
# short try block. The search also stops at the end of the enclosing block
# (see block_depths), so it can never pair a lookup with a setAccessible that
# belongs to the next method.
DEEP_WINDOW = 8


def block_depths(lines: list) -> list:
    """Brace nesting depth *after* each line, 1-based to match line numbers.

    Used to stop the setAccessible search at the closing brace of the method
    it started in. Without this a lookup near the end of one method happily
    pairs with a setAccessible at the top of the next one -- which silently
    upgrades a harmless public lookup into a fake "deep access" blocker.

    Braces inside strings, chars and // comments are stripped first. Block
    comments aren't tracked: a stray brace there only ever ends the search
    early, which costs us a pairing rather than inventing one.
    """
    depths = [0]                      # index 0 unused; line N lives at depths[N]
    depth = 0
    for line in lines:
        code = re.sub(r'//.*', '', line)
        code = re.sub(r'"(?:\\.|[^"\\])*"', '', code)
        code = re.sub(r"'(?:\\.|[^'\\])*'", '', code)
        depth += code.count('{') - code.count('}')
        depths.append(depth)
    return depths


def parse_file_context(text: str):
    """Extract what we need to turn a simple name like `Foo` into `a.b.Foo`.

    Returns (imports, wildcards, package):
      imports   {'Unsafe': 'sun.misc.Unsafe', ...}  -- explicit single imports
      wildcards ['java.util', 'com.example.core'] -- `import x.y.*;` roots
      package   'com.example.core.foo' or ''      -- this file's own package
    """
    imports, wildcards, package = {}, [], ''

    for line in text.splitlines():
        # Imports and package are always above the first type declaration, so
        # stop as soon as real code starts. Keeps us off commented-out imports
        # further down the file.
        if line.startswith(('public ', 'class ', 'final ', 'abstract ', '@')):
            break

        m = PACKAGE.match(line)
        if m:
            package = m.group(1)
            continue

        m = IMPORT.match(line)
        if m:
            fqn = m.group(1)
            if fqn.endswith('.*'):
                wildcards.append(fqn[:-2])
            else:
                imports[fqn.rsplit('.', 1)[-1]] = fqn

    return imports, wildcards, package


def collect_declared_types(java_files: list) -> set:
    """Every fully-qualified type name the corpus itself declares.

    A pre-pass so that same-package resolution can be *checked* rather than
    guessed. `Order.class` in package com.x means com.x.Order -- but only if
    com/x/Order.java exists. Without this check the scanner would happily
    invent a class and then classify the invented name, which is how you get
    a fake blocker (or, worse, a fake "safe").

    Java lets one file declare several types; we key off the public type,
    which by rule matches the file name.
    """
    declared = set()
    for path in java_files:
        head = path.read_text(encoding='utf-8', errors='replace')[:4000]
        m = PACKAGE.search(head)
        if m:
            declared.add(m.group(1) + '.' + path.stem)
    return declared


def resolve_target(line: str, imports: dict, wildcards: list,
                   package: str, declared: set):
    """Work out WHICH class a reflection line is poking at.

    Name resolution follows Java's own precedence (JLS 6.5.5): an explicit
    single-type import beats a type in the current package, which in turn
    beats a wildcard (on-demand) import. Getting that order wrong is how
    `Order.class` in package com.x becomes "java.lang.reflect.Order".

    Returns (fqn_or_None, how) where `how` records how much to trust the fqn:
      'literal'    Class.forName("...") gave us the exact name       -- certain
      'import'     Foo.class + an import line named Foo's package    -- certain
      'java.lang'  Foo.class, no import, Foo is implicitly imported  -- certain
      'same-pkg'   Foo.class + a sibling Foo.java really exists      -- certain
      'wildcard'   Foo.class, unresolved, but a JDK `import x.y.*;`
                   could supply it                                   -- guess
      'runtime'    obj.getClass() -- unknowable from source          -- opaque
      'unknown'    receiver is a variable, or nothing resolved       -- opaque
    """
    # Most precise signal first: an explicit fully-qualified string.
    m = FORNAME_TARGET.search(line)
    if m:
        return m.group(1), 'literal'

    m = CLASS_LITERAL_TARGET.search(line)
    if m:
        simple = m.group(1)

        # 1. single-type import -- wins over everything else
        if simple in imports:
            return imports[simple], 'import'

        # 2. java.lang, imported implicitly into every file
        if simple in JAVA_LANG:
            return 'java.lang.' + simple, 'java.lang'

        # 3. same package -- but only if the corpus really declares it
        if package and (package + '.' + simple) in declared:
            return package + '.' + simple, 'same-pkg'

        # 4. wildcard import, last and least. Only JDK wildcards are worth
        #    guessing at: a company or third-party wildcard would resolve to
        #    a non-blocker anyway, so an unknown there costs us nothing.
        for w in wildcards:
            if (w + '.').startswith(JDK_PREFIXES) and (w + '.' + simple) in declared:
                return w + '.' + simple, 'wildcard'
        for w in wildcards:
            if (w + '.').startswith(JDK_PREFIXES):
                return w + '.' + simple, 'wildcard'

        # Nothing resolved. Send it to the human queue rather than guessing.
        return None, 'unknown'

    if RUNTIME_TARGET.search(line):
        return None, 'runtime'

    return None, 'unknown'


# javap answers are stable per (class, member); the subprocess is not free.
_ACCESS_CACHE = {}

# Captures the member name out of getDeclaredMethod("x") / getDeclaredField("x").
MEMBER_NAME = re.compile(r'get(?:Declared)?(?:Method|Field)\(\s*"([^"]+)"')


def jdk_access_is_legal(fqn: str, member: str):
    """Would setAccessible on this JDK member succeed on Java 17+ unopened?

    AccessibleObject.setAccessible succeeds when the member is public AND its
    declaring class is public in an exported package. Every java.* package is
    exported (just not opened), so for JDK targets the question collapses to:
    are the class and the member both public?

    That distinction matters. `Integer.class.getDeclaredMethod("toString")`
    plus setAccessible is perfectly legal on 21 -- calling it a blocker sends
    someone chasing a crash that will never happen. Meanwhile the members that
    really break (Thread.threadLocals, ClassLoader.parent, the backing field of
    an unmodifiable collection) are all non-public, which is exactly why the
    reflection was there in the first place.

    Returns True (legal), False (will throw), or None (couldn't tell).

    NOTE: this asks the javap on PATH, so it describes that JDK's shape. Run it
    under the JDK you are migrating TO for an authoritative answer.
    """
    key = (fqn, member)
    if key in _ACCESS_CACHE:
        return _ACCESS_CACHE[key]

    result = None
    try:
        # Without -p, javap prints only the public and protected API. A member
        # missing from that listing is not public.
        out = subprocess.run(['javap', fqn], capture_output=True, text=True)
        if out.returncode == 0 and out.stdout.strip():
            lines = [ln for ln in out.stdout.splitlines()
                     if ln.strip() and not ln.startswith('Compiled from')]
            decl = lines[0] if lines else ''
            if not decl.lstrip().startswith('public'):
                result = False              # non-public class: nothing is reachable
            elif member is None:
                result = None               # bulk enumeration -- can't say
            else:
                needle = re.compile(rf'\b{re.escape(member)}\s*[(;]')
                result = any(needle.search(ln) and 'public' in ln
                             for ln in lines[1:])
    except (OSError, subprocess.SubprocessError):
        result = None

    _ACCESS_CACHE[key] = result
    return result


def classify(fqn, how, deep, modifiers_hack, company, member=None):
    """Map a resolved target to a migration bucket.

    Returns (bucket, is_blocker, confidence, note).

    Bucket meanings:
      MODIFIERS_HACK  the Field."modifiers" trick -- dead since Java 12
      REMOVED_API     target class no longer exists in the JDK
      JDK_INTERNAL    deep reflection into a JDK module -> the JEP 403 failure
      JDK_PUBLIC      reflection into the JDK, but public-only -> still legal
      JDK_OPEN        sun.misc / sun.reflect -> jdk.unsupported opens these
      OWN_CODE        our own target, classpath = unnamed module = open
      THIRD_PARTY     a jar's class; also unnamed module on the classpath
      OPAQUE          target not knowable from source -> human queue
    """
    if modifiers_hack:
        return ('MODIFIERS_HACK', True, 'high',
                'Field.class.getDeclaredField("modifiers") throws '
                'NoSuchFieldException on Java 12+; no flag re-enables it')

    if fqn is None:
        return ('OPAQUE', False, 'low',
                f'target unresolved ({how}); needs bytecode or human review')

    # Removed-API check runs before the jdk.unsupported allowance, because a
    # deleted class in an open package is still a deleted class.
    if fqn in REMOVED_TYPES or (fqn.startswith(REMOVED_PREFIXES)
                                and not fqn.startswith(STILL_PRESENT)):
        return ('REMOVED_API', True, 'high',
                'class removed from the JDK; expect ClassNotFoundException')

    if fqn.startswith(OPEN_JDK_PREFIXES):
        return ('JDK_OPEN', False, 'high',
                'jdk.unsupported opens this package, so it still works on 21 '
                '-- but it is deprecated for removal; raise a ticket')

    if fqn.startswith(JDK_PREFIXES):
        if not deep:
            return ('JDK_PUBLIC', False, 'medium',
                    'reflection into an exported JDK package without setAccessible '
                    '-- legal on 21 (verify the member really is public)')

        # Deep access into the JDK only fails when the member is out of reach.
        # A public member of a public class in an exported package is fine.
        legal = jdk_access_is_legal(fqn, member)
        if legal is True:
            return ('JDK_PUBLIC', False, 'high',
                    f'setAccessible on public member "{member}" of a public class '
                    'in an exported package -- legal on 21, no --add-opens needed')

        confidence = 'low' if how == 'wildcard' else (
            'high' if legal is False else 'medium')
        detail = ('member is not public, so the package must be opened'
                  if legal is False else
                  'could not confirm the member is public -- treated as a blocker')
        return ('JDK_INTERNAL', True, confidence,
                f'setAccessible into a JDK module ({detail}) -> '
                'InaccessibleObjectException; fix with a supported API, '
                'or --add-opens as a stopgap')

    if fqn.startswith(company):
        return ('OWN_CODE', False, 'high' if how != 'wildcard' else 'low',
                'classpath -> unnamed module -> open; unaffected by Java 21. '
                'Belongs to the later encapsulation cleanup, not this migration')

    return ('THIRD_PARTY', False, 'medium',
            'third-party jar on the classpath -> unnamed module -> open; '
            'unaffected by Java 21 unless that jar ships a module-info')


def scan_file_java21(path: Path, root: Path, company: str,
                     declared: set) -> list:
    """Produce one record per reflection site in one .java file."""
    text = path.read_text(encoding='utf-8', errors='replace')
    lines = text.splitlines()
    imports, wildcards, package = parse_file_context(text)
    rel = str(path.relative_to(root))

    # Pre-index setAccessible lines so we can ask "is this lookup deep?"
    # without rescanning the file for every site.
    setacc = {n for n, l in enumerate(lines, 1) if SET_ACCESSIBLE.search(l)}
    depths = block_depths(lines)
    claimed = set()          # setAccessible lines we managed to pair up
    sites = []

    for lineno, line in enumerate(lines, 1):
        if not (LOOKUP.search(line) or FORNAME_TARGET.search(line)):
            continue

        # Deep = a setAccessible on this line or shortly after it, inside the
        # same block. That call is what makes JDK targets fail, so it decides
        # blocker status.
        deep_at = None
        here = depths[lineno]
        for n in range(lineno, min(lineno + DEEP_WINDOW, len(lines)) + 1):
            if n in setacc:
                deep_at = n
                break
            if depths[n] < here:     # walked out of the enclosing block
                break
        if deep_at:
            claimed.add(deep_at)

        fqn, how = resolve_target(line, imports, wildcards, package, declared)
        hack = bool(MODIFIERS_HACK.search(line))
        member_hit = MEMBER_NAME.search(line)
        member = member_hit.group(1) if member_hit else None
        bucket, blocker, confidence, note = classify(
            fqn, how, deep_at is not None, hack, company, member)

        sites.append({
            'file': rel,
            'line': lineno,
            'target_fqn': fqn,
            'member': member,
            'resolution': how,
            'deep': deep_at is not None,
            'bucket': bucket,
            'java21_blocker': blocker,
            'confidence': confidence,
            'note': note,
            'snippet': line.strip()[:160],
        })

    # A setAccessible with no lookup nearby means the Method/Field object came
    # from somewhere else (a field, a helper, a loop). Can't resolve its target
    # from one line, so it goes straight to the human queue rather than being
    # silently dropped.
    for n in sorted(setacc - claimed):
        sites.append({
            'file': rel,
            'line': n,
            'target_fqn': None,
            'member': None,
            'resolution': 'orphan-setAccessible',
            'deep': True,
            'bucket': 'OPAQUE',
            'java21_blocker': False,
            'confidence': 'low',
            'note': 'setAccessible with no lookup within '
                    f'{DEEP_WINDOW} lines; target unknown -- human review',
            'snippet': lines[n - 1].strip()[:160],
        })

    return sites


def audit_java21(root: Path, company: str, declared: set = None,
                 artifact: Path = None) -> list:
    """Scan one repo and print the migration verdict. Returns the site records.

    `declared` is the set of types known across EVERY repo being scanned, so
    so that a call in one repo targeting a class declared in another still
    resolves. Pass every repo on one command line to get that. Falls back to
    this repo alone, which just sends more sites to the human queue.
    """
    java_files = list(root.rglob('*.java'))
    if declared is None:
        declared = collect_declared_types(java_files)
    sites = []
    for path in java_files:
        sites.extend(scan_file_java21(path, root, company, declared))

    # Second pass: anything source couldn't resolve gets another chance
    # against the compiled artifact, where the target is in the constant pool.
    escalation = None
    if artifact is not None:
        escalation = escalate(sites, artifact, company)

    blockers = [s for s in sites if s['java21_blocker']]
    buckets = Counter(s['bucket'] for s in sites)

    print(f'\n{"=" * 74}')
    print(f'JAVA 21 MIGRATION SCAN: {root}')
    print(f'{len(java_files)} java files, {len(sites)} reflection sites, '
          f'own-code prefix: {", ".join(company)}')
    print('=' * 74)

    if escalation:
        print('\n-- Bytecode escalation --')
        if 'error' in escalation:
            print(f'  skipped: {escalation["error"]}')
        else:
            print(f'  artifact layout      {escalation["layout"]}')
            print(f'  classes disassembled {escalation["classes"]}')
            print(f'  lookup sites in bytecode {escalation["bytecode_sites"]}')
            print(f'  OPAQUE sites matched to bytecode {escalation["matched"]}')
            print(f'  of those, target resolved        {escalation["upgraded"]}')
            if escalation['source_only_missed']:
                print(f'  bytecode sites with no source row: '
                      f'{escalation["source_only_missed"]} '
                      f'(source regexes missed these -- worth a look)')

    print('\n-- Sites by bucket --')
    for bucket, n in buckets.most_common():
        flag = 'BLOCKS JAVA 21' if bucket in (
            'MODIFIERS_HACK', 'REMOVED_API', 'JDK_INTERNAL') else ''
        print(f'  {bucket:<16} {n:>5}  {flag}')

    print(f'\n-- Java 21 blockers: {len(blockers)} --')
    if not blockers:
        print('  none. No reflection site in this repo blocks the 8 -> 21 move.')
        print('  (Confirm with: jdeps --jdk-internals -R over the jar AND its'
              ' dependency jars.)')
    else:
        for s in blockers:
            print(f'\n  {s["file"]}:{s["line"]}  [{s["bucket"]}]'
                  f'  confidence={s["confidence"]}')
            shown = s["target_fqn"]
            if s.get('member'):
                shown += '#' + s['member']
            print(f'    target: {shown}  (resolved via {s["resolution"]})')
            print(f'    why:    {s["note"]}')
            print(f'    code:   {s["snippet"]}')

    opaque = [s for s in sites if s['bucket'] == 'OPAQUE']
    print(f'\n-- Human queue (target not resolvable from source): {len(opaque)} --')
    for s in opaque[:15]:
        print(f'  {s["file"]}:{s["line"]}  {s["resolution"]}  {s["snippet"][:80]}')
    if len(opaque) > 15:
        print(f'  ... and {len(opaque) - 15} more (see the JSON output)')

    deprecated = [s for s in sites if s['bucket'] == 'JDK_OPEN']
    if deprecated:
        print(f'\n-- Works on 21 but deprecated for removal: {len(deprecated)} --')
        for s in deprecated[:10]:
            print(f'  {s["file"]}:{s["line"]}  {s["target_fqn"]}')

    return sites


# ===========================================================================
# Bytecode escalation  (--bytecode ARTIFACT)
# ===========================================================================
#
# WHY THIS EXISTS
#   The source scanner resolves a target only when the call site names it on
#   one line -- `Foo.class.getDeclaredMethod(...)` or a Class.forName string
#   literal. Real code usually doesn't:
#
#       Class<?> c = resolveSomehow();      // or Foo.class, several lines up
#       Method m = c.getDeclaredMethod("x");   <-- receiver is a variable
#       m.setAccessible(true);
#
#   Those land in OPAQUE, and a corpus can easily be 80% OPAQUE. That isn't a
#   finding, it's a blind spot: a migration verdict built on the resolved
#   minority understates the blocker count.
#
#   Compiled bytecode has the answer. `Foo.class` compiles to an `ldc` of a
#   constant-pool *class* entry, and Class.forName("a.b.C") to an `ldc` of a
#   String, both of which survive into the .class file no matter how many
#   locals the source put in between. Walking backwards from the lookup call
#   recovers the target exactly, with no type inference and no guessing.
#
# WHAT IT CANNOT DO
#   `obj.getClass().getDeclaredField(...)` has no constant-pool target -- the
#   class is whatever showed up at runtime. Those stay OPAQUE, correctly.
#
# LAYOUT RULES (from ROADMAP "artifact -> own_classes / dependency_jars")
#   Fat jars carry other people's classes. Getting this wrong means auditing
#   Spring Boot's loader or a bundled dependency and calling it our code.
#     BOOT-INF/classes/ present -> Spring Boot fat jar; own code lives there
#     WEB-INF/classes/  present -> WAR; own code lives there
#     neither                   -> plain jar; classes at the root are own code
#   Root classes count as own code ONLY in the plain-jar case.

import shutil
import subprocess
import tempfile
import zipfile

# Class methods whose result is a Method/Field/Constructor we might force open.
BYTECODE_LOOKUPS = (
    'getDeclaredMethod', 'getMethod', 'getDeclaredField', 'getField',
    'getDeclaredConstructor', 'getConstructor',
    'getDeclaredMethods', 'getMethods', 'getDeclaredFields', 'getFields',
    'getDeclaredConstructors', 'getConstructors',
)

# javap output shapes.
JAVAP_INSN = re.compile(r'^\s+(\d+): (\S+)(?:\s+#\d+)?(?:\s+// (.*))?$')
JAVAP_LINE = re.compile(r'^\s+line (\d+): (\d+)$')
JAVAP_MEMBER = re.compile(r'^  (?!Code:|LineNumberTable|LocalVariableTable|'
                          r'Exception table:|StackMapTable:|Signature:)(\S.*);$')
JAVAP_CLASS = re.compile(r'^[\w\s]*\b(?:class|interface|enum) ([\w.$]+)')
JAVAP_SOURCE = re.compile(r'^Compiled from "(.+)"$')

# How far back through a method's instructions to look for the target class.
# Bounded so a class literal belonging to some unrelated earlier statement
# can't be mistaken for this call's receiver.
BACKSCAN_LIMIT = 60

# javap takes class names as arguments, so very large corpora would blow the
# command-line length limit. Batch them; process startup dominates anyway.
JAVAP_BATCH = 150


def detect_layout(artifact: Path):
    """Classify an artifact and say where its OWN classes live.

    Returns (kind, own_prefix) where own_prefix is a path prefix inside the
    archive ('' means the archive root). See the layout rules above.
    """
    if artifact.is_dir():
        return 'dir', ''
    if not zipfile.is_zipfile(artifact):
        return 'unknown', ''
    with zipfile.ZipFile(artifact) as z:
        names = z.namelist()
    if any(n.startswith('BOOT-INF/classes/') for n in names):
        return 'boot', 'BOOT-INF/classes/'
    if any(n.startswith('WEB-INF/classes/') for n in names):
        return 'war', 'WEB-INF/classes/'
    return 'plain', ''


def stage_own_classes(artifact: Path, kind: str, own_prefix: str):
    """Give javap a directory whose root is the package root.

    A plain jar or an exploded directory already is one. Boot/WAR layouts bury
    classes under a prefix that `javap -classpath` cannot see through, so those
    entries get extracted to a temp dir first.

    Returns (classpath_for_javap, tempdir_to_clean_up_or_None).
    """
    if kind in ('dir', 'plain'):
        return str(artifact), None

    tmp = Path(tempfile.mkdtemp(prefix='reflection-audit-'))
    with zipfile.ZipFile(artifact) as z:
        for name in z.namelist():
            if not name.startswith(own_prefix) or not name.endswith('.class'):
                continue
            rel = name[len(own_prefix):]
            dest = tmp / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(z.read(name))
    return str(tmp), tmp


def own_class_names(classpath_root: str) -> list:
    """Every class FQN under a package root, inner classes included.

    Inner classes matter: an anonymous `Foo$1` holds its own bytecode, so a
    reflection site inside one is invisible if only `Foo` is disassembled.

    The root is a directory for exploded classes and for Boot/WAR layouts
    (which get staged into one), but a plain jar is handed to javap as-is --
    so that case enumerates out of the archive instead of walking the disk.
    """
    root = Path(classpath_root)
    if root.is_dir():
        entries = ['/'.join(path.relative_to(root).parts)
                   for path in root.rglob('*.class')]
    else:
        with zipfile.ZipFile(root) as z:
            entries = [n for n in z.namelist() if n.endswith('.class')]

    names = []
    for entry in entries:
        if entry.startswith('META-INF/'):
            continue
        fqn = entry[:-len('.class')].replace('/', '.')
        if fqn.endswith('module-info') or fqn.endswith('package-info'):
            continue
        names.append(fqn)
    return sorted(names)


def disassemble(classpath: str, fqns: list) -> str:
    """Run javap over many classes at once and concatenate the output."""
    chunks = []
    for i in range(0, len(fqns), JAVAP_BATCH):
        batch = fqns[i:i + JAVAP_BATCH]
        proc = subprocess.run(
            ['javap', '-p', '-c', '-l', '-classpath', classpath, *batch],
            capture_output=True, text=True)
        chunks.append(proc.stdout)
    return '\n'.join(chunks)


def parse_javap(out: str) -> dict:
    """javap text -> {class_fqn: {source, methods: [{sig, insns, lines}]}}.

    insns entries are (offset, opcode, comment); the comment is where javap
    prints the resolved constant-pool entry, which is the part we actually
    care about.
    """
    classes, cur, meth, source = {}, None, None, None
    for ln in out.splitlines():
        m = JAVAP_SOURCE.match(ln)
        if m:
            source = m.group(1)
            continue
        m = JAVAP_CLASS.match(ln)
        if m:
            cur = {'source': source, 'methods': [], 'fqn': m.group(1)}
            classes[m.group(1)] = cur
            meth = None
            continue
        if cur is None:
            continue
        m = JAVAP_MEMBER.match(ln)
        if m:
            meth = {'sig': m.group(1), 'insns': [], 'lines': []}
            cur['methods'].append(meth)
            continue
        if meth is None:
            continue
        m = JAVAP_INSN.match(ln)
        if m:
            meth['insns'].append((int(m.group(1)), m.group(2), m.group(3) or ''))
            continue
        m = JAVAP_LINE.match(ln)
        if m:
            meth['lines'].append((int(m.group(2)), int(m.group(1))))
    return classes


def source_line(meth: dict, offset: int):
    """Map an instruction offset to a source line via the LineNumberTable."""
    best = None
    for off, line in sorted(meth['lines']):
        if off <= offset:
            best = line
    return best


def resolve_from_constant_pool(insns: list, idx: int):
    """Walk backwards from a lookup call to find the class it targets.

    javac emits the receiver first, then the member name, then the parameter
    types, so scanning backwards the order is reversed: parameter class
    literals, then the member String, then the receiver class literal. That
    ordering is what tells a parameter type apart from the target -- exactly
    the trick examples/decide.py uses.

    Returns (target_fqn_or_None, member_name_or_None).
    """
    target = member = None
    stop = max(0, idx - BACKSCAN_LIMIT)
    for j in range(idx - 1, stop - 1, -1):
        op, comment = insns[j][1], insns[j][2]

        # Another reflective lookup means we've walked into a previous
        # statement; its operands are not ours.
        if any(f'java/lang/Class.{L}' in comment for L in BYTECODE_LOOKUPS):
            break

        if op.startswith('ldc') and comment.startswith('String ') and member is None:
            member = comment[len('String '):]
            continue

        if op.startswith('ldc') and comment.startswith('class '):
            fqn = comment[len('class '):].replace('/', '.')
            if member is None:
                continue                       # a parameter type, not the target
            if target is None and fqn != 'java.lang.Class':
                target = fqn
            continue

        # Class.forName("a.b.C") -- the name is an ldc String just before it.
        if 'java/lang/Class.forName' in comment and target is None:
            for k in range(j - 1, max(0, j - 10) - 1, -1):
                if insns[k][1].startswith('ldc') and insns[k][2].startswith('String '):
                    target = insns[k][2][len('String '):]
                    break
    return target, member


def is_deep(insns: list, idx: int) -> bool:
    """Does the object this lookup returned get setAccessible(true) called on it?

    Scans forward to the next lookup (or the end of the method) rather than a
    fixed window, so it tracks the actual statement boundary.
    """
    for j in range(idx + 1, len(insns)):
        comment = insns[j][2]
        if 'setAccessible' in comment:
            return True
        if any(f'java/lang/Class.{L}' in comment for L in BYTECODE_LOOKUPS):
            return False
    return False


def bytecode_sites(classes: dict) -> list:
    """Every reflective lookup found in bytecode, with its resolved target."""
    sites = []
    for fqn, cls in classes.items():
        if not cls['source']:
            continue
        package = fqn.rsplit('.', 1)[0] if '.' in fqn else ''
        # Where this class's source sits relative to the package root, e.g.
        # com/example/app/TypeUtil.java -- the key we join to the source scan.
        src_key = (package.replace('.', '/') + '/' + cls['source']).lstrip('/')
        for meth in cls['methods']:
            for i, (off, op, comment) in enumerate(meth['insns']):
                if not any(f'java/lang/Class.{L}' in comment
                           for L in BYTECODE_LOOKUPS):
                    continue
                target, member = resolve_from_constant_pool(meth['insns'], i)
                sites.append({
                    'src_key': src_key,
                    'basename': cls['source'],
                    'line': source_line(meth, off),
                    'owner': fqn,
                    'target_fqn': target,
                    'member': member,
                    'deep': is_deep(meth['insns'], i),
                })
    return sites


def build_resolution_index(sites: list) -> dict:
    """(source basename, line) -> [site, ...] for joining back to source rows."""
    index = collections.defaultdict(list)
    for s in sites:
        if s['line'] is not None:
            index[(s['basename'], s['line'])].append(s)
    return index


def escalate(source_sites: list, artifact: Path, company: tuple) -> dict:
    """Re-resolve OPAQUE source sites using the compiled artifact.

    Mutates source_sites in place. Returns a stats dict for the report.
    """
    kind, own_prefix = detect_layout(artifact)
    if kind == 'unknown':
        return {'error': f'{artifact} is neither a directory nor a jar/war'}

    classpath, tmp = stage_own_classes(artifact, kind, own_prefix)
    try:
        fqns = own_class_names(classpath)
        if not fqns:
            return {'error': f'no .class files found in {artifact} (layout={kind})'}
        classes = parse_javap(disassemble(classpath, fqns))
        bsites = bytecode_sites(classes)
        index = build_resolution_index(bsites)

        upgraded = matched = 0
        for s in source_sites:
            if s['bucket'] != 'OPAQUE':
                continue
            # Join on file name + line, then confirm the package path agrees --
            # two classes can share a basename across packages.
            src_path = s['file'].replace('\\', '/')
            candidates = [b for b in index.get((Path(src_path).name, s['line']), [])
                          if src_path.endswith(b['src_key'])]
            if not candidates:
                continue
            matched += 1
            hit = candidates[0]
            if not hit['target_fqn']:
                continue                      # genuinely runtime-typed; leave OPAQUE
            bucket, blocker, confidence, note = classify(
                hit['target_fqn'], 'bytecode', hit['deep'], False, company,
                hit['member'])
            s.update({
                'target_fqn': hit['target_fqn'],
                'resolution': 'bytecode',
                'deep': hit['deep'],
                'bucket': bucket,
                'java21_blocker': blocker,
                'confidence': confidence,
                'note': note,
                'member': hit['member'],
            })
            upgraded += 1

        # Bytecode sites with no source row are a coverage warning: the source
        # scanner's regexes missed a call shape it should have caught.
        src_keys = {(Path(s['file'].replace('\\', '/')).name, s['line'])
                    for s in source_sites}
        unmatched = sum(1 for b in bsites
                        if b['line'] is not None
                        and (b['basename'], b['line']) not in src_keys)

        return {'layout': kind, 'classes': len(fqns), 'bytecode_sites': len(bsites),
                'matched': matched, 'upgraded': upgraded,
                'source_only_missed': unmatched}
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


def main() -> None:
    # argparse rather than slicing sys.argv: a hand-rolled parser treats any
    # unrecognised flag as a path, so a typo like --java-21 surfaces as
    # "not a directory: --java-21" instead of naming the actual problem.
    parser = argparse.ArgumentParser(
        prog='reflection_audit.py',
        description='Audit Java reflection, and triage it for a Java 8 -> 21 move.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            'examples:\n'
            '  # inventory: pattern counts, hot files, clone check\n'
            '  reflection_audit.py /path/to/repo\n'
            '\n'
            '  # migration triage across two repos, full inventory to JSON\n'
            '  reflection_audit.py --java21 --json sites.json /path/repo-a /path/repo-b\n'
            '\n'
            '  # same, resolving targets that source alone cannot name\n'
            '  reflection_audit.py --java21 --bytecode /path/app.war /path/repo-a\n'))

    parser.add_argument(
        'repos', nargs='+', metavar='REPO',
        help='repo root(s) to scan. Pass every repo at once so a call in one '
             'that targets a class declared in another still resolves.')
    parser.add_argument(
        '--java21', '--java-21', dest='java21', action='store_true',
        help='classify every site as blocking / not blocking the Java 8 -> 21 '
             'migration, and say why. Without it, the original inventory runs.')
    parser.add_argument(
        '--company', metavar='PREFIX', dest='company',
        help='package prefix meaning "our code". Overrides COMPANY_PREFIX from '
             'the environment or .env (see .env.example). Comma-separate '
             'several roots.')
    parser.add_argument(
        '--bytecode', metavar='ARTIFACT', dest='bytecode',
        help='jar, war, or exploded-classes dir built from the same source. '
             'Resolves targets the source scanner cannot name. Needs javap; '
             'use the JDK you are migrating TO.')
    parser.add_argument(
        '--json', metavar='OUT.json', dest='json_path',
        help='write the full site inventory as JSON for the fix table.')

    opts = parser.parse_args()

    company = resolve_company_prefixes(opts.company)

    artifact = None
    if opts.bytecode:
        artifact = Path(opts.bytecode).expanduser().resolve()
        if not artifact.exists():
            parser.error(f'no such artifact: {artifact}')
        if not opts.java21:
            parser.error('--bytecode only applies to --java21')

    roots = []
    for arg in opts.repos:
        root = Path(arg).expanduser().resolve()
        if not root.is_dir():
            parser.error(f'not a directory: {root}')
        roots.append(root)

    # One shared type index across every repo on the command line, built
    # before any scanning. A cross-repo call only resolves if both the
    # calling repo and the declaring repo are in this set.
    declared = set()
    if opts.java21:
        for root in roots:
            declared |= collect_declared_types(list(root.rglob('*.java')))

    all_sites = []
    for root in roots:
        if opts.java21:
            for s in audit_java21(root, company, declared, artifact):
                s['repo'] = root.name
                all_sites.append(s)
        else:
            audit_repo(root)

    if opts.json_path:
        Path(opts.json_path).write_text(json.dumps(all_sites, indent=2))
        print(f'\nwrote {len(all_sites)} site records -> {opts.json_path}')


if __name__ == '__main__':
    main()

# ---------------------------------------------------------------------------
# ESCALATION PATH (not built yet -- deliberate)
# ---------------------------------------------------------------------------
# Sites landing in OPAQUE have a target this line-based scanner can't name.
# The deterministic next step is examples/decide.py: javap dumps the compiled
# constant pool, which holds the *resolved* target type, so opaque sites become
# exact ones with no guessing. Wire that in once the OPAQUE count justifies it.
