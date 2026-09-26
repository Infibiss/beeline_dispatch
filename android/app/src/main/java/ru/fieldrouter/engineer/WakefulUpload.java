package ru.fieldrouter.engineer;

import java.util.concurrent.Executor;

/** Acquire before leaving the location callback, not later in a sleeping timer.
 * One pending upload; bounded lease, released on completion, rejection and stop.
 * Android supplies the lease; pure JVM tests exercise the lifecycle.
 */
public final class WakefulUpload implements AutoCloseable {
    public interface Lease { void acquire(long timeoutMs); void release(); }
    private final Executor executor;
    private final Lease lease;
    private final Runnable task;
    private boolean pending, closed;
    public WakefulUpload(Executor executor, Lease lease, Runnable task) {
        this.executor=executor; this.lease=lease; this.task=task;
    }
    public synchronized void request() {
        if(closed || pending)return;
        pending=true;
        try {
            lease.acquire(30000);
            executor.execute(()->{
                try {
                    synchronized(this){if(closed)return;}
                    task.run();
                } finally {
                    synchronized(this){try{lease.release();}finally{pending=false;}}
                }
            });
        } catch(RuntimeException e) {
            try{lease.release();}finally{pending=false;}
            throw e;
        }
    }
    @Override public synchronized void close() {
        closed=true;lease.release();
    }
}
