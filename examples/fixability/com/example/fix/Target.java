package com.example.fix;
import java.lang.reflect.Method;

public class Target {
    private void secretA() { }
    private void secretB() { }
    public void openC() { }
    private void secretD() { }

    // AUTO: the hack is inside the declaring class, so a direct call needs
    // no modifier change at all.
    void reflectOnSelf() throws Exception {
        Method m = Target.class.getDeclaredMethod("secretD");
        m.setAccessible(true);
        m.invoke(this);
    }
}
