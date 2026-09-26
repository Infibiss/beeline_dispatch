package ru.fieldrouter.engineer;

import java.net.URI;

/** Small platform-independent rules, also exercised by the build's JVM checks. */
public final class TrackingPolicy {
    public static final long MAX_SESSION_MS = 12 * 60 * 60 * 1000L;
    public static final long RETENTION_MS = 24 * 60 * 60 * 1000L;
    public static final int MAX_QUEUE = 10000;
    private TrackingPolicy() {}
    public static String origin(String value) {
        try {
            URI uri = new URI(value.trim());
            if (!"https".equalsIgnoreCase(uri.getScheme()) || uri.getHost() == null
                || uri.getRawUserInfo() != null || uri.getRawQuery() != null || uri.getRawFragment() != null
                || (uri.getPath() != null && !uri.getPath().isEmpty() && !"/".equals(uri.getPath()))
                || uri.getPort() == 0 || uri.getPort() > 65535) throw new IllegalArgumentException();
            return new URI("https", null, uri.getHost().toLowerCase(java.util.Locale.ROOT),
                uri.getPort() == 443 ? -1 : uri.getPort(), null, null, null).toASCIIString();
        } catch (Exception e) { throw new IllegalArgumentException("Нужен HTTPS-адрес сервера без пути и пароля"); }
    }
    public static boolean sameOrigin(String page, String origin) {
        try {
            URI p = new URI(page), o = new URI(origin);
            return "https".equalsIgnoreCase(p.getScheme()) && p.getRawUserInfo() == null
                && o.getHost().equalsIgnoreCase(p.getHost())
                && (p.getPort() == -1 ? 443 : p.getPort()) == (o.getPort() == -1 ? 443 : o.getPort());
        } catch (Exception e) { return false; }
    }
    public static boolean validFix(double lat, double lon, float accuracy, long stamp, long now) {
        return Double.isFinite(lat) && Double.isFinite(lon) && Float.isFinite(accuracy)
            && lat >= -90 && lat <= 90 && lon >= -180 && lon <= 180 && accuracy > 0
            && accuracy <= 100000 && stamp <= now + 30000 && stamp >= now - 120000;
    }
    public static long backoff(int failures) { return Math.min(120000L, 5000L << Math.min(5, Math.max(0, failures))); }
}
