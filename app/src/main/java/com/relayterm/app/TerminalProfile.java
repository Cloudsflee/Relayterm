package com.relayterm.app;

import org.json.JSONArray;
import org.json.JSONObject;

import java.time.Instant;
import java.time.LocalDateTime;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.time.format.DateTimeParseException;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Comparator;
import java.util.List;
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
    public final boolean pinned;
    public final boolean enabled;
    public final String launchMode;
    public final List<String> codexArgs;
    public final boolean managed;
    public final String connectionId;
    /** ID used by the bridge catalog; manual profiles retain legacy session IDs. */
    public final String remoteProfileId;
    /** Shared bridge activity timestamp, or an empty string when never opened. */
    public final String lastOpenedAt;

    public TerminalProfile(String id, String name, String endpoint, String token) {
        this(id, name, endpoint, token, "codex", "");
    }

    public TerminalProfile(
            String id, String name, String endpoint, String token,
            String startupCommand, String workingDirectory) {
        this(id, name, endpoint, token, startupCommand, workingDirectory,
                "", false, true, false, "", "", "");
    }

    public TerminalProfile(
            String id, String name, String endpoint, String token,
            String startupCommand, String workingDirectory, String lastOpenedAt) {
        this(id, name, endpoint, token, startupCommand, workingDirectory,
                "", false, true, false, "", "", lastOpenedAt);
    }

    public TerminalProfile(
            String id, String name, String endpoint, String token,
            String startupCommand, String workingDirectory, String shell, boolean pinned,
            boolean enabled, boolean managed, String connectionId, String remoteProfileId,
            String lastOpenedAt) {
        this(id, name, endpoint, token, startupCommand, workingDirectory, shell, pinned,
                enabled, managed, connectionId, remoteProfileId, lastOpenedAt,
                "command", Collections.emptyList());
    }

    public TerminalProfile(
            String id, String name, String endpoint, String token,
            String startupCommand, String workingDirectory, String shell, boolean pinned,
            boolean enabled, boolean managed, String connectionId, String remoteProfileId,
            String lastOpenedAt, String launchMode, List<String> codexArgs) {
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
        this.pinned = pinned;
        this.enabled = enabled;
        String mode = launchMode == null ? "command" : launchMode.trim().toLowerCase(Locale.US);
        this.launchMode = "codex".equals(mode) ? "codex" : "command";
        List<String> args = new ArrayList<>();
        if (codexArgs != null) {
            for (String arg : codexArgs) {
                if (arg != null && !arg.trim().isEmpty()) args.add(arg.trim());
            }
        }
        this.codexArgs = Collections.unmodifiableList(args);
        this.connectionId = connectionId == null ? "" : connectionId.trim();
        this.remoteProfileId = remoteProfileId == null ? "" : remoteProfileId.trim();
        String activity = lastOpenedAt == null ? "" : lastOpenedAt.trim();
        this.lastOpenedAt = "null".equalsIgnoreCase(activity) ? "" : activity;
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
                remote.optString("shell", "pwsh"), remote.optBoolean("pinned", false),
                remote.optBoolean("enabled", true), true, connectionId, remoteId,
                remote.optString("lastOpenedAt", ""), remote.optString("launchMode", "command"),
                jsonStrings(remote.optJSONArray("codexArgs")));
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

    public boolean usesCodexSessions() {
        return managed && "codex".equals(launchMode);
    }

    public TerminalProfile withLastOpenedAt(String value) {
        return new TerminalProfile(id, name, endpoint, token, startupCommand, workingDirectory,
                shell, pinned, enabled, managed, connectionId, remoteProfileId, value,
                launchMode, codexArgs);
    }

    public static Instant parseLastOpenedAt(String value) {
        if (value == null || value.trim().isEmpty() || "-".equals(value.trim())) return null;
        String text = value.trim();
        if (text.endsWith("z")) text = text.substring(0, text.length() - 1) + "Z";
        try {
            return Instant.parse(text);
        } catch (DateTimeParseException ignored) {
            try {
                return OffsetDateTime.parse(text).toInstant();
            } catch (DateTimeParseException ignoredAgain) {
                try {
                    return LocalDateTime.parse(text).toInstant(ZoneOffset.UTC);
                } catch (DateTimeParseException ignoredNaive) {
                    return null;
                }
            }
        }
    }

    public static Comparator<TerminalProfile> recentComparator() {
        return (left, right) -> {
            if (left == right) return 0;
            if (left == null) return 1;
            if (right == null) return -1;
            if (left.pinned != right.pinned) return left.pinned ? -1 : 1;
            if (left.pinned) return 0;
            Instant a = parseLastOpenedAt(left.lastOpenedAt);
            Instant b = parseLastOpenedAt(right.lastOpenedAt);
            if (a != null && b == null) return -1;
            if (a == null && b != null) return 1;
            if (a != null) {
                int byTime = b.compareTo(a);
                if (byTime != 0) return byTime;
            }
            return 0;
        };
    }

    public static List<TerminalProfile> sortByRecent(List<TerminalProfile> input) {
        List<TerminalProfile> result = input == null ? new ArrayList<>() : new ArrayList<>(input);
        result.sort(recentComparator());
        return result;
    }

    /** Pure status vocabulary used by the Android switcher and JVM tests. */
    public static String displaySessionStatus(JSONObject session, boolean currentConnection, String role) {
        return displaySessionStatus(
                session != null && session.optBoolean("running", false),
                session == null ? "" : session.optString("state", ""),
                session == null ? "" : session.optString("desktopState", ""),
                currentConnection,
                role);
    }

    public static String displaySessionStatus(
            boolean running, String state, String desktopState,
            boolean currentConnection, String role) {
        if ("exited".equalsIgnoreCase(state)) return "已退出";
        if (currentConnection && running) {
            return "controller".equals(role) ? "控制端 · 运行中" : "观察端 · 运行中";
        }
        if (running && "closed".equalsIgnoreCase(desktopState)) {
            return "终端已关闭 · 会话可恢复";
        }
        if (running) return "运行中 · 未连接";
        return "未连接";
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
        object.put("pinned", pinned);
        object.put("enabled", enabled);
        object.put("launchMode", launchMode);
        object.put("codexArgs", new JSONArray(codexArgs));
        object.put("managed", managed);
        object.put("connectionId", connectionId);
        object.put("remoteProfileId", remoteProfileId);
        object.put("lastOpenedAt", lastOpenedAt);
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
                object.optBoolean("pinned", false), object.optBoolean("enabled", true), managed,
                object.optString("connectionId", ""), object.optString("remoteProfileId", ""),
                object.optString("lastOpenedAt", ""), object.optString("launchMode", "command"),
                jsonStrings(object.optJSONArray("codexArgs")));
    }

    private static List<String> jsonStrings(JSONArray values) {
        List<String> result = new ArrayList<>();
        if (values == null) return result;
        for (int i = 0; i < values.length(); i++) {
            String value = values.optString(i, "").trim();
            if (!value.isEmpty()) result.add(value);
        }
        return result;
    }
}
