package com.relayterm.app;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;

/** Executes only on the Android app's own sandbox for the built-in local profile. */
public final class LocalShell {
    private LocalShell() { }

    public static CommandResult execute(String command, AtomicReference<Process> active)
            throws Exception {
        Process process = new ProcessBuilder("/system/bin/sh", "-c", command)
                .redirectErrorStream(false)
                .start();
        active.set(process);
        try {
            StringBuilder stdout = new StringBuilder();
            StringBuilder stderr = new StringBuilder();
            Thread outReader = readAsync(process.getInputStream(), stdout);
            Thread errReader = readAsync(process.getErrorStream(), stderr);
            boolean finished = process.waitFor(30, TimeUnit.SECONDS);
            if (!finished) {
                process.destroy();
                throw new Exception("命令执行超过 30 秒，已停止");
            }
            outReader.join(1000);
            errReader.join(1000);
            return new CommandResult(stdout.toString(), stderr.toString(), process.exitValue());
        } finally {
            active.compareAndSet(process, null);
        }
    }

    private static Thread readAsync(java.io.InputStream stream, StringBuilder target) {
        Thread thread = new Thread(() -> {
            try (BufferedReader reader = new BufferedReader(
                    new InputStreamReader(stream, StandardCharsets.UTF_8))) {
                char[] buffer = new char[1024];
                int count;
                while ((count = reader.read(buffer)) >= 0) target.append(buffer, 0, count);
            } catch (Exception ignored) {
                // The process may be intentionally interrupted.
            }
        }, "relayterm-output");
        thread.start();
        return thread;
    }
}
