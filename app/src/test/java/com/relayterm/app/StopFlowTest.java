package com.relayterm.app;

import org.junit.Test;

import static org.junit.Assert.assertEquals;

public final class StopFlowTest {
    @Test
    public void firstClickOnlyRequestsConfirmation() {
        StopFlow flow = new StopFlow();
        assertEquals(StopFlow.Action.SHOW_STOP_CONFIRMATION, flow.click(true));
        assertEquals(StopFlow.State.STOP_CONFIRMATION, flow.state());
        flow.cancelConfirmation();
        assertEquals(StopFlow.State.IDLE, flow.state());
    }

    @Test
    public void confirmedStopArmsForcePromptAndRequiresSecondConfirmation() {
        StopFlow flow = new StopFlow();
        flow.click(true);
        assertEquals(StopFlow.Action.SEND_INTERRUPT, flow.confirmStop());
        assertEquals(StopFlow.Action.SHOW_FORCE_CONFIRMATION, flow.click(true));
        flow.cancelConfirmation();
        assertEquals(StopFlow.State.INTERRUPT_SENT, flow.state());
        assertEquals(StopFlow.Action.SHOW_FORCE_CONFIRMATION, flow.click(true));
        assertEquals(StopFlow.Action.FORCE_TERMINATE, flow.confirmForce());
        assertEquals(StopFlow.State.IDLE, flow.state());
    }

    @Test
    public void inactiveSessionDoesNothing() {
        StopFlow flow = new StopFlow();
        assertEquals(StopFlow.Action.NONE, flow.click(false));
        assertEquals(StopFlow.State.IDLE, flow.state());
    }
}
