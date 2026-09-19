package com.relayterm.app;

import org.junit.Test;
import org.json.JSONObject;
import org.json.JSONArray;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

public final class TerminalProfileTest {
    @Test
    public void failedOpenAndClosedPtyStopRetryButInputValidationKeepsSession() throws Exception {
        assertTrue(PtyClient.isTerminalError(
                new JSONObject().put("code", "codex_thread_unavailable"), false));
        assertTrue(PtyClient.isTerminalError(
                new JSONObject().put("code", "Pty is closed"), true));
        assertTrue(PtyClient.isTerminalError(
                new JSONObject().put("code", "codex_launch_failed").put("fatal", true), true));
        assertFalse(PtyClient.isTerminalError(
                new JSONObject().put("code", "signal_invalid"), true));
    }

    private TerminalProfile profile(String id, boolean pinned, String opened) {
        return new TerminalProfile(id, id, "demo://local", "", "codex", "", "pwsh", pinned,
                true, false, "", "", opened);
    }

    @Test
    public void pinsKeepManualOrderThenRecentTimesUseArrayFallbacks() {
        List<TerminalProfile> values = Arrays.asList(
                profile("pin-old", true, "2026-08-01T00:00:00Z"),
                profile("pin-new", true, "2026-09-02T00:00:00Z"),
                profile("old", false, "2026-08-01T00:00:00Z"),
                profile("same-b", false, "2026-08-02T00:00:00Z"),
                profile("same-a", false, "2026-08-02T00:00:00Z"),
                profile("missing-z", false, "broken"),
                profile("missing-a", false, ""));
        List<TerminalProfile> sorted = TerminalProfile.sortByRecent(values);
        assertEquals(Arrays.asList(
                "pin-old", "pin-new", "same-b", "same-a", "old", "missing-z", "missing-a"),
                ids(sorted));
        assertTrue(TerminalProfile.parseLastOpenedAt("broken") == null);
    }

    @Test
    public void pinnedCacheRoundTripDropsLegacyOrder() throws Exception {
        JSONObject remote = new JSONObject()
                .put("id", "remote")
                .put("name", "Remote")
                .put("workingDirectory", "C:\\workspace")
                .put("shell", "pwsh")
                .put("pinned", true)
                .put("lastOpenedAt", "2026-08-02T00:00:00Z");
        TerminalProfile managed = TerminalProfile.managed(
                "connection", "https://bridge.example", "", remote);
        assertTrue(managed.pinned);

        JSONObject cached = managed.toJson(new SecretStore());
        assertTrue(cached.getBoolean("pinned"));
        assertFalse(cached.has("order"));
        TerminalProfile restored = TerminalProfile.fromJson(cached, new SecretStore());
        assertTrue(restored.pinned);
        assertEquals("remote", restored.remoteProfileId);
    }

    @Test
    public void bridgeReplacementPreservesCatalogAndLocalArrayOrder() throws Exception {
        TerminalProfile localA = new TerminalProfile("local-a", "Local A", "demo://local", "");
        TerminalProfile localB = new TerminalProfile("local-b", "Local B", "demo://local", "");
        TerminalProfile stale = TerminalProfile.managed(
                "connection", "https://bridge.example", "",
                new JSONObject().put("id", "stale").put("name", "Stale")
                        .put("workingDirectory", "C:\\workspace"));
        List<TerminalProfile> incoming = Arrays.asList(
                profileFromRemote("pin-a", true, "2026-08-01T00:00:00Z"),
                profileFromRemote("pin-b", true, "2026-09-01T00:00:00Z"),
                profileFromRemote("plain-old", false, "2026-08-01T00:00:00Z"),
                profileFromRemote("plain-new", false, "2026-08-02T00:00:00Z"));

        List<TerminalProfile> cached = TerminalStore.replaceManagedProfiles(
                Arrays.asList(localA, stale, localB), "connection", incoming);
        assertEquals(Arrays.asList(
                "local-a", "pc:connection:pin-a", "pc:connection:pin-b",
                "pc:connection:plain-old", "pc:connection:plain-new", "local-b"), ids(cached));
        assertFalse(localA.pinned);
        assertFalse(localB.pinned);

        List<TerminalProfile> displayed = TerminalStore.sortProfiles(cached);
        assertEquals(Arrays.asList(
                "pc:connection:pin-a", "pc:connection:pin-b", "pc:connection:plain-new",
                "pc:connection:plain-old", "local-a", "local-b"), ids(displayed));
    }

    @Test
    public void closedDesktopAndExitedStatesStayDistinct() {
        assertEquals("终端已关闭 · 会话可恢复",
                TerminalProfile.displaySessionStatus(true, "running", "closed", false, "observer"));
        assertEquals("观察端 · 运行中",
                TerminalProfile.displaySessionStatus(true, "running", "closed", true, "observer"));
        assertEquals("已退出",
                TerminalProfile.displaySessionStatus(false, "exited", "closed", true, "controller"));
    }

    @Test
    public void managedCodexLaunchFieldsAndExplicitUuidRoundTrip() throws Exception {
        String threadId = "019c5a2f-87f6-7db0-babc-2bb3923347a3";
        JSONObject remote = new JSONObject()
                .put("id", "codex")
                .put("name", "Codex")
                .put("workingDirectory", "C:\\workspace")
                .put("shell", "pwsh")
                .put("launchMode", "codex")
                .put("codexArgs", new JSONArray().put("--yolo"));
        TerminalProfile profile = TerminalProfile.managed(
                "connection", "https://bridge.example", "token", remote);
        assertTrue(profile.usesCodexSessions());
        assertEquals(Arrays.asList("--yolo"), profile.codexArgs);

        JSONObject open = PtyClient.buildOpenMessage(
                profile, "android-test", 140, 42, false, threadId);
        assertEquals(profile.remoteProfileId, open.getString("profileId"));
        assertEquals(threadId, open.getString("codexThreadId"));
        assertFalse(open.getBoolean("resume"));

        JSONObject cached = profile.toJson(new SecretStore());
        assertEquals("codex", cached.getString("launchMode"));
        TerminalProfile restored = TerminalProfile.fromJson(cached, new SecretStore());
        assertTrue(restored.usesCodexSessions());
        assertEquals(Arrays.asList("--yolo"), restored.codexArgs);
    }

    private List<String> ids(List<TerminalProfile> values) {
        List<String> ids = new ArrayList<>();
        for (TerminalProfile value : values) ids.add(value.id);
        return ids;
    }

    private TerminalProfile profileFromRemote(String id, boolean pinned, String opened)
            throws Exception {
        return TerminalProfile.managed(
                "connection", "https://bridge.example", "",
                new JSONObject().put("id", id).put("name", id)
                        .put("workingDirectory", "C:\\workspace")
                        .put("pinned", pinned).put("lastOpenedAt", opened));
    }
}
