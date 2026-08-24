package com.relayterm.app;

import org.junit.Test;

import java.nio.charset.StandardCharsets;
import static org.junit.Assert.assertArrayEquals;
import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;

public final class TerminalInputCodecTest {
    @Test
    public void encodesChineseAndEmojiAsUtf8() {
        assertArrayEquals("中文🙂".getBytes(StandardCharsets.UTF_8),
                TerminalInputCodec.utf8("中文🙂"));
    }

    @Test
    public void composingTextIsRepresentedOnlyByCommittedPayload() {
        TerminalInputCodec.Composer composer = new TerminalInputCodec.Composer();
        composer.set("ni");
        assertEquals("ni", composer.value());
        assertArrayEquals("你".getBytes(StandardCharsets.UTF_8), composer.commit("你"));
        assertTrue(composer.value().isEmpty());
    }

    @Test
    public void encodesEditingAndControlKeys() {
        assertArrayEquals(new byte[]{0x7f},
                TerminalInputCodec.key(TerminalInputCodec.Key.BACKSPACE));
        assertArrayEquals(new byte[]{'\r'},
                TerminalInputCodec.key(TerminalInputCodec.Key.ENTER));
        assertArrayEquals(new byte[]{3}, TerminalInputCodec.ctrl('c'));
        assertArrayEquals(new byte[]{0x1b, '[', 'A'},
                TerminalInputCodec.key(TerminalInputCodec.Key.UP));
    }
}
