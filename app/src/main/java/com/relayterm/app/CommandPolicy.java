package com.relayterm.app;

import java.util.Locale;
import java.net.URI;
import java.net.URLEncoder;

/** Small input policy that catches accidental empty/destructive UI submits. */
public final class CommandPolicy {
    private CommandPolicy() { }

    public static String validate(String command) {
        if (command == null || command.trim().isEmpty()) return "请输入命令";
        if (command.length() > 8192) return "命令长度超过 8192 个字符";
        return "";
    }

    public static boolean isLocalEndpoint(String endpoint) {
        if (endpoint == null) return false;
        String value = endpoint.toLowerCase(Locale.US);
        return value.startsWith("demo://") || value.startsWith("local://");
    }

    public static String validateEndpoint(String endpoint) {
        if (endpoint == null || endpoint.trim().isEmpty()) return "请输入桥接地址";
        String value = endpoint.trim();
        if (isLocalEndpoint(value)) return "";
        try {
            URI uri = new URI(value);
            String scheme = uri.getScheme() == null ? "" : uri.getScheme().toLowerCase(Locale.US);
            String host = uri.getHost() == null ? "" : uri.getHost().toLowerCase(Locale.US);
            if (uri.getUserInfo() != null || uri.getFragment() != null) {
                return "地址格式不受支持";
            }
            if ("https".equals(scheme) && !host.isEmpty()) return "";
            if ("http".equals(scheme)
                    && ("localhost".equals(host) || "127.0.0.1".equals(host) || "10.0.2.2".equals(host))) {
                return "";
            }
        } catch (Exception ignored) {
            // Fall through to the concise validation message below.
        }
        return "远端地址必须使用 HTTPS（本机调试可用 localhost/127.0.0.1/10.0.2.2）";
    }

    /** Convert a profile's HTTP endpoint into the PTY WebSocket URL. */
    public static String websocketEndpoint(String endpoint, String sessionId) {
        String value = endpoint == null ? "" : endpoint.trim();
        String error = validateEndpoint(value);
        if (!error.isEmpty()) throw new IllegalArgumentException(error);
        if (isLocalEndpoint(value)) throw new IllegalArgumentException("本机演示不提供远程 PTY");
        String base = value.endsWith("/") ? value.substring(0, value.length() - 1) : value;
        if (base.regionMatches(true, 0, "https://", 0, 8)) {
            base = "wss://" + base.substring(8);
        } else if (base.regionMatches(true, 0, "http://", 0, 7)) {
            base = "ws://" + base.substring(7);
        }
        String id;
        try {
            id = URLEncoder.encode(sessionId == null ? "default" : sessionId, "UTF-8");
        } catch (Exception ignored) {
            id = "default";
        }
        return base + "/v1/pty?sessionId=" + id;
    }

    public static String toWebSocketEndpoint(String endpoint, String sessionId) {
        return websocketEndpoint(endpoint, sessionId);
    }
}
