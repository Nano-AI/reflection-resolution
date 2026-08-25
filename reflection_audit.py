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
LOOKUP = re.compile(r'\.get(?:Declared)?(?:Method|Field|Constructor)s?\s*\(')

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


def classify(fqn, how, deep, modifiers_hack, company):
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
        if deep:
            return ('JDK_INTERNAL', True,
                    'low' if how == 'wildcard' else 'high',
                    'setAccessible into a JDK module -> InaccessibleObjectException; '
                    'fix with a supported API, or --add-opens as a stopgap')
        return ('JDK_PUBLIC', False, 'medium',
                'reflection into an exported JDK package without setAccessible '
                '-- legal on 21 (verify the member really is public)')

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
        bucket, blocker, confidence, note = classify(
            fqn, how, deep_at is not None, hack, company)

        sites.append({
            'file': rel,
            'line': lineno,
            'target_fqn': fqn,
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


def audit_java21(root: Path, company: str, declared: set = None) -> list:
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

    blockers = [s for s in sites if s['java21_blocker']]
    buckets = Counter(s['bucket'] for s in sites)

    print(f'\n{"=" * 74}')
    print(f'JAVA 21 MIGRATION SCAN: {root}')
    print(f'{len(java_files)} java files, {len(sites)} reflection sites, '
          f'own-code prefix: {", ".join(company)}')
    print('=' * 74)

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
            print(f'    target: {s["target_fqn"]}  (resolved via {s["resolution"]})')
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


def main() -> None:
    args = sys.argv[1:]

    java21 = '--java21' in args
    if java21:
        args.remove('--java21')

    cli_company = None
    if '--company' in args:
        i = args.index('--company')
        cli_company = args[i + 1]
        del args[i:i + 2]
    company = resolve_company_prefixes(cli_company)

    json_path = None
    if '--json' in args:
        i = args.index('--json')
        json_path = args[i + 1]
        del args[i:i + 2]

    if not args:
        sys.exit(
            'usage: python3 reflection_audit.py [--java21] [--company PREFIX]\n'
            '                                  [--json OUT.json] /path/to/repo [...]\n'
            '\n'
            '  (no flags)   original audit: pattern counts, hot files, clone check\n'
            '  --java21     classify every site as blocking / not blocking the\n'
            '               Java 8 -> 21 migration, and say why\n'
            '  --company    package prefix that means "our code"; overrides\n'
            '               COMPANY_PREFIX from the environment or .env\n'
            '               (see .env.example; comma-separate several roots)\n'
            '  --json       write the full site inventory as JSON for the fix table')

    roots = []
    for arg in args:
        root = Path(arg).resolve()
        if not root.is_dir():
            sys.exit(f'not a directory: {root}')
        roots.append(root)

    # One shared type index across every repo on the command line, built
    # before any scanning. A cross-repo call only resolves if both the
    # calling repo and the declaring repo are in this set.
    declared = set()
    if java21:
        for root in roots:
            declared |= collect_declared_types(list(root.rglob('*.java')))

    all_sites = []
    for root in roots:
        if java21:
            for s in audit_java21(root, company, declared):
                s['repo'] = root.name
                all_sites.append(s)
        else:
            audit_repo(root)

    if json_path:
        Path(json_path).write_text(json.dumps(all_sites, indent=2))
        print(f'\nwrote {len(all_sites)} site records -> {json_path}')


if __name__ == '__main__':
    main()

# ---------------------------------------------------------------------------
# ESCALATION PATH (not built yet -- deliberate)
# ---------------------------------------------------------------------------
# Sites landing in OPAQUE have a target this line-based scanner can't name.
# The deterministic next step is examples/decide.py: javap dumps the compiled
# constant pool, which holds the *resolved* target type, so opaque sites become
# exact ones with no guessing. Wire that in once the OPAQUE count justifies it.
