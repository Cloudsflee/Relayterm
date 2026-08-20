package com.relayterm.app;

import android.content.Context;
import android.content.SharedPreferences;
import android.os.Handler;
import android.os.Looper;

import org.json.JSONObject;

import java.nio.charset.StandardCharsets;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicLong;
import java.util.UUID;

import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.Response;
import okhttp3.WebSocket;
import okhttp3.WebSocketListener;
import okio.ByteString;

/** OkHttp WebSocket state machine for one currently visible PTY profile. */
public final class PtyClient {
    public interface Listener {
        void onConnecting(String profileId, int attempt);
        void onReady(String profileId, int pid, boolean resumed, String role);
        void onOutput(String profileId, byte[] bytes);
        void onEvent(String profileId, JSONObject event);
        void onExit(String profileId, int code, String cwd);
        void onDisconnected(String profileId, boolean reconnecting);
        void onError(String profileId, String message);
    }

    private static final long[] RECONNECT_DELAYS = {500, 1000, 2000, 4000, 8000, 15000};
    private static final int MAX_FRAME = 64 * 1024;

    private final Handler main = new Handler(Looper.getMainLooper());
    private final AtomicLong generation = new AtomicLong();
    private final String clientId;
    private final OkHttpClient http = new OkHttpClient.Builder()
            .connectTimeout(8, TimeUnit.SECONDS)
            .readTimeout(0, TimeUnit.MILLISECONDS)
            .pingInterval(25, TimeUnit.SECONDS)
            .retryOnConnectionFailure(true)
            .build();
    private volatile WebSocket socket;
    private volatile boolean connected;
    private volatile boolean manualClose = true;
    private volatile boolean sessionEnded;
    private volatile int columns = 100;
    private volatile int rows = 32;
    private TerminalProfile profile;
    private Listener listener;
    private boolean resume = true;
    private int retry;
    private long reconnectScheduledFor = -1L;

    public PtyClient(Context context) {
        SharedPreferences preferences = context.getApplicationContext()
                .getSharedPreferences("relayterm_client", Context.MODE_PRIVATE);
        String stored = preferences.getString("clientId", "");
        if (stored == null || stored.isEmpty()) {
            stored = "android-" + UUID.randomUUID().toString().replace("-", "");
            preferences.edit().putString("clientId", stored).apply();
        }
        clientId = stored;
    }

    public synchronized void connect(
            TerminalProfile profile,
            int columns,
            int rows,
            boolean resume,
            Listener listener) {
        manualClose = true;
        generation.incrementAndGet();
        disconnectInternal();
        this.profile = profile;
        this.listener = listener;
        this.columns = clampColumns(columns);
        this.rows = clampRows(rows);
        this.resume = resume;
        this.retry = 0;
        this.reconnectScheduledFor = -1L;
        this.manualClose = false;
        this.sessionEnded = false;
        long operation = generation.incrementAndGet();
        connectAttempt(operation, profile, listener);
    }

    private void connectAttempt(long operation, TerminalProfile selected, Listener callback) {
        if (!isCurrent(operation) || manualClose || sessionEnded) return;
        int attempt = retry + 1;
        post(operation, () -> callback.onConnecting(selected.id, attempt));
        final Request request;
        try {
            Request.Builder builder = new Request.Builder()
                    .url(CommandPolicy.websocketEndpoint(selected.endpoint,
                            selected.managed ? selected.remoteProfileId : selected.id));
            if (!selected.token.isEmpty()) builder.header("Authorization", "Bearer " + selected.token);
            request = builder.build();
        } catch (Exception error) {
            handleFailure(operation, selected, callback, message(error));
            return;
        }
        socket = http.newWebSocket(request, new WebSocketListener() {
            @Override
            public void onOpen(WebSocket webSocket, Response response) {
                if (!isActive(operation, webSocket) || manualClose) {
                    webSocket.cancel();
                    return;
                }
                JSONObject open = new JSONObject();
                try {
                    open.put("type", "open");
                    open.put("sessionId", selected.id);
                    open.put("clientId", clientId);
                    open.put("clientType", "android");
                    if (selected.managed) {
                        open.put("profileId", selected.remoteProfileId);
                    } else {
                        open.put("startupCommand", selected.startupCommand);
                        open.put("cwd", selected.workingDirectory);
                    }
                    open.put("cols", columns);
                    open.put("rows", rows);
                    open.put("resume", PtyClient.this.resume);
                    if (!webSocket.send(open.toString())) throw new Exception("open_send_failed");
                } catch (Exception error) {
                    webSocket.cancel();
                    handleFailure(operation, selected, callback, message(error));
                }
            }

            @Override
            public void onMessage(WebSocket webSocket, ByteString bytes) {
                if (!isActive(operation, webSocket)) return;
                byte[] copy = bytes.toByteArray();
                post(operation, () -> callback.onOutput(selected.id, copy));
            }

            @Override
            public void onMessage(WebSocket webSocket, String text) {
                if (!isActive(operation, webSocket)) return;
                try {
                    JSONObject event = new JSONObject(text);
                    String type = event.optString("type", "");
                    if ("ready".equals(type)) {
                        connected = true;
                        sessionEnded = false;
                        retry = 0;
                        PtyClient.this.resume = true;
                        int pid = event.optInt("pid", 0);
                        boolean resumedEvent = event.optBoolean("resumed", false);
                        String role = event.optString("role", "observer");
                        post(operation, () -> callback.onReady(selected.id, pid, resumedEvent, role));
                    } else if ("exit".equals(type)) {
                        connected = false;
                        sessionEnded = true;
                        int code = event.optInt("code", 0);
                        String cwd = event.optString("cwd", "");
                        post(operation, () -> callback.onExit(selected.id, code, cwd));
                    } else if ("error".equals(type)) {
                        String value = event.optString("message", event.optString("code", "PTY 错误"));
                        post(operation, () -> callback.onError(selected.id, value));
                    } else if ("resync_required".equals(type)) {
                        post(operation, () -> callback.onEvent(selected.id, event));
                        connected = false;
                        scheduleReconnect(operation, selected, callback);
                        webSocket.cancel();
                    } else {
                        if ("control_changed".equals(type)) {
                            event.put("role", clientId.equals(event.optString("controllerClientId", ""))
                                    ? "controller" : "observer");
                        }
                        post(operation, () -> callback.onEvent(selected.id, event));
                    }
                } catch (Exception error) {
                    post(operation, () -> callback.onError(selected.id, "无效 PTY 事件"));
                }
            }

            @Override
            public void onClosing(WebSocket webSocket, int code, String reason) {
                webSocket.close(code, reason);
            }

            @Override
            public void onClosed(WebSocket webSocket, int code, String reason) {
                if (!isActive(operation, webSocket)) return;
                connected = false;
                if (!manualClose && !sessionEnded) {
                    scheduleReconnect(operation, selected, callback);
                }
            }

            @Override
            public void onFailure(WebSocket webSocket, Throwable error, Response response) {
                if (!isActive(operation, webSocket)) return;
                connected = false;
                if (!manualClose && !sessionEnded) {
                    handleFailure(operation, selected, callback, message(error));
                }
            }
        });
    }

    private synchronized void handleFailure(
            long operation, TerminalProfile selected, Listener callback, String detail) {
        if (!isCurrent(operation) || manualClose) return;
        if (reconnectScheduledFor == operation) return;
        post(operation, () -> callback.onError(selected.id, detail));
        scheduleReconnect(operation, selected, callback);
    }

    private synchronized void scheduleReconnect(
            long operation, TerminalProfile selected, Listener callback) {
        if (!isCurrent(operation) || manualClose || sessionEnded) return;
        if (reconnectScheduledFor == operation) return;
        reconnectScheduledFor = operation;
        long delay = RECONNECT_DELAYS[Math.min(retry, RECONNECT_DELAYS.length - 1)];
        retry++;
        post(operation, () -> callback.onDisconnected(selected.id, true));
        main.postDelayed(() -> {
            boolean retryNow = false;
            synchronized (PtyClient.this) {
                if (reconnectScheduledFor == operation) {
                    reconnectScheduledFor = -1L;
                    socket = null;
                    retryNow = true;
                }
            }
            if (retryNow && isCurrent(operation) && !manualClose && !sessionEnded) {
                connectAttempt(operation, selected, callback);
            }
        }, delay);
    }

    public boolean isConnected() {
        return connected;
    }

    public synchronized String activeProfileId() {
        return profile == null ? "" : profile.id;
    }

    public String clientId() {
        return clientId;
    }

    public void sendInput(byte[] bytes) {
        if (bytes == null || bytes.length == 0) return;
        if (bytes.length > MAX_FRAME) throw new IllegalArgumentException("输入帧超过 64 KiB");
        WebSocket active = socket;
        if (!connected || active == null || !active.send(ByteString.of(bytes))) notifyWriteError("发送失败");
    }

    public void sendText(String text) {
        if (text != null && !text.isEmpty()) sendInput(text.getBytes(StandardCharsets.UTF_8));
    }

    public void resize(int columns, int rows) {
        this.columns = clampColumns(columns);
        this.rows = clampRows(rows);
        JSONObject event = new JSONObject();
        try {
            event.put("type", "resize");
            event.put("cols", this.columns);
            event.put("rows", this.rows);
            sendControl(event);
        } catch (Exception error) {
            notifyWriteError(message(error));
        }
    }

    public void signal(String name) {
        JSONObject event = new JSONObject();
        try {
            event.put("type", "signal");
            event.put("name", name);
            sendControl(event);
        } catch (Exception error) {
            notifyWriteError(message(error));
        }
    }

    public void ping() {
        JSONObject event = new JSONObject();
        try {
            event.put("type", "ping");
            sendControl(event);
        } catch (Exception error) {
            notifyWriteError(message(error));
        }
    }

    public synchronized void closeSession(boolean terminate) {
        WebSocket active = socket;
        manualClose = true;
        generation.incrementAndGet();
        connected = false;
        socket = null;
        if (active != null) {
            JSONObject event = new JSONObject();
            try {
                event.put("type", "close");
                event.put("terminate", terminate);
                active.send(event.toString());
                active.close(1000, "profile_close");
            } catch (Exception ignored) {
                active.cancel();
            }
        }
    }

    /** Close only the phone socket; the bridge-owned PTY remains alive. */
    public synchronized void disconnect() {
        manualClose = true;
        generation.incrementAndGet();
        disconnectInternal();
    }

    public synchronized void shutdown() {
        disconnect();
        http.dispatcher().executorService().shutdown();
        http.connectionPool().evictAll();
    }

    private void disconnectInternal() {
        connected = false;
        WebSocket active = socket;
        socket = null;
        if (active != null) active.cancel();
    }

    private void sendControl(JSONObject event) {
        WebSocket active = socket;
        if (active != null && !active.send(event.toString())) notifyWriteError("发送失败");
    }

    private void notifyWriteError(String detail) {
        TerminalProfile selected = profile;
        Listener callback = listener;
        long operation = generation.get();
        if (selected != null && callback != null) {
            post(operation, () -> callback.onError(selected.id, detail));
        }
    }

    private boolean isCurrent(long operation) {
        return generation.get() == operation;
    }

    private boolean isActive(long operation, WebSocket candidate) {
        return isCurrent(operation) && socket == candidate;
    }

    private void post(long operation, Runnable runnable) {
        main.post(() -> { if (isCurrent(operation)) runnable.run(); });
    }

    private static int clampColumns(int value) { return Math.max(2, Math.min(value, 400)); }
    private static int clampRows(int value) { return Math.max(2, Math.min(value, 200)); }

    private static String message(Throwable error) {
        String value = error == null ? "" : error.getMessage();
        return value == null || value.trim().isEmpty()
                ? (error == null ? "连接失败" : error.getClass().getSimpleName()) : value;
    }
}
