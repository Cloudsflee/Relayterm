package com.relayterm.app;

import org.junit.Test;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertThrows;

public final class PairingLinkTest {
    private static final String CHALLENGE = "AbcdefghijklmnopQRSTUV";

    @Test
    public void parsesHttpsPairingPageQrPayload() {
        PairingLink result = PairingLink.parse(
                "https://relay.example:8443/pair/" + CHALLENGE);

        assertEquals("https://relay.example:8443", result.endpoint);
        assertEquals(CHALLENGE, result.challenge);
    }

    @Test
    public void parsesExistingRelaytermDeepLink() {
        PairingLink result = PairingLink.parse(
                "relayterm://pair?endpoint=https%3A%2F%2Frelay.example&challenge=" + CHALLENGE);

        assertEquals("https://relay.example", result.endpoint);
        assertEquals(CHALLENGE, result.challenge);
    }

    @Test
    public void acceptsLocalDebugPairingPage() {
        PairingLink result = PairingLink.parse(
                "http://10.0.2.2:18765/pair/" + CHALLENGE);

        assertEquals("http://10.0.2.2:18765", result.endpoint);
    }

    @Test
    public void rejectsMissingPairingValues() {
        assertThrows(IllegalArgumentException.class,
                () -> PairingLink.parse("relayterm://pair?endpoint=https%3A%2F%2Frelay.example"));
        assertThrows(IllegalArgumentException.class,
                () -> PairingLink.parse("relayterm://pair?challenge=" + CHALLENGE));
    }

    @Test
    public void rejectsUnsupportedSchemeAndHost() {
        assertThrows(IllegalArgumentException.class,
                () -> PairingLink.parse("ftp://relay.example/pair/" + CHALLENGE));
        assertThrows(IllegalArgumentException.class,
                () -> PairingLink.parse("https:///pair/" + CHALLENGE));
        assertThrows(IllegalArgumentException.class,
                () -> PairingLink.parse("http://relay.example/pair/" + CHALLENGE));
    }

    @Test
    public void rejectsWrongPathOrChallenge() {
        assertThrows(IllegalArgumentException.class,
                () -> PairingLink.parse("https://relay.example/not-pair/" + CHALLENGE));
        assertThrows(IllegalArgumentException.class,
                () -> PairingLink.parse("https://relay.example/pair/short"));
        assertThrows(IllegalArgumentException.class,
                () -> PairingLink.parse("relayterm://pair/extra?endpoint=https%3A%2F%2Frelay.example"
                        + "&challenge=" + CHALLENGE));
    }
}
