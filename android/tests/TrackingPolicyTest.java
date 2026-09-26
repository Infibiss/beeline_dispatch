import ru.fieldrouter.engineer.TrackingPolicy;
import ru.fieldrouter.engineer.FixSelector;
import ru.fieldrouter.engineer.WakefulUpload;
import ru.fieldrouter.engineer.ShiftPower;
import java.util.ArrayDeque;
import java.util.concurrent.RejectedExecutionException;
public class TrackingPolicyTest {
    private static final class TestLease implements WakefulUpload.Lease {
        boolean held;int acquired;long timeout;
        public void acquire(long ms){assert !held;held=true;acquired++;timeout=ms;}
        public void release(){held=false;}
    }
    private static void wakefulUploads() {
        ArrayDeque<Runnable> tasks=new ArrayDeque<>();
        TestLease lease=new TestLease();int[] sent={0};
        WakefulUpload uploads=new WakefulUpload(task->{
            assert lease.held;tasks.add(task); // Already awake BEFORE submitting, not inside worker.
        },lease,()->{assert lease.held;sent[0]++;});
        uploads.request();uploads.request();
        assert tasks.size()==1 && lease.timeout==30000 && lease.acquired==1;
        tasks.remove().run();
        assert sent[0]==1 && !lease.held;
        uploads.request();assert lease.acquired==2;
        uploads.close();assert !lease.held;
        tasks.remove().run();assert sent[0]==1; // Stop prevents an already queued task.
        uploads.request();assert tasks.isEmpty();
        TestLease rejected=new TestLease();
        WakefulUpload reject=new WakefulUpload(task->{throw new RejectedExecutionException();},rejected,()->{});
        try{reject.request();throw new AssertionError();}catch(RejectedExecutionException expected){}
        assert !rejected.held;
        TestLease failed=new TestLease();
        WakefulUpload error=new WakefulUpload(tasks::add,failed,()->{throw new IllegalStateException();});
        error.request();
        try{tasks.remove().run();throw new AssertionError();}catch(IllegalStateException expected){}
        assert !failed.held;
        error.request();assert failed.held; // Failure releases gate, so retry is possible.
        error.close();tasks.remove().run();assert !failed.held;
    }
    public static void main(String[] args) {
        assert TrackingPolicy.origin("https://example.org:443/").equals("https://example.org");
        assert TrackingPolicy.sameOrigin("https://example.org/r/east/engineer","https://example.org");
        for(String u:new String[]{"http://example.org","https://user:pass@example.org","https://example.org/x","file:///x","https://example.org?token=x"})
            try{TrackingPolicy.origin(u);throw new AssertionError(u);}catch(IllegalArgumentException expected){}
        assert !TrackingPolicy.sameOrigin("https://example.org.evil.test/","https://example.org");
        assert !TrackingPolicy.sameOrigin("https://example.org:443/","https://example.org:8768");
        assert !TrackingPolicy.sameOrigin("https://user@example.org/","https://example.org");
        long now=1000000;
        assert TrackingPolicy.validFix(55,37,8,now,now);
        assert TrackingPolicy.validFix(55,37,20000,now,now); // Never fabricate better accuracy.
        assert !TrackingPolicy.validFix(55,37,0,now,now);
        assert !TrackingPolicy.validFix(Double.NaN,37,8,now,now);
        assert !TrackingPolicy.validFix(55,37,8,now-180000,now);
        assert !TrackingPolicy.validFix(55,37,8,now+60000,now);
        assert TrackingPolicy.backoff(0)==5000 && TrackingPolicy.backoff(20)==120000;
        FixSelector fixes=new FixSelector();
        assert fixes.accept(false,100,1000,1000,1000); // Initial network fix remains honest.
        assert fixes.accept(true,8,2000,2000,2000);
        assert !fixes.accept(false,100,3000,3000,3000); // Coarse network cannot replace fresh GPS.
        assert !fixes.accept(false,100,21999,21999,21999);
        assert fixes.accept(false,100,22000,22000,22000); // Fallback, never freeze indefinitely.
        assert !fixes.accept(true,8,21000,21000,23000); // Out-of-order is not made "current".
        assert fixes.accept(true,9,23000,23000,23000);
        assert fixes.accept(false,7,24000,24000,24000); // A precise network fix is useful too.
        fixes.reset();
        assert fixes.accept(true,200,25000,25000,25000);
        assert fixes.accept(false,100,26000,26000,26000); // Poor GPS is not blindly preferred.
        fixes.reset();
        assert fixes.accept(true,8,27000,0,27000);
        assert fixes.accept(false,100,28000,28000,28000); // Grace uses measurement age.
        wakefulUploads();
        TestLease lease=new TestLease();long[] clock={1000};
        ShiftPower shift=new ShiftPower(lease,()->clock[0]);
        shift.start("a",60000);assert lease.held && lease.timeout==60000;
        clock[0]+=10000;shift.start("a",60000);assert lease.timeout==50000; // Cannot extend on resume.
        clock[0]+=50000;assert !shift.active();
        shift.close();assert !lease.held;
        shift.start("b",Long.MAX_VALUE);assert lease.timeout==TrackingPolicy.MAX_SESSION_MS;
        shift.close();assert !lease.held && !shift.active();
        shift.start("c",-1);assert !lease.held && !shift.active();
        System.out.println("PASS: origin isolation, location validation, retries, source selection, wakeful upload lifecycle.");
    }
}
