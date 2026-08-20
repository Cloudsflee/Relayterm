package com.relayterm.app;

import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.atomic.AtomicReference;

/** Minimal JSON-over-HTTPS client for a user's own relay bridge. */
public final class BridgeClient {
    private final AtomicReference<HttpURLConnection> activeConnection = new AtomicReference<>();

    public CommandResult execute(TerminalProfile profile, String command, String sessionId)
            throws Exception {
        HttpURLConnection connection = open(profile, "/v1/exec", "POST");
        activeConnection.set(connection);
        try {
            JSONObject request = new JSONObject();
            request.put("command", command);
            request.put("sessionId", sessionId == null ? "mobile" : sessionId);
            byte[] body = request.toString().getBytes(StandardCharsets.UTF_8);
            connection.setFixedLengthStreamingMode(body.length);
            try (OutputStream output = connection.getOutputStream()) {
                output.write(body);
            }
            int status = connection.getResponseCode();
            String response = read(status >= 400 ? connection.getErrorStream() : connection.getInputStream());
            if (status < 200 || status >= 300) {
                throw new Exception("桥接服务返回 HTTP " + status + ": " + response);
            }
            return parseResult(response);
        } finally {
            activeConnection.compareAndSet(connection, null);
            connection.disconnect();
        }
    }

    public void health(TerminalProfile profile) throws Exception {
        HttpURLConnection connection = open(profile, "/health", "GET");
        activeConnection.set(connection);
        try {
            int status = connection.getResponseCode();
            String response = read(status >= 400 ? connection.getErrorStream() : connection.getInputStream());
            if (status < 200 || status >= 300) {
                throw new Exception("健康检查失败 HTTP " + status + ": " + response);
            }
        } finally {
            activeConnection.compareAndSet(connection, null);
            connection.disconnect();
        }
    }

    public void cancel() {
        HttpURLConnection connection = activeConnection.getAndSet(null);
        if (connection != null) connection.disconnect();
    }

    private HttpURLConnection open(TerminalProfile profile, String path, String method) throws Exception {
        String endpointError = CommandPolicy.validateEndpoint(profile.endpoint);
        if (!endpointError.isEmpty()) throw new Exception(endpointError);
        String endpoint = profile.endpoint.endsWith("/")
                ? profile.endpoint.substring(0, profile.endpoint.length() - 1)
                : profile.endpoint;
        HttpURLConnection connection = (HttpURLConnection) new URL(endpoint + path).openConnection();
        connection.setRequestMethod(method);
        connection.setConnectTimeout(8000);
        connection.setReadTimeout(30000);
        connection.setUseCaches(false);
        connection.setRequestProperty("Accept", "application/json");
        if (!profile.token.isEmpty()) {
            connection.setRequestProperty("Authorization", "Bearer " + profile.token);
        }
        if ("POST".equals(method)) {
            connection.setDoOutput(true);
            connection.setRequestProperty("Content-Type", "application/json; charset=utf-8");
        }
        return connection;
    }

    private static String read(InputStream stream) throws Exception {
        if (stream == null) return "";
        StringBuilder result = new StringBuilder();
        try (BufferedReader reader = new BufferedReader(
                new InputStreamReader(stream, StandardCharsets.UTF_8))) {
            String line;
            while ((line = reader.readLine()) != null) result.append(line).append('\n');
        }
        return result.toString().trim();
    }

    static CommandResult parseResult(String response) throws Exception {
        JSONObject json = new JSONObject(response);
        return new CommandResult(
                json.optString("stdout", json.optString("output", "")),
                json.optString("stderr", ""),
                json.optInt("exitCode", json.optInt("code", 0)));
    }
}
