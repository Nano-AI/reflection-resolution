package com.example.fix.other;
import com.example.fix.Target;
import java.lang.reflect.Method;

public class CrossPkgHack {
    // MANUAL: different package, so a direct call needs secretB() public --
    // a real API commitment, not a mechanical edit.
    void reach(Target t) throws Exception {
        Method m = Target.class.getDeclaredMethod("secretB");
        m.setAccessible(true);
        m.invoke(t);
    }
}
