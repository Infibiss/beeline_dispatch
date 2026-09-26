package ru.fieldrouter.engineer;

/**
 * A fresh good satellite fix takes precedence over a coarse network fix for
 * two GPS intervals. Accuracy and timestamps are never fabricated.
 */
public final class FixSelector {
    private static final long GPS_GRACE_MS=20000;
    private long lastStamp;
    private long gpsElapsed=-1;
    private float gpsAccuracy=Float.POSITIVE_INFINITY;
    public void reset() {lastStamp=0;gpsElapsed=-1;gpsAccuracy=Float.POSITIVE_INFINITY;}
    public boolean accept(boolean gps,float accuracy,long stamp,long measuredElapsed,long nowElapsed) {
        if(stamp<=lastStamp || !Float.isFinite(accuracy) || accuracy<=0)return false;
        long age=gpsElapsed<0?Long.MAX_VALUE:nowElapsed-gpsElapsed;
        if(!gps && age>=0 && age<GPS_GRACE_MS && gpsAccuracy<=50
            && accuracy>Math.max(50,gpsAccuracy*2))return false;
        lastStamp=stamp;
        if(gps) {gpsElapsed=measuredElapsed;gpsAccuracy=accuracy;}
        return true;
    }
}
