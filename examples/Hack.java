package com.x;
import java.lang.reflect.*;
public class Hack {
  // three different SOURCE styles, same target
  void styleA(Order o) throws Exception {
    Order.class.getDeclaredMethod("recalc", String.class).setAccessible(true);
  }
  void styleB(Order o) throws Exception {
    Class<?> c = Class.forName("com.x.Order");
    Method m = c.getDeclaredMethod("recalc", String.class);
    m.setAccessible(true);
    m.invoke(o, "x");
  }
  void styleC(Order o) throws Exception {
    Method mm; { mm = o.getClass().getDeclaredMethod("recalc", String.class); }
    mm.setAccessible(true);
  }
}
