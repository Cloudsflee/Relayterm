package com.relayterm.app;

import org.json.JSONObject;

import java.util.Locale;
import java.util.UUID;

/** Persisted manual connection or read-only PC-managed project. */
public final class TerminalProfile {
    public final String id;
    public final String name;
    public final String endpoint;
    public final String token;
    public final String startupCommand;
    public final String workingDirectory;
    public final String shell;
    public final int order;
    public final boolean enabled;
    public final boolean managed;
    public final String connectionId;
    /** ID used by the bridge catalog; manual profiles retain legacy session IDs. */
    public final String remoteProfileId;

    public TerminalProfile(String id, String name, String endpoint, String token) {
        this(id, name, endpoint, token, "codex", "");
    }

    public TerminalProfile(
            String id, String name, String endpoint, String token,
            String startupCommand, String workingDirectory) {
        this(id, name, endpoint, token, startupCommand, workingDirectory,
                "", 0, true, false, "", "");
    }

    private TerminalProfile(
            String id, String name, String endpoint, String token,
            String startupCommand, String workingDirectory, String shell, int order,
            boolean enabled, boolean managed, String connectionId, String remoteProfileId) {
        this.id = id == null || id.trim().isEmpty() ? UUID.randomUUID().toString() : id.trim();
        this.name = name == null || name.trim().isEmpty() ? "未命名终端" : name.trim();
        this.endpoint = endpoint == null ? "" : endpoint.trim();
        this.token = token == null ? "" : token;
        this.managed = managed;
        this.startupCommand = managed
                ? (startupCommand == null ? "" : startupCommand.trim())
                : (startupCommand == null || startupCommand.trim().isEmpty() ? "codex" : startupCommand.trim());
        this.workingDirectory = workingDirectory == null ? "" : workingDirectory.trim();
        this.shell = shell == null ? "" : shell.trim().toLowerCase(Locale.US);
        this.order = order;
        this.enabled = enabled;
        this.connectionId = connectionId == null ? "" : connectionId.trim();
        this.remoteProfileId = remoteProfileId == null ? "" : remoteProfileId.trim();
    }

    public static TerminalProfile managed(
            String connectionId, String endpoint, String token, JSONObject remote) {
        String remoteId = remote.optString("id", "");
        String localId = "pc:" + connectionId + ":" + remoteId;
        return new TerminalProfile(
                localId,
                remote.optString("name", remoteId), endpoint, token,
                remote.optString("startupCommand", ""),
                remote.optString("workingDirectory", ""),
                remote.optString("shell", "pwsh"), remote.optInt("order", 0),
                remote.optBoolean("enabled", true), true, connectionId, remoteId);
    }

    public boolean isLocal() {
        String value = endpoint.toLowerCase(Locale.US);
        return value.startsWith("demo://") || value.startsWith("local://");
    }

    public boolean isManaged() {
        return managed;
    }

    public String bridgeProfileId() {
        return managed ? remoteProfileId : "";
    }

    public JSONObject toJson(SecretStore secrets) throws Exception {
        JSONObject object = new JSONObject();
        object.put("id", id);
        object.put("name", name);
        object.put("endpoint", endpoint);
        object.put("token", secrets.encrypt(token));
        object.put("startupCommand", startupCommand);
        object.put("workingDirectory", workingDirectory);
        object.put("shell", shell);
        object.put("order", order);
        object.put("enabled", enabled);
        object.put("managed", managed);
        object.put("connectionId", connectionId);
        object.put("remoteProfileId", remoteProfileId);
        return object;
    }

    public static TerminalProfile fromJson(JSONObject object, SecretStore secrets) throws Exception {
        String token = secrets.decrypt(object.optString("token", ""));
        boolean managed = object.optBoolean("managed", false);
        return new TerminalProfile(
                object.optString("id", ""),
                object.optString("name", "未命名终端"),
                object.optString("endpoint", ""), token,
                object.optString("startupCommand", managed ? "" : "codex"),
                object.optString("workingDirectory", ""),
                object.optString("shell", managed ? "pwsh" : ""),
                object.optInt("order", 0), object.optBoolean("enabled", true), managed,
                object.optString("connectionId", ""), object.optString("remoteProfileId", ""));
    }
}
