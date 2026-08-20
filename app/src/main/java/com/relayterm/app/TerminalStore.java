package com.relayterm.app;

import android.content.Context;
import android.content.SharedPreferences;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.List;
import java.util.UUID;

/** Persists only the small amount of state needed by the terminal switcher. */
public final class TerminalStore {
    private static final String PREFS = "relayterm_profiles";
    private static final String PROFILES = "items";
    private static final String SELECTED = "selected";

    private final SharedPreferences preferences;
    private final SecretStore secrets;

    public TerminalStore(Context context) {
        Context app = context.getApplicationContext();
        preferences = app.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
        secrets = new SecretStore();
    }

    public synchronized List<TerminalProfile> load() {
        String raw = preferences.getString(PROFILES, "");
        List<TerminalProfile> result = new ArrayList<>();
        boolean migrated = false;
        if (!raw.isEmpty()) {
            try {
                JSONArray array = new JSONArray(raw);
                for (int i = 0; i < array.length(); i++) {
                    JSONObject item = array.getJSONObject(i);
                    if (!item.has("startupCommand") || !item.has("workingDirectory")
                            || !item.has("managed") || !item.has("remoteProfileId")) migrated = true;
                    try {
                        result.add(TerminalProfile.fromJson(item, secrets));
                    } catch (Exception ignored) {
                        // Keep a malformed profile from hiding the rest of the list.
                    }
                }
            } catch (Exception ignored) {
                // Recreate a clean minimal list below.
            }
        }
        if (result.isEmpty()) {
            result.add(new TerminalProfile(
                    "demo-local",
                    "本机演示",
                    "demo://local",
                    ""));
            save(result);
        } else if (migrated) {
            // Rewrite legacy JSON once so subsequent upgrades carry the new
            // PTY fields while preserving the encrypted token representation.
            save(result);
        }
        return result;
    }

    public synchronized void save(List<TerminalProfile> profiles) {
        JSONArray array = new JSONArray();
        for (TerminalProfile profile : profiles) {
            try {
                array.put(profile.toJson(secrets));
            } catch (Exception ignored) {
                // Do not write a partially encoded profile.
            }
        }
        preferences.edit().putString(PROFILES, array.toString()).apply();
    }

    public synchronized void upsert(TerminalProfile profile) {
        List<TerminalProfile> all = load();
        boolean replaced = false;
        for (int i = 0; i < all.size(); i++) {
            if (all.get(i).id.equals(profile.id)) {
                all.set(i, profile);
                replaced = true;
                break;
            }
        }
        if (!replaced) all.add(profile);
        save(all);
    }

    public synchronized void remove(String id) {
        List<TerminalProfile> all = load();
        all.removeIf(profile -> profile.id.equals(id)
                || (profile.managed && profile.connectionId.equals(id)));
        save(all);
        if (id.equals(selectedId())) {
            preferences.edit().remove(SELECTED).apply();
        }
    }

    /** Replace only one connection's cached server catalog; manual profiles stay untouched. */
    public synchronized void replaceManaged(String connectionId, List<TerminalProfile> managed) {
        List<TerminalProfile> all = load();
        all.removeIf(profile -> profile.managed && profile.connectionId.equals(connectionId));
        all.addAll(managed);
        save(all);
    }

    public synchronized List<TerminalProfile> manualConnections() {
        List<TerminalProfile> result = new ArrayList<>();
        for (TerminalProfile profile : load()) {
            if (!profile.managed && !profile.isLocal()) result.add(profile);
        }
        return result;
    }

    public String selectedId() {
        return preferences.getString(SELECTED, "demo-local");
    }

    public void setSelectedId(String id) {
        preferences.edit().putString(SELECTED, id).apply();
    }

    public String newId() {
        return UUID.randomUUID().toString();
    }
}
