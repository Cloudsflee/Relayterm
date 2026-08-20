package com.relayterm.app;

import java.net.URI;
import java.net.URLDecoder;
import java.util.HashMap;
import java.util.Locale;
import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/** Parses both the HTTPS QR payload and the browser relayterm pairing deep link. */
public final class PairingLink {
    private static final Pattern PAIR_PATH = Pattern.compile("^/pair/([A-Za-z0-9_-]{16,256})$");

    public final String endpoint;
    public final String challenge;

    private PairingLink(String endpoint, String challenge) {
        this.endpoint = endpoint;
        this.challenge = challenge;
    }

    public static PairingLink parse(String rawValue) {
        String value = rawValue == null ? "" : rawValue.trim();
        if (value.isEmpty()) throw invalid();
        try {
            URI uri = new URI(value);
            String scheme = lower(uri.getScheme());
            if ("relayterm".equals(scheme)) return fromDeepLink(uri);
            if ("https".equals(scheme) || "http".equals(scheme)) return fromPairingPage(uri);
        } catch (IllegalArgumentException error) {
            throw error;
        } catch (Exception error) {
            throw invalid();
        }
        throw invalid();
    }

    private static PairingLink fromDeepLink(URI uri) {
        String path = uri.getRawPath() == null ? "" : uri.getRawPath();
        if (!"pair".equalsIgnoreCase(uri.getHost()) || !(path.isEmpty() || "/".equals(path))
                || uri.getPort() != -1 || uri.getUserInfo() != null || uri.getFragment() != null) {
            throw invalid();
        }
        Map<String, String> query = query(uri.getRawQuery());
        return checked(query.get("endpoint"), query.get("challenge"));
    }

    private static PairingLink fromPairingPage(URI uri) throws Exception {
        if (uri.getHost() == null || uri.getHost().isEmpty() || uri.getUserInfo() != null
                || uri.getRawQuery() != null || uri.getFragment() != null) throw invalid();
        Matcher match = PAIR_PATH.matcher(uri.getRawPath() == null ? "" : uri.getRawPath());
        if (!match.matches()) throw invalid();
        URI endpoint = new URI(lower(uri.getScheme()), null, uri.getHost(), uri.getPort(),
                null, null, null);
        return checked(endpoint.toString(), match.group(1));
    }

    private static PairingLink checked(String endpoint, String challenge) {
        String cleanEndpoint = endpoint == null ? "" : endpoint.trim();
        String cleanChallenge = challenge == null ? "" : challenge.trim();
        if (!PAIR_PATH.matcher("/pair/" + cleanChallenge).matches()
                || !CommandPolicy.validateEndpoint(cleanEndpoint).isEmpty()) throw invalid();
        return new PairingLink(cleanEndpoint, cleanChallenge);
    }

    private static Map<String, String> query(String rawQuery) {
        if (rawQuery == null || rawQuery.isEmpty()) throw invalid();
        Map<String, String> values = new HashMap<>();
        for (String part : rawQuery.split("&", -1)) {
            int separator = part.indexOf('=');
            String key = decode(separator < 0 ? part : part.substring(0, separator));
            String value = decode(separator < 0 ? "" : part.substring(separator + 1));
            if (("endpoint".equals(key) || "challenge".equals(key))
                    && values.put(key, value) != null) throw invalid();
        }
        return values;
    }

    private static String decode(String value) {
        try {
            return URLDecoder.decode(value, "UTF-8");
        } catch (Exception error) {
            throw invalid();
        }
    }

    private static String lower(String value) {
        return value == null ? "" : value.toLowerCase(Locale.US);
    }

    private static IllegalArgumentException invalid() {
        return new IllegalArgumentException("invalid_pairing_link");
    }
}
