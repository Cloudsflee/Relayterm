package com.relayterm.app;

/** Endpoint and Keystore-backed credential used to synchronize one PC catalog. */
public final class PcConnection {
    public final String id;
    public final String name;
    public final String endpoint;
    public final String token;

    public PcConnection(String id, String name, String endpoint, String token) {
        this.id = id;
        this.name = name;
        this.endpoint = endpoint;
        this.token = token;
    }

    public static PcConnection fromManualProfile(TerminalProfile profile) {
        if (profile.isManaged()) throw new IllegalArgumentException("managed_profile_is_not_connection");
        return new PcConnection(profile.id, profile.name, profile.endpoint, profile.token);
    }
}
