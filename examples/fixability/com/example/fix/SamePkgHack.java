package com.example.fix;
import java.lang.reflect.Method;

public class SamePkgHack {
    // ASSISTED: private -> package-private, same package, no subclass shadow.
    void reachPrivate(Target t) throws Exception {
        Method m = Target.class.getDeclaredMethod("secretA");
        m.setAccessible(true);
        m.invoke(t);
    }

    // AUTO: openC() is already public, so the reflection buys nothing.
    void reachPublic(Target t) throws Exception {
        Method m = Target.class.getDeclaredMethod("openC");
        m.setAccessible(true);
        m.invoke(t);
    }
}
