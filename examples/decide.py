#!/usr/bin/env python3
"""DECIDE layer demo: javap -> sites -> targets -> callers -> verdict."""
import re, subprocess, sys
from collections import defaultdict

INSN = re.compile(r'^\s+(\d+): (\S+)(?:\s+#\d+)?(?:\s+// (.*))?$')
LINE = re.compile(r'^\s+line (\d+): (\d+)$')
MEMBER = re.compile(r'^  (?!Code:|LineNumberTable|LocalVariableTable)(\S.*);$')

LOOKUPS = ('getDeclaredMethod', 'getMethod', 'getDeclaredField',
           'getField', 'getDeclaredConstructor')


def disassemble(classpath, fqns):
    """One javap call for many classes -- process startup dominates."""
    out = subprocess.run(['javap', '-p', '-c', '-l', '-classpath', classpath, *fqns],
                         capture_output=True, text=True).stdout
    return out


def parse(out):
    """javap text -> {class_fqn: {source, methods: [{name, insns, lines}]}}"""
    classes, cur, meth = {}, None, None
    source = None
    for ln in out.splitlines():
        if ln.startswith('Compiled from'):
            source = ln.split('"')[1]; continue
        m = re.match(r'^(?:public |final |abstract )*(?:class|interface|enum) (\S+)', ln)
        if m:
            cur = {'source': source, 'methods': [], 'fqn': m.group(1)}
            classes[m.group(1)] = cur; meth = None; continue
        if cur is None:
            continue
        m = MEMBER.match(ln)
        if m:
            meth = {'sig': m.group(1), 'insns': [], 'lines': []}
            cur['methods'].append(meth); continue
        if meth is None:
            continue
        m = INSN.match(ln)
        if m:
            meth['insns'].append((int(m.group(1)), m.group(2), m.group(3) or ''))
            continue
        m = LINE.match(ln)
        if m:
            meth['lines'].append((int(m.group(2)), int(m.group(1))))   # (offset, srcline)
    return classes


def src_line(meth, offset):
    """LineNumberTable: largest entry offset <= instruction offset."""
    best = None
    for off, line in sorted(meth['lines']):
        if off <= offset:
            best = line
    return best


def find_sites(classes):
    sites = []
    for fqn, c in classes.items():
        for meth in c['methods']:
            for i, (off, op, com) in enumerate(meth['insns']):
                if re.search(r'java/lang/reflect/\w+\.setAccessible', com):
                    sites.append({'owner': fqn, 'source': c['source'],
                                  'method': meth['sig'], 'offset': off,
                                  'line': src_line(meth, off),
                                  'target': resolve_target(meth['insns'], i)})
    return sites


def resolve_target(insns, site_idx):
    """Backward scan: setAccessible <- lookup <- ldc member <- ldc class."""
    lookup = None
    for j in range(site_idx - 1, -1, -1):
        if any(f'java/lang/Class.{L}' in insns[j][2] for L in LOOKUPS):
            lookup = j; break
    if lookup is None:
        return {'case': 'opaque'}

    member = klass = None
    params = []
    for j in range(lookup - 1, -1, -1):
        op, com = insns[j][1], insns[j][2]
        if op.startswith('ldc') and com.startswith('String ') and member is None:
            member = com[len('String '):]
            continue
        if op.startswith('ldc') and com.startswith('class '):
            t = com[len('class '):].replace('/', '.')
            if member is None:
                params.insert(0, t)                    # param type, seen before member
            elif klass is None and t != 'java.lang.Class':
                klass = t                              # class literal form
        if 'java/lang/Class.forName' in com and klass is None:
            for k in range(j - 1, -1, -1):             # forName("com.x.Order")
                if insns[k][1].startswith('ldc') and insns[k][2].startswith('String '):
                    klass = insns[k][2][len('String '):]; break
    if member is None:
        return {'case': 'opaque'}
    return {'case': 'resolved', 'class': klass, 'member': member, 'params': params}


def build_callers(classes):
    """member key -> set of calling classes. Bytecode owners are exact."""
    callers = defaultdict(set)
    for fqn, c in classes.items():
        for meth in c['methods']:
            for _, op, com in meth['insns']:
                m = re.match(r'Method (?:([\w/$]+)\.)?(\w+):(\(.*)', com)
                if not m:
                    continue
                owner = (m.group(1).replace('/', '.') if m.group(1) else fqn)
                callers[f'{owner}#{m.group(2)}'].add(fqn)
    return callers


def modifiers(classpath, fqn, member):
    out = subprocess.run(['javap', '-p', '-classpath', classpath, fqn],
                         capture_output=True, text=True).stdout
    for ln in out.splitlines():
        if re.search(rf'\b{re.escape(member)}\(', ln):
            return ln.strip().rstrip(';')
    return None


def verdict(site, callers, classpath):
    t = site['target']
    if t['case'] != 'resolved' or not t.get('class'):
        return 'needs a human decision — target class not statically resolvable'
    key = f"{t['class']}#{t['member']}"
    who = callers.get(key, set()) - {site['owner']}          # drop the reflective site
    external = who - {t['class']}                            # drop the declaring class
    decl = modifiers(classpath, t['class'], t['member'])
    pkg = t['class'].rsplit('.', 1)[0]
    if not external:
        return (f"no visibility change needed — `{decl}` has no callers outside "
                f"{t['class']}; delete the reflection and call it directly.")
    if all(c.rsplit('.', 1)[0] == pkg for c in external):
        return (f"reduce to package-private — callers {sorted(external)} all live in "
                f"{pkg}.")
    return (f"needs a human decision — callers in other packages {sorted(external)}; "
            f"exposing this means committing to it as public API.")


if __name__ == '__main__':
    cp, fqns = sys.argv[1], sys.argv[2:]
    classes = parse(disassemble(cp, fqns))
    callers = build_callers(classes)
    for s in find_sites(classes):
        t = s['target']
        tgt = f"{t.get('class')}#{t.get('member')}{tuple(t.get('params', []))}" \
              if t['case'] == 'resolved' else '<opaque>'
        print(f"\n{s['source']}:{s['line']}  in {s['owner']}.{s['method'].split()[1].split('(')[0]}")
        print(f"  target : {tgt}")
        print(f"  verdict: {verdict(s, callers, cp)}")
