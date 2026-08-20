package com.relayterm.app;

import java.nio.charset.StandardCharsets;

/** Lightweight JVM smoke tests; executable with the repository JDK directly. */
public final class AnsiTerminalModelTest {
    private static void check(boolean condition, String message) {
        if (!condition) throw new AssertionError(message);
    }

    public static void main(String[] args) {
        AnsiTerminalModel model = new AnsiTerminalModel(12, 4);
        model.feed(new byte[]{0x1b, '['});
        model.feed("31mred\u001b[0m".getBytes(StandardCharsets.UTF_8));
        check("red".equals(model.text().substring(0, 3)), "fragmented SGR");
        check(model.cellAt(0, 0).foreground == 1, "red SGR state");

        model.reset();
        model.feed("中e\u0301".getBytes(StandardCharsets.UTF_8));
        check(model.cellAt(0, 0).text.startsWith("中"), "CJK glyph");
        check(model.cellAt(1, 0).wideContinuation, "CJK width");
        check(model.cellAt(2, 0).text.startsWith("e"), "combining glyph");

        model.reset();
        model.feed("main\u001b[?1049halt\u001b[?1049l".getBytes(StandardCharsets.UTF_8));
        check(model.text().startsWith("main"), "alternate screen restore");
        check(!model.isAlternateScreen(), "alternate flag");

        model.reset();
        model.feed("a\nb\nc\nd\ne".getBytes(StandardCharsets.UTF_8));
        check(model.scrollbackSnapshot().size() > 0, "scrollback");
        model.resize(20, 6);
        check(model.getColumns() == 20 && model.getRows() == 6, "resize");
    }
}
