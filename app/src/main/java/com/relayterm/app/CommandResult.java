package com.relayterm.app;

public final class CommandResult {
    public final String stdout;
    public final String stderr;
    public final int exitCode;

    public CommandResult(String stdout, String stderr, int exitCode) {
        this.stdout = stdout == null ? "" : stdout;
        this.stderr = stderr == null ? "" : stderr;
        this.exitCode = exitCode;
    }

    public boolean ok() {
        return exitCode == 0;
    }
}
