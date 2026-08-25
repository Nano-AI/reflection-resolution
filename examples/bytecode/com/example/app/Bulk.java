package com.example.app;

import java.lang.reflect.Field;

/** Regression fixture: a bulk enumeration takes NO member name, so the nearest
 *  ldc String belongs to an earlier statement. Resolving it as a member would
 *  invent a fact, and the invented member would then legitimise the next class
 *  literal as its owner. Both must come back null. */
public class Bulk {
    Field[] enumerate() throws Exception {
        Class<?> t = Order.class;
        String label = "recalculate";
        System.out.println(label);
        Field[] fields = t.getDeclaredFields();
        for (Field f : fields) { f.setAccessible(true); }
        return fields;
    }
}
