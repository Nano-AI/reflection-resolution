package com.example.app;

import java.lang.reflect.Field;
import java.lang.reflect.Method;

/** Call shapes the source scanner cannot resolve on its own. Compile this and
 *  pass the output to --bytecode; the constant pool names every target except
 *  the genuinely runtime-typed one. */
public class Deep {
    private Method cached;
    static final String N = "java.lang.Thread";

    // Receiver is a variable and the class literal is several lines up.
    // Source: OPAQUE. Bytecode: java.lang.ClassLoader#parent -> BLOCKER.
    void variableReceiverJdk() throws Exception {
        Class<?> c = ClassLoader.class;
        String noise = "unrelated";
        int k = noise.length();
        Field f = c.getDeclaredField("parent");
        f.setAccessible(true);
    }

    // Same shape, our own class. Source: OPAQUE. Bytecode: OWN_CODE.
    void variableReceiverOwn() throws Exception {
        Class<?> c = Order.class;
        Method m = c.getDeclaredMethod("recalculate");
        m.setAccessible(true);
    }

    // forName off a compile-time String constant; javac inlines it, so the
    // name survives as an ldc. Bytecode: java.lang.Thread#threadLocals.
    void forNameConstant() throws Exception {
        Class<?> k = Class.forName(N);
        Field f = k.getDeclaredField("threadLocals");
        f.setAccessible(true);
    }

    // Method kept in a field, so source only sees an orphan setAccessible.
    // Target is java.lang.Integer#toString -- public, therefore NOT a blocker.
    void cachedMember() throws Exception {
        cached = Integer.class.getDeclaredMethod("toString");
        cached.setAccessible(true);
    }

    // Public JDK member, no setAccessible at all. Legal on 21.
    void publicLookup() throws Exception {
        Class<?> c = java.util.ArrayList.class;
        c.getDeclaredMethod("size");
    }

    // Genuinely runtime-typed. Stays OPAQUE even with bytecode -- correctly.
    void runtimeType(Object o) throws Exception {
        o.getClass().getDeclaredField("secret").setAccessible(true);
    }
}
