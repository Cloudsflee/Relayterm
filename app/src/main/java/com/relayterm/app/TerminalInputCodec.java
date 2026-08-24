package com.relayterm.app;

import java.nio.charset.StandardCharsets;

/**
 * Encodes terminal input in one place so Android key events and IME commits
 * use the same byte representation.
 *
 * <p>PTY input is a byte stream. Printable text is therefore always encoded
 * as UTF-8, while control keys use their traditional VT/ASCII sequences.</p>
 */
public final class TerminalInputCodec {
    public enum Key {
        ESCAPE,
        TAB,
        ENTER,
        BACKSPACE,
        UP,
        DOWN,
        LEFT,
        RIGHT,
        HOME,
        END,
        PAGE_UP,
        PAGE_DOWN
    }

    private TerminalInputCodec() { }

    /** Tracks an IME composition without ever emitting its intermediate text. */
    public static final class Composer {
        private String value = "";

        public void set(CharSequence text) {
            value = text == null ? "" : text.toString();
        }

        public String value() {
            return value;
        }

        public void clear() {
            value = "";
        }

        /** Commit the supplied final text and clear the composing region. */
        public byte[] commit(CharSequence text) {
            String committed = text == null ? "" : text.toString();
            clear();
            return utf8(committed);
        }

        /** Delete one composed code point, returning whether anything changed. */
        public boolean deleteLastCodePoint() {
            if (value.isEmpty()) return false;
            value = value.substring(0, value.offsetByCodePoints(value.length(), -1));
            return true;
        }
    }

    /** Encode committed editor text without altering Unicode code points. */
    public static byte[] utf8(CharSequence text) {
        return text == null ? new byte[0] : text.toString().getBytes(StandardCharsets.UTF_8);
    }

    /** Encode a single ASCII Ctrl-letter (Ctrl-A through Ctrl-Z). */
    public static byte[] ctrl(char letter) {
        char upper = Character.toUpperCase(letter);
        if (upper < 'A' || upper > 'Z') return new byte[0];
        return new byte[]{(byte) (upper - 'A' + 1)};
    }

    /** Encode a terminal key using the sequences expected by the bridge PTY. */
    public static byte[] key(Key key) {
        if (key == null) return new byte[0];
        switch (key) {
            case ESCAPE: return ascii("\u001b");
            case TAB: return ascii("\t");
            case ENTER: return ascii("\r");
            case BACKSPACE: return ascii("\u007f");
            case UP: return ascii("\u001b[A");
            case DOWN: return ascii("\u001b[B");
            case RIGHT: return ascii("\u001b[C");
            case LEFT: return ascii("\u001b[D");
            case HOME: return ascii("\u001b[H");
            case END: return ascii("\u001b[F");
            case PAGE_UP: return ascii("\u001b[5~");
            case PAGE_DOWN: return ascii("\u001b[6~");
            default: return new byte[0];
        }
    }

    private static byte[] ascii(String value) {
        return value.getBytes(StandardCharsets.US_ASCII);
    }
}
