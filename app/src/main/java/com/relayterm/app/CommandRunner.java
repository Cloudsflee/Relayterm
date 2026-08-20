package com.relayterm.app;

import android.os.Handler;
import android.os.Looper;

import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.atomic.AtomicLong;
import java.util.concurrent.atomic.AtomicReference;

/** Serializes commands so output from different terminals cannot interleave. */
public final class CommandRunner {
    public interface Callback {
        void onStarted();
        void onFinished(CommandResult result);
        void onError(String message);
    }

    private final ExecutorService executor = Executors.newSingleThreadExecutor();
    private final Handler main = new Handler(Looper.getMainLooper());
    private final BridgeClient bridge = new BridgeClient();
    private final AtomicReference<Process> activeProcess = new AtomicReference<>();
    private final AtomicLong generation = new AtomicLong();
    private Future<?> current;

    public synchronized boolean isRunning() {
        return current != null && !current.isDone();
    }

    public synchronized void run(TerminalProfile profile, String command, Callback callback) {
        cancel();
        long operation = generation.get();
        current = executor.submit(() -> {
            postIfCurrent(operation, callback::onStarted);
            try {
                CommandResult result;
                if (profile.isLocal()) {
                    result = LocalShell.execute(command, activeProcess);
                } else {
                    result = bridge.execute(profile, command, profile.id);
                }
                postIfCurrent(operation, () -> callback.onFinished(result));
            } catch (Exception error) {
                postIfCurrent(operation,
                        () -> callback.onError(error.getMessage() == null ? "执行失败" : error.getMessage()));
            }
        });
    }

    public synchronized void cancel() {
        generation.incrementAndGet();
        Process process = activeProcess.getAndSet(null);
        if (process != null) process.destroy();
        bridge.cancel();
        if (current != null) current.cancel(true);
        current = null;
    }

    public void shutdown() {
        cancel();
        executor.shutdownNow();
    }

    private void postIfCurrent(long operation, Runnable runnable) {
        main.post(() -> {
            if (generation.get() == operation) runnable.run();
        });
    }
}
