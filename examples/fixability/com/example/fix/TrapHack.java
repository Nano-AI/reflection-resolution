package com.example.fix;
import java.lang.reflect.Method;

public class TrapHack {
    // MANUAL: widening Trapped.who() would turn TrappedChild.who() into a
    // real override and silently redirect dispatch.
    void poke(Trapped t) throws Exception {
        Method m = Trapped.class.getDeclaredMethod("who");
        m.setAccessible(true);
        m.invoke(t);
    }
}
