package ru.fieldrouter.engineer;

import android.Manifest;
import android.app.*;
import android.content.*;
import android.content.pm.PackageManager;
import android.content.pm.ServiceInfo;
import android.location.*;
import android.os.*;
import org.json.JSONObject;
import java.io.*;
import java.net.*;
import java.nio.charset.StandardCharsets;
import java.util.Iterator;
import java.util.concurrent.*;
import javax.net.ssl.HttpsURLConnection;

/** All GPS and network work lives here, never in WebView timers or callbacks. */
public final class TrackingService extends Service implements LocationListener {
    public static final String STOP="ru.fieldrouter.engineer.STOP";
    public static volatile boolean running;
    private static final int NOTIFICATION=41;
    private PrivateStore store;
    private LocationManager locations;
    private ScheduledExecutorService worker;
    private ScheduledFuture<?> uploads;
    private volatile WakefulUpload uploadQueue;
    private PowerManager power;
    private ShiftPower shiftPower;
    private long lastContact;
    private boolean heartbeatUnsupported;
    private boolean listening;
    private volatile long retryAt;
    private int failures;
    private final FixSelector fixes=new FixSelector();
    @Override public void onCreate() {
        super.onCreate();store=PrivateStore.get(this);locations=(LocationManager)getSystemService(LOCATION_SERVICE);
        worker=Executors.newSingleThreadScheduledExecutor();
        power=(PowerManager)getSystemService(POWER_SERVICE);
        PowerManager.WakeLock shiftLock=power.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK,"FieldEngineer:ActiveShift");
        shiftLock.setReferenceCounted(false);
        shiftPower=new ShiftPower(new WakefulUpload.Lease(){
            public void acquire(long timeout){shiftLock.acquire(timeout);}
            public void release(){if(shiftLock.isHeld())shiftLock.release();}
        },SystemClock::elapsedRealtime);
        NotificationManager nm=(NotificationManager)getSystemService(NOTIFICATION_SERVICE);
        nm.createNotificationChannel(new NotificationChannel("gps","Передача координат смены",NotificationManager.IMPORTANCE_LOW));
    }
    private Notification notification(String text) {
        PendingIntent open=PendingIntent.getActivity(this,0,new Intent(this,MainActivity.class),PendingIntent.FLAG_IMMUTABLE|PendingIntent.FLAG_UPDATE_CURRENT);
        Intent stopIntent=new Intent(this,TrackingService.class).setAction(STOP);
        try{stopIntent.putExtra("session",store.session().optString("sessionId"));}catch(Exception ignored){}
        PendingIntent stop=PendingIntent.getService(this,1,stopIntent,PendingIntent.FLAG_IMMUTABLE|PendingIntent.FLAG_UPDATE_CURRENT);
        return new Notification.Builder(this,"gps").setSmallIcon(R.drawable.ic_location)
            .setContentTitle("Выездной инженер · GPS").setContentText(text).setContentIntent(open)
            .setOngoing(true).setOnlyAlertOnce(true).setVisibility(Notification.VISIBILITY_PRIVATE)
            .addAction(new Notification.Action.Builder(null,"Остановить GPS",stop).build()).build();
    }
    private void foreground(String text) {
        if(Build.VERSION.SDK_INT>=29)startForeground(NOTIFICATION,notification(text),ServiceInfo.FOREGROUND_SERVICE_TYPE_LOCATION);
        else startForeground(NOTIFICATION,notification(text));
    }
    @Override public int onStartCommand(Intent intent,int flags,int startId) {
        try {
            if(intent!=null && STOP.equals(intent.getAction())) {
                String expected=intent.getStringExtra("session");
                if(expected!=null&&!expected.equals(store.session().optString("sessionId")))return START_STICKY;
                stopLocally("Передача остановлена. Неотправленные точки удалены.");
                // Existing service is foreground; a stop requested without a live service
                // runs as a short normal service from the visible activity.
                worker.execute(this::sendStop);
                return START_NOT_STICKY;
            }
            JSONObject s=store.session();
            if(!s.optBoolean("enabled")) {stopSelf();return START_NOT_STICKY;}
            foreground("Передача координат во время смены");
            if(System.currentTimeMillis()>=s.optLong("expires")) {
                stopLocally("Срок GPS-смены истёк. Включите передачу снова.");
                worker.execute(this::sendStop);return START_NOT_STICKY;
            }
            shiftPower.start(s.optString("sessionId"),s.optLong("expires")-System.currentTimeMillis());
            running=true;
            if(!listening) {
                listening=true;lastContact=0;heartbeatUnsupported=false;retryAt=0;failures=0;
                fixes.reset();
                if(checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION)!=PackageManager.PERMISSION_GRANTED
                    &&checkSelfPermission(Manifest.permission.ACCESS_COARSE_LOCATION)!=PackageManager.PERMISSION_GRANTED)
                    throw new SecurityException();
                for(String provider:new String[]{LocationManager.GPS_PROVIDER,LocationManager.NETWORK_PROVIDER}) {
                    if(locations.getAllProviders().contains(provider)) {
                        try {locations.requestLocationUpdates(provider,10000,0,this,Looper.getMainLooper());}
                        catch(SecurityException ignored) { /* Coarse-only permission can still use network. */ }
                    }
                }
                PowerManager.WakeLock lock=power.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK,"FieldEngineer:Upload");
                lock.setReferenceCounted(false);
                WakefulUpload queue=new WakefulUpload(worker,new WakefulUpload.Lease() {
                    public void acquire(long timeout){lock.acquire(timeout);}
                    public void release(){if(lock.isHeld())lock.release();}
                },this::upload);
                uploadQueue=queue;
                uploads=worker.scheduleWithFixedDelay(()->requestUpload(queue),0,5,TimeUnit.SECONDS);
            }
            return START_STICKY;
        } catch(Exception e) {
            try {stopLocally("GPS не запущен. Проверьте разрешение геолокации и запустите из приложения.");}
            catch(Exception ignored) {}
            stopSelf();return START_NOT_STICKY;
        }
    }
    private void requestUpload(WakefulUpload queue) {
        if(queue==null)return;
        try {queue.request();}catch(RuntimeException ignored) {
            // Shutdown/rejection does not discard the persistent point.
        }
    }
    @Override public void onLocationChanged(Location loc) {
        try {
            long now=System.currentTimeMillis();
            JSONObject session=store.session();
            if(!session.optBoolean("enabled"))return;
            if(now>=session.optLong("expires") || !shiftPower.active()) {
                stopLocally("Срок GPS-смены истёк. Включите передачу снова.");
                worker.execute(this::sendStop);return;
            }
            if(!loc.hasAccuracy()
                ||!TrackingPolicy.validFix(loc.getLatitude(),loc.getLongitude(),loc.getAccuracy(),loc.getTime(),now))return;
            // Mock fixes can be used only by a debug APK; release never represents a mock as real.
            if(loc.isFromMockProvider() && (getApplicationInfo().flags & android.content.pm.ApplicationInfo.FLAG_DEBUGGABLE)==0) {
                store.note("Обнаружена тестовая геолокация. Отключите подмену местоположения.",0,0,0);return;
            }
            boolean satellite=LocationManager.GPS_PROVIDER.equals(loc.getProvider());
            if(!fixes.accept(satellite,loc.getAccuracy(),loc.getTime(),loc.getElapsedRealtimeNanos()/1000000,
                SystemClock.elapsedRealtime()))return;
            JSONObject point=new JSONObject().put("lat",loc.getLatitude()).put("lon",loc.getLongitude())
                .put("accuracy",loc.getAccuracy()).put("timestamp",loc.getTime())
                .put("provider",satellite?"gps":LocationManager.NETWORK_PROVIDER.equals(loc.getProvider())?"network":"unknown")
                .put("mock",loc.isFromMockProvider());
            store.put(point);
            store.fixMetadata(loc.getAccuracy(),loc.getTime(),satellite?"gps":
                LocationManager.NETWORK_PROVIDER.equals(loc.getProvider())?"network":"other");
            if(!power.isInteractive())store.backgroundEvent(false,now);
            requestUpload(uploadQueue);
        } catch(Exception e) {failStorage();}
    }
    private void upload() {
        try {
            JSONObject s=store.session();
            if(!s.optBoolean("enabled"))return;
            if(System.currentTimeMillis()>=s.optLong("expires") || !shiftPower.active()) {
                stopLocally("Срок GPS-смены истёк. Включите передачу снова в приложении.");sendStop();return;
            }
            if(System.currentTimeMillis()<retryAt)return;
            long deadline=System.currentTimeMillis()+12000;
            for(int i=0;i<30 && System.currentTimeMillis()<deadline;i++) {
                JSONObject current=store.session();
                if(!current.optBoolean("enabled")||!current.optString("token").equals(s.optString("token")))return;
                JSONObject point=store.next();
                if(point==null){heartbeat(s);return;}
                JSONObject body=new JSONObject(point.toString()).put("id",s.getString("token"));
                store.attempt(System.currentTimeMillis());
                Reply reply=post(s.getString("origin"),body);
                int status=reply.code;
                if(!store.session().optString("token").equals(s.optString("token")))return;
                if(status==200) {
                    store.ack(point.getLong("timestamp"));failures=0;retryAt=0;lastContact=System.currentTimeMillis();
                    if(!power.isInteractive())store.backgroundEvent(true,System.currentTimeMillis());
                    store.receipt(point.getLong("timestamp"),reply.body,System.currentTimeMillis());
                } else if(status==401 || status==403 || status==409) {
                    stopLocally("Доступ к GPS завершён или телефон заменён. Войдите и включите GPS заново.");
                    store.stoppedOnServer(s.getString("token"));stopSelf();return;
                } else if(status==400 || status==413 || status==415) {
                    // Incompatible protocol must be visible, not silently discarded.
                    store.note("Сервер отклонил GPS ("+status+"). Сообщите диспетчеру.",0,0,0);
                    retryAt=System.currentTimeMillis()+120000;return;
                } else {throw new IOException("HTTP "+status);}
            }
        } catch(javax.net.ssl.SSLException e) {retry("Не удалось проверить сертификат сервера. Проверьте адрес и сборку приложения.");}
        catch(Exception e) {retry("Нет связи с сервером. Точки сохранены на телефоне и будут отправлены автоматически.");}
    }
    private void heartbeat(JSONObject s) throws Exception {
        long now=System.currentTimeMillis();
        if(heartbeatUnsupported || now-lastContact<30000)return;
        int status=post(s.getString("origin"),new JSONObject().put("id",s.getString("token")).put("event","heartbeat")).code;
        JSONObject current=store.session();
        if(!current.optBoolean("enabled") || !current.optString("token").equals(s.optString("token")))return;
        if(status==200){lastContact=now;store.heartbeat(now);failures=0;retryAt=0;}
        else if(status==400||status==404){heartbeatUnsupported=true;} // Older server: location uploads still work.
        else if(status==401||status==403||status==409) {
            stopLocally("Доступ к GPS завершён. Войдите и включите передачу заново.");
            store.stoppedOnServer(s.getString("token"));stopSelf();
        } else throw new IOException("HTTP "+status);
    }
    private void retry(String text) {
        retryAt=System.currentTimeMillis()+TrackingPolicy.backoff(failures++);
        try {store.note(text,0,0,0);}catch(Exception e){failStorage();}
    }
    private static final class Reply {
        final int code;final JSONObject body;
        Reply(int code,JSONObject body){this.code=code;this.body=body;}
    }
    private Reply post(String origin,JSONObject params) throws Exception {
        StringBuilder encoded=new StringBuilder();
        Iterator<String> keys=params.keys();
        while(keys.hasNext()) {
            String k=keys.next();if(encoded.length()>0)encoded.append('&');
            encoded.append(URLEncoder.encode(k,"UTF-8")).append('=').append(URLEncoder.encode(String.valueOf(params.get(k)),"UTF-8"));
        }
        byte[] bytes=encoded.toString().getBytes(StandardCharsets.UTF_8);
        HttpsURLConnection c=(HttpsURLConnection)new URL(TrackingPolicy.origin(origin)+"/api/gps/native").openConnection();
        c.setConnectTimeout(8000);c.setReadTimeout(8000);c.setInstanceFollowRedirects(false);
        c.setRequestMethod("POST");c.setDoOutput(true);c.setFixedLengthStreamingMode(bytes.length);
        c.setRequestProperty("Content-Type","application/x-www-form-urlencoded");
        try {
            try(OutputStream out=c.getOutputStream()){out.write(bytes);}
            int status=c.getResponseCode();
            // Consume bounded response; never log GPS or credentials.
            InputStream input=status<400?c.getInputStream():c.getErrorStream();
            java.io.ByteArrayOutputStream bytesOut=new java.io.ByteArrayOutputStream();
            if(input!=null)try(InputStream in=input){
                byte[] buf=new byte[1024];int n;
                while((n=in.read(buf))!=-1){if(bytesOut.size()+n>8192)throw new IOException("Oversized GPS response");bytesOut.write(buf,0,n);}
            }
            JSONObject body=new JSONObject();
            try{body=new JSONObject(new String(bytesOut.toByteArray(),StandardCharsets.UTF_8));}
            catch(org.json.JSONException ignored){ /* Old tracker servers can return an empty ACK. */ }
            return new Reply(status,body);
        } finally {c.disconnect();}
    }
    private void stopLocally(String message) throws Exception {
        // Release resources even if private storage has failed.
        try {store.stop(message);} finally {releaseTracking();}

    }
    private void sendStop() {
        try {
            JSONObject s=store.session();
            if(s.optBoolean("pendingStop") && s.has("token")) {
                int status=post(s.getString("origin"),new JSONObject().put("id",s.getString("token")).put("event","stop")).code;
                if(status==200||status==401)store.stoppedOnServer(s.getString("token"));
            }
        } catch(Exception ignored) { /* Stop stays pending; next app opening retries. No more locations are collected. */ }
        finally {try{if(!store.session().optBoolean("enabled"))stopSelf();}catch(Exception ignored){stopSelf();}}
    }
    private void releaseTracking() {
        running=false;listening=false;
        if(shiftPower!=null)shiftPower.close();
        if(uploads!=null){uploads.cancel(false);uploads=null;}
        WakefulUpload queue=uploadQueue;uploadQueue=null;
        if(queue!=null)queue.close();
        if(locations!=null)try{locations.removeUpdates(this);}catch(SecurityException ignored){}
        stopForeground(STOP_FOREGROUND_REMOVE);
    }
    private void failStorage() {
        try {store.stop("Хранилище телефона недоступно. GPS остановлен.");}catch(Exception ignored){}
        releaseTracking();stopSelf();
    }
    @Override public void onProviderDisabled(String p) {try{store.note("Проверьте, включена ли геолокация телефона.",0,0,0);}catch(Exception ignored){}}
    @Override public void onProviderEnabled(String p) {}
    @Override public void onStatusChanged(String p,int s,Bundle b) {}
    @Override public android.os.IBinder onBind(Intent intent) {return null;}
    @Override public void onDestroy() {
        releaseTracking();
        if(worker!=null)worker.shutdownNow();
        super.onDestroy();
    }
}
