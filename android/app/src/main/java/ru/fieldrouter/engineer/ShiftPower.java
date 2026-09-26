package ru.fieldrouter.engineer;
import java.util.function.LongSupplier;

/** CPU lease only for explicitly enabled tracking, bounded by the current shift.
 * Repeated starts never extend the same session's monotonic deadline.
 */
public final class ShiftPower implements AutoCloseable {
    private final WakefulUpload.Lease lease;
    private final LongSupplier clock;
    private String session="";
    private long deadline;
    public ShiftPower(WakefulUpload.Lease lease,LongSupplier clock){this.lease=lease;this.clock=clock;}
    public synchronized void start(String id,long remainingMs) {
        long now=clock.getAsLong(), duration=Math.max(0,Math.min(TrackingPolicy.MAX_SESSION_MS,remainingMs));
        long next=now+duration;
        if(session.equals(id))next=Math.min(deadline,next);
        lease.release();session=id;deadline=next;
        if(next>now)lease.acquire(next-now);
    }
    public synchronized boolean active(){return !session.isEmpty() && clock.getAsLong()<deadline;}
    @Override public synchronized void close(){session="";deadline=0;lease.release();}
}
