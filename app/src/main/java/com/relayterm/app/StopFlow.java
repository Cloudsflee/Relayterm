package com.relayterm.app;

/**
 * Small, UI-independent state machine for the two-step stop interaction.
 * The first click only requests confirmation; no process signal is emitted
 * until {@link #confirmStop()} is called.
 */
public final class StopFlow {
    public enum State {
        IDLE,
        STOP_CONFIRMATION,
        INTERRUPT_SENT,
        FORCE_CONFIRMATION
    }

    public enum Action {
        NONE,
        SHOW_STOP_CONFIRMATION,
        SEND_INTERRUPT,
        SHOW_FORCE_CONFIRMATION,
        FORCE_TERMINATE
    }

    private State state = State.IDLE;

    public synchronized State state() {
        return state;
    }

    public synchronized boolean isArmed() {
        return state == State.INTERRUPT_SENT || state == State.FORCE_CONFIRMATION;
    }

    /** Handle a stop button click for an active session. */
    public synchronized Action click(boolean active) {
        if (!active) return Action.NONE;
        if (state == State.IDLE) {
            state = State.STOP_CONFIRMATION;
            return Action.SHOW_STOP_CONFIRMATION;
        }
        if (state == State.INTERRUPT_SENT) {
            state = State.FORCE_CONFIRMATION;
            return Action.SHOW_FORCE_CONFIRMATION;
        }
        return Action.NONE;
    }

    /** Confirm the initial stop prompt. */
    public synchronized Action confirmStop() {
        if (state != State.STOP_CONFIRMATION) return Action.NONE;
        state = State.INTERRUPT_SENT;
        return Action.SEND_INTERRUPT;
    }

    /** Confirm the dangerous force-termination prompt. */
    public synchronized Action confirmForce() {
        if (state != State.FORCE_CONFIRMATION) return Action.NONE;
        state = State.IDLE;
        return Action.FORCE_TERMINATE;
    }

    /** Dismiss either prompt while retaining the prior armed state. */
    public synchronized void cancelConfirmation() {
        if (state == State.STOP_CONFIRMATION) state = State.IDLE;
        else if (state == State.FORCE_CONFIRMATION) state = State.INTERRUPT_SENT;
    }

    public synchronized void reset() {
        state = State.IDLE;
    }
}
