#!/usr/bin/env python3
"""PATCH layer demo: tree-sitter source edits for the AUTO tier.

Two edit kinds:
  1. change a member's modifier            (private -> package-private/public)
  2. delete a reflective chain + fix fallout (imports, orphaned catch clauses)

Edits are (start_byte, end_byte, replacement) triples applied in REVERSE offset
order so earlier offsets stay valid.
"""
import sys
from tree_sitter import Language, Parser
import tree_sitter_java

PARSER = Parser(Language(tree_sitter_java.language()))
REFLECT_EXC = {'NoSuchMethodException', 'NoSuchFieldException',
               'IllegalAccessException', 'InvocationTargetException',
               'ClassNotFoundException', 'ReflectiveOperationException'}


def walk(node):
    yield node
    for c in node.children:
        yield from walk(c)


def text(src, n):
    return src[n.start_byte:n.end_byte].decode()


def apply_edits(src: bytes, edits) -> bytes:
    """Reverse-order splice: later edits first, so earlier byte offsets hold."""
    for start, end, repl in sorted(edits, key=lambda e: -e[0]):
        src = src[:start] + repl.encode() + src[end:]
    return src


# ---- edit 1: modifier change -------------------------------------------------
def modifier_edit(src, tree, member, new_vis=''):
    """Rewrite the `modifiers` node of `member`. new_vis='' => package-private."""
    for n in walk(tree.root_node):
        if n.type not in ('method_declaration', 'field_declaration'):
            continue
        name = n.child_by_field_name('name')
        if not (name and text(src, name) == member):
            continue
        mods = next((c for c in n.children if c.type == 'modifiers'), None)
        if mods is None:
            return None
        kept = [text(src, c) for c in mods.children
                if text(src, c) not in ('private', 'protected', 'public')]
        repl = ' '.join(([new_vis] if new_vis else []) + kept)
        return (mods.start_byte, mods.end_byte, repl)
    return None


# ---- edit 2: delete a reflective chain --------------------------------------
def enclosing(node, types):
    while node is not None and node.type not in types:
        node = node.parent
    return node


def reflection_edits(src, tree, line):
    """Delete the statement holding setAccessible at `line`, plus its fallout."""
    edits, notes = [], []
    site = None
    for n in walk(tree.root_node):
        if (n.type == 'method_invocation'
                and n.start_point[0] + 1 == line
                and (nm := n.child_by_field_name('name'))
                and text(src, nm) == 'setAccessible'):
            site = n
            break
    if site is None:
        return edits, ['no setAccessible found on that line']

    stmt = enclosing(site, {'expression_statement', 'local_variable_declaration'})
    edits.append((stmt.start_byte, stmt.end_byte, ''))          # delete the statement
    notes.append(f'delete: {text(src, stmt).strip()}')

    # the whole chain must go, not just this line: find the receiver variable,
    # its lookup declaration, and the .invoke() that follows.
    recv = site.child_by_field_name('object')
    method_body = enclosing(site, {'method_declaration'})
    if recv is not None and recv.type == 'identifier' and method_body is not None:
        var = text(src, recv)
        member = None
        for n in walk(method_body):
            # the lookup: Method m = X.class.getDeclaredMethod("member", ...)
            if n.type == 'local_variable_declaration' and f' {var} ' in text(src, n) + ' ':
                for c in walk(n):
                    if (c.type == 'method_invocation'
                            and (nm := c.child_by_field_name('name'))
                            and text(src, nm) in ('getDeclaredMethod', 'getMethod')):
                        args = c.child_by_field_name('arguments')
                        lit = next((g for g in walk(args) if g.type == 'string_fragment'), None)
                        if lit is not None:
                            member = text(src, lit)
                            edits.append((n.start_byte, n.end_byte, ''))
                            notes.append(f'delete lookup: {text(src, n).strip()}')
            # the call: m.invoke(receiver, arg...)  ->  receiver.member(arg...)
            if (n.type == 'method_invocation'
                    and (nm := n.child_by_field_name('name')) and text(src, nm) == 'invoke'
                    and (ob := n.child_by_field_name('object')) is not None
                    and text(src, ob) == var and member):
                argv = [text(src, a) for a in n.child_by_field_name('arguments').children
                        if a.type not in ('(', ')', ',')]
                if argv:
                    direct = f'{argv[0]}.{member}({", ".join(argv[1:])})'
                    edits.append((n.start_byte, n.end_byte, direct))
                    notes.append(f'rewrite invoke -> {direct}')

    # fallout A: catch clauses for reflection-only exceptions in the enclosing try
    try_node = enclosing(site, {'try_statement'})
    if try_node:
        for c in walk(try_node):
            if c.type == 'catch_type':
                names = {text(src, g) for g in c.children if g.type == 'type_identifier'}
                if names and names <= REFLECT_EXC:
                    clause = enclosing(c, {'catch_clause'})
                    edits.append((clause.start_byte, clause.end_byte, ''))
                    notes.append(f'delete now-unreachable catch ({"|".join(sorted(names))})')
        remaining = [c for c in walk(try_node) if c.type == 'catch_clause']
        has_finally = any(c.type == 'finally_clause' for c in walk(try_node))
        if len(remaining) <= 1 and not has_finally:
            notes.append('BLOCKER: try would have no catch/finally left — the try must be '
                         'unwrapped too. Needs knowing whether anything else in the block '
                         'throws. Emit for human review; do not auto-apply.')

    # fallout B: java.lang.reflect imports left unused
    body = src.decode()
    for n in walk(tree.root_node):
        if n.type == 'import_declaration' and 'java.lang.reflect' in text(src, n):
            simple = text(src, n).rstrip(';').split('.')[-1]
            if simple != '*' and body.count(simple) <= 2:
                edits.append((n.start_byte, n.end_byte, ''))
                notes.append(f'delete unused import of {simple}')
            elif simple == '*':
                notes.append('CHECK: wildcard reflect import may now be unused')
    return edits, notes


if __name__ == '__main__':
    path, mode = sys.argv[1], sys.argv[2]
    src = open(path, 'rb').read()
    tree = PARSER.parse(src)

    if mode == 'modifier':
        e = modifier_edit(src, tree, sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else '')
        print('edit:', e)
        print(apply_edits(src, [e]).decode())
    else:
        edits, notes = reflection_edits(src, tree, int(sys.argv[3]))
        for n in notes:
            print('  •', n)
        print('--- result ---')
        print(apply_edits(src, edits).decode())
