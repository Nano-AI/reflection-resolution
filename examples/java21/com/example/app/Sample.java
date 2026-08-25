package com.example.app;

import java.lang.reflect.Field;
import java.lang.reflect.Method;
import sun.misc.Unsafe;
import java.util.ArrayList;
import org.apache.commons.lang.StringUtils;

public class Sample {

    // 1. OWN_CODE -- sibling class, no import needed. Not a Java 21 blocker.
    void ownCode() throws Exception {
        Method m = Order.class.getDeclaredMethod("recalculate");
        m.setAccessible(true);
        m.invoke(new Order());
    }

    // 2. JDK_INTERNAL -- deep reflection into java.lang. Blocker.
    void jdkDeep() throws Exception {
        Field f = String.class.getDeclaredField("value");
        f.setAccessible(true);
    }

    // 3. JDK_PUBLIC -- reflection into the JDK, no setAccessible. Legal.
    void jdkPublic() throws Exception {
        ArrayList.class.getDeclaredMethod("size");
    }

    // 4. MODIFIERS_HACK -- dead since Java 12. Blocker.
    void stripFinal() throws Exception {
        Field mods = Field.class.getDeclaredField("modifiers");
        mods.setAccessible(true);
    }

    // 5. JDK_OPEN -- jdk.unsupported opens sun.misc. Works, but deprecated.
    void unsafe() throws Exception {
        Field f = Unsafe.class.getDeclaredField("theUnsafe");
        f.setAccessible(true);
    }

    // 6. REMOVED_API -- class deleted from the JDK. Blocker.
    void removed() throws Exception {
        Class<?> c = Class.forName("sun.misc.BASE64Encoder");
        c.getDeclaredMethod("encode", byte[].class);
    }

    // 7. THIRD_PARTY -- jar on the classpath, unnamed module. Not a blocker.
    void thirdParty() throws Exception {
        Method m = StringUtils.class.getDeclaredMethod("isEmpty", String.class);
        m.setAccessible(true);
    }

    // 8. OPAQUE -- target only known at runtime.
    void opaque(Object o) throws Exception {
        o.getClass().getDeclaredField("secret").setAccessible(true);
    }

    // 9. OPAQUE -- orphan setAccessible, lookup happened elsewhere.
    void orphan(Field cached) {
        cached.setAccessible(true);
    }

    // 10. REMOVED_API via literal -- Java EE module dropped in Java 11.
    void jaxb() throws Exception {
        Class.forName("javax.xml.bind.JAXBContext").getDeclaredMethod("newInstance");
    }
}
