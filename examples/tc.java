package com.x;
import java.lang.reflect.Method;
public class T {
  void go(Order o) {
    try {
      Method m = Order.class.getDeclaredMethod("recalc", String.class);
      m.setAccessible(true);
      m.invoke(o, "x");
    } catch (NoSuchMethodException | IllegalAccessException e) {
      throw new RuntimeException(e);
    }
  }
}
