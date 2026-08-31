package com.relayterm.app;

import android.os.Handler;
import android.os.Looper;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.IOException;
import java.net.URLEncoder;
import java.time.Instant;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicLong;

import okhttp3.MediaType;
import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.RequestBody;
import okhttp3.Response;

/** Synchronizes server-managed projects and live session state without changing PC data. */
public final class CatalogClient {
    public interface Callback {
        void onCatalog(PcConnection connection, List<TerminalProfile> profiles,
                       Map<String, JSONObject> sessions);
        void onError(PcConnection connection, String message);
    }

    public interface PairingCallback {
        void onPaired(PairingResult result);
        void onError(String message);
    }

    public interface CodexCallback {
        void onResult(TerminalProfile profile, JSONObject value);
        void onError(TerminalProfile profile, String message);
    }

    public static final class PairingResult {
        public final String endpoint;
        public final String token;
        public final JSONArray profiles;
        public final String revision;

        public PairingResult(String endpoint, String token, JSONArray profiles) {
            this(endpoint, token, profiles, "");
        }

        public PairingResult(String endpoint, String token, JSONArray profiles, String revision) {
            this.endpoint = endpoint;
            this.token = token;
            this.profiles = profiles;
            this.revision = revision == null ? "" : revision;
        }
    }

    private static final MediaType JSON = MediaType.get("application/json; charset=utf-8");
    private final Handler main = new Handler(Looper.getMainLooper());
    private final ExecutorService executor = Executors.newSingleThreadExecutor();
    private final AtomicLong generation = new AtomicLong();
    private final OkHttpClient http = new OkHttpClient.Builder()
            .connectTimeout(8, TimeUnit.SECONDS)
            .readTimeout(15, TimeUnit.SECONDS)
            .build();

    public void sync(PcConnection connection, Callback callback) {
        long operation = generation.get();
        executor.execute(() -> {
            try {
                JSONObject catalog = get(connection.endpoint, connection.token, "/v1/profiles");
                JSONObject status = get(connection.endpoint, connection.token, "/v1/sessions");
                JSONArray items = catalog.optJSONArray("profiles");
                List<TerminalProfile> profiles = new ArrayList<>();
                if (items != null) {
                    for (int i = 0; i < items.length(); i++) {
                        JSONObject item = items.optJSONObject(i);
                        if (item != null && item.optBoolean("enabled", true)) {
                            profiles.add(TerminalProfile.managed(
                                    connection.id, connection.endpoint, connection.token, item));
                        }
                    }
                }
                Map<String, JSONObject> sessions = new HashMap<>();
                JSONArray running = status.optJSONArray("sessions");
                if (running != null) {
                    for (int i = 0; i < running.length(); i++) {
                        JSONObject session = running.optJSONObject(i);
                        if (session != null) sessions.put(session.optString("profileId", ""), session);
                    }
                }
                // The session endpoint is authoritative during the short
                // window between a ready event and the next catalog refresh.
                for (int i = 0; i < profiles.size(); i++) {
                    TerminalProfile profile = profiles.get(i);
                    JSONObject session = sessions.get(profile.remoteProfileId);
                    String timestamp = session == null ? "" : session.optString("lastOpenedAt", "");
                    Instant incoming = TerminalProfile.parseLastOpenedAt(timestamp);
                    Instant current = TerminalProfile.parseLastOpenedAt(profile.lastOpenedAt);
                    if (incoming != null && (current == null || incoming.isAfter(current))) {
                        profiles.set(i, profile.withLastOpenedAt(timestamp));
                    }
                }
                post(operation, () -> callback.onCatalog(connection, profiles, sessions));
            } catch (Exception error) {
                post(operation, () -> callback.onError(connection, message(error)));
            }
        });
    }

    public void exchange(String endpoint, String challenge, PairingCallback callback) {
        long operation = generation.get();
        executor.execute(() -> {
            try {
                String error = CommandPolicy.validateEndpoint(endpoint);
                if (!error.isEmpty()) throw new IOException(error);
                JSONObject requestJson = new JSONObject();
                requestJson.put("challenge", challenge);
                Request request = new Request.Builder()
                        .url(base(endpoint) + "/v1/pairing/exchange")
                        .post(RequestBody.create(requestJson.toString(), JSON))
                        .header("Accept", "application/json")
                        .build();
                try (Response response = http.newCall(request).execute()) {
                    String body = response.body() == null ? "" : response.body().string();
                    if (response.code() == 410) {
                        throw new IOException("配对码已过期或已使用，请在电脑端刷新配对二维码");
                    }
                    if (!response.isSuccessful()) throw new IOException("配对失败 HTTP " + response.code());
                    JSONObject value = new JSONObject(body);
                    PairingResult result = new PairingResult(
                            value.optString("endpoint", endpoint), value.optString("token", ""),
                            value.optJSONArray("profiles") == null ? new JSONArray() : value.optJSONArray("profiles"),
                            value.optString("revision", ""));
                    if (result.token.isEmpty()) throw new IOException("配对响应缺少 Token");
                    post(operation, () -> callback.onPaired(result));
                }
            } catch (Exception error) {
                post(operation, () -> callback.onError(message(error)));
            }
        });
    }

    public void codexSessions(TerminalProfile profile, CodexCallback callback) {
        codexRequest(profile, "GET", "/v1/profiles/" + segment(profile.remoteProfileId)
                + "/codex-sessions", null, callback);
    }

    public void setCodexBinding(
            TerminalProfile profile, String mode, String threadId, CodexCallback callback) {
        JSONObject body = new JSONObject();
        try {
            body.put("mode", mode);
            if (threadId != null && !threadId.isEmpty()) body.put("threadId", threadId);
        } catch (Exception ignored) { }
        codexRequest(profile, "PUT", "/v1/profiles/" + segment(profile.remoteProfileId)
                + "/codex-binding", body, callback);
    }

    public void createCodexSession(TerminalProfile profile, CodexCallback callback) {
        JSONObject body = new JSONObject();
        try { body.put("lock", true); } catch (Exception ignored) { }
        codexRequest(profile, "POST", "/v1/profiles/" + segment(profile.remoteProfileId)
                + "/codex-sessions", body, callback);
    }

    public void terminateForCodexSwitch(TerminalProfile profile, CodexCallback callback) {
        JSONObject body = new JSONObject();
        try {
            body.put("force", true);
            body.put("remove", true);
        } catch (Exception ignored) { }
        codexRequest(profile, "POST", "/v1/sessions/" + segment(profile.remoteProfileId)
                + "/terminate", body, callback);
    }

    private void codexRequest(
            TerminalProfile profile, String method, String path, JSONObject body,
            CodexCallback callback) {
        long operation = generation.get();
        executor.execute(() -> {
            try {
                RequestBody requestBody = body == null ? null : RequestBody.create(body.toString(), JSON);
                Request.Builder builder = new Request.Builder()
                        .url(base(profile.endpoint) + path)
                        .header("Accept", "application/json");
                if (!profile.token.isEmpty()) builder.header("Authorization", "Bearer " + profile.token);
                if ("GET".equals(method)) builder.get();
                else if ("PUT".equals(method)) builder.put(requestBody);
                else builder.post(requestBody);
                try (Response response = http.newCall(builder.build()).execute()) {
                    String raw = response.body() == null ? "" : response.body().string();
                    JSONObject value = raw.isEmpty() ? new JSONObject() : new JSONObject(raw);
                    if (!response.isSuccessful()) {
                        throw new IOException(value.optString("message",
                                value.optString("error", "HTTP " + response.code())));
                    }
                    post(operation, () -> callback.onResult(profile, value));
                }
            } catch (Exception error) {
                post(operation, () -> callback.onError(profile, message(error)));
            }
        });
    }

    private void post(long operation, Runnable callback) {
        main.post(() -> {
            if (generation.get() == operation) callback.run();
        });
    }

    private JSONObject get(String endpoint, String token, String path) throws Exception {
        Request.Builder builder = new Request.Builder().url(base(endpoint) + path).get()
                .header("Accept", "application/json");
        if (!token.isEmpty()) builder.header("Authorization", "Bearer " + token);
        try (Response response = http.newCall(builder.build()).execute()) {
            String body = response.body() == null ? "" : response.body().string();
            if (!response.isSuccessful()) throw new IOException("HTTP " + response.code());
            return new JSONObject(body);
        }
    }

    private static String base(String endpoint) {
        return endpoint.endsWith("/") ? endpoint.substring(0, endpoint.length() - 1) : endpoint;
    }

    private static String segment(String value) {
        try {
            return URLEncoder.encode(value == null ? "" : value, "UTF-8").replace("+", "%20");
        } catch (Exception ignored) {
            return "";
        }
    }

    private static String message(Throwable error) {
        String value = error.getMessage();
        return value == null || value.trim().isEmpty() ? error.getClass().getSimpleName() : value;
    }

    public void shutdown() {
        generation.incrementAndGet();
        executor.shutdownNow();
        http.dispatcher().executorService().shutdown();
        http.connectionPool().evictAll();
    }
}
