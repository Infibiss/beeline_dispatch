package ru.fieldrouter.engineer;

import android.content.ContentValues;
import android.content.Context;
import android.database.Cursor;
import android.database.sqlite.SQLiteDatabase;
import android.database.sqlite.SQLiteOpenHelper;
import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;
import android.util.Base64;
import org.json.JSONObject;
import javax.crypto.Cipher;
import javax.crypto.KeyGenerator;
import javax.crypto.SecretKey;
import javax.crypto.spec.GCMParameterSpec;
import java.security.KeyStore;
import java.nio.charset.StandardCharsets;

/** Private encrypted credentials and bounded persistent queue; Android backup is disabled. */
public final class PrivateStore extends SQLiteOpenHelper {
    private static PrivateStore instance;
    private static final String ALIAS = "field-router-gps-v1";
    public static synchronized PrivateStore get(Context c) {
        if (instance == null) instance = new PrivateStore(c.getApplicationContext());
        return instance;
    }
    private PrivateStore(Context c) { super(c, "native-gps.db", null, 1); setWriteAheadLoggingEnabled(true); }
    @Override public void onCreate(SQLiteDatabase db) {
        db.execSQL("CREATE TABLE session (id INTEGER PRIMARY KEY, data TEXT NOT NULL)");
        db.execSQL("CREATE TABLE points (stamp INTEGER PRIMARY KEY, data TEXT NOT NULL)");
    }
    @Override public void onUpgrade(SQLiteDatabase db, int old, int next) { throw new IllegalStateException("Unsupported GPS database"); }
    private SecretKey key() throws Exception {
        KeyStore store=KeyStore.getInstance("AndroidKeyStore"); store.load(null);
        if (!store.containsAlias(ALIAS)) {
            KeyGenerator generator=KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore");
            generator.init(new KeyGenParameterSpec.Builder(ALIAS, KeyProperties.PURPOSE_ENCRYPT|KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM).setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE).build());
            generator.generateKey();
        }
        return (SecretKey)store.getKey(ALIAS, null);
    }
    private String encrypt(String raw) throws Exception {
        Cipher c=Cipher.getInstance("AES/GCM/NoPadding"); c.init(Cipher.ENCRYPT_MODE,key());
        return Base64.encodeToString(c.getIV(),Base64.NO_WRAP)+":"+Base64.encodeToString(c.doFinal(raw.getBytes(StandardCharsets.UTF_8)),Base64.NO_WRAP);
    }
    private String decrypt(String raw) throws Exception {
        String[] parts=raw.split(":",2); Cipher c=Cipher.getInstance("AES/GCM/NoPadding");
        c.init(Cipher.DECRYPT_MODE,key(),new GCMParameterSpec(128,Base64.decode(parts[0],Base64.NO_WRAP)));
        return new String(c.doFinal(Base64.decode(parts[1],Base64.NO_WRAP)),StandardCharsets.UTF_8);
    }
    public synchronized JSONObject session() throws Exception {
        try(Cursor c=getReadableDatabase().rawQuery("SELECT data FROM session WHERE id=1",null)) {
            return c.moveToFirst() ? new JSONObject(decrypt(c.getString(0))) : new JSONObject();
        }
    }
    public synchronized void save(JSONObject value) throws Exception {
        ContentValues v=new ContentValues(); v.put("id",1); v.put("data",encrypt(value.toString()));
        if(getWritableDatabase().insertWithOnConflict("session",null,v,SQLiteDatabase.CONFLICT_REPLACE)<0)
            throw new java.io.IOException("Cannot persist GPS session");
    }
    public synchronized void begin(JSONObject value) throws Exception {
        SQLiteDatabase db=getWritableDatabase(); db.beginTransaction();
        try {db.delete("points",null,null);save(value);db.setTransactionSuccessful();} finally {db.endTransaction();}
    }
    public synchronized void stop(String reason) throws Exception {
        JSONObject s=session(); s.put("enabled",false);s.put("pendingStop",s.has("token"));s.put("message",reason);
        SQLiteDatabase db=getWritableDatabase();db.beginTransaction();
        try {save(s);db.delete("points",null,null);db.setTransactionSuccessful();}finally{db.endTransaction();}
    }
    public synchronized void put(JSONObject point) throws Exception {
        if(!session().optBoolean("enabled"))return;
        SQLiteDatabase db=getWritableDatabase();db.beginTransaction();
        try {
            ContentValues v=new ContentValues(); v.put("stamp",point.getLong("timestamp"));v.put("data",encrypt(point.toString()));
            try{db.insertOrThrow("points",null,v);}
            catch(android.database.sqlite.SQLiteConstraintException duplicate){ /* Timestamp idempotency. */ }
            db.delete("points","stamp<?",new String[]{Long.toString(System.currentTimeMillis()-TrackingPolicy.RETENTION_MS)});
            db.execSQL("DELETE FROM points WHERE stamp IN (SELECT stamp FROM points ORDER BY stamp DESC LIMIT -1 OFFSET "+TrackingPolicy.MAX_QUEUE+")");
            db.setTransactionSuccessful();
        } finally {db.endTransaction();}
    }
    // Send newest first so reconnect restores the current position before old history.
    public synchronized JSONObject next() throws Exception {
        try(Cursor c=getReadableDatabase().rawQuery("SELECT data FROM points ORDER BY stamp DESC LIMIT 1",null)) {
            return c.moveToFirst()?new JSONObject(decrypt(c.getString(0))):null;
        }
    }
    public synchronized void ack(long stamp) { getWritableDatabase().delete("points","stamp=?",new String[]{Long.toString(stamp)}); }
    public synchronized long count() {
        try(Cursor c=getReadableDatabase().rawQuery("SELECT COUNT(*) FROM points",null)){c.moveToFirst();return c.getLong(0);}
    }
    public synchronized void note(String message, long sentAt, float accuracy, long measured) throws Exception {
        JSONObject s=session();s.put("message",message);
        if(sentAt>0)s.put("sentAt",sentAt);
        if(measured>0){s.put("measuredAt",measured);s.put("accuracy",accuracy);}
        save(s);
    }
    public synchronized void stoppedOnServer(String token) throws Exception {
        JSONObject s=session();
        if(!s.optString("token").equals(token))return;
        s.remove("token");s.put("pendingStop",false);save(s);
    }
    public synchronized JSONObject status() throws Exception {
        JSONObject s=session(),out=new JSONObject();
        for(String k:new String[]{"enabled","pendingStop","message","sentAt","measuredAt","accuracy","provider","expires","engineer","startedAt","attemptAt","backgroundFixAt","backgroundSentAt","heartbeatAt","serverAccepted","serverReason","serverDetail","serverMeasuredAt"})
            if(s.has(k))out.put(k,s.get(k));
        out.put("queued",count());out.put("serviceRunning",TrackingService.running);
        return out;
    }
    public synchronized void receipt(long measured,JSONObject response,long received) throws Exception {
        JSONObject s=session();s.put("sentAt",received);
        // Uploading old offline history must not overwrite the current fix's verdict.
        if(measured>=s.optLong("serverMeasuredAt")) {
            boolean accepted=response.optBoolean("usable",response.optBoolean("accepted",false));
            String reason=response.optString("reason",""),detail=response.optString("detail","");
            if(detail.length()>1500)detail=detail.substring(0,1500);
            s.put("serverMeasuredAt",measured);s.put("serverAccepted",accepted);
            s.put("serverReason",reason);s.put("serverDetail",detail);
            s.put("message",accepted?"Сервер принял координату для расчёта.":
                "Точка доставлена, но не используется для прогноза. "+reasonText(reason));
        }
        save(s);
    }
    private static String reasonText(String reason) {
        switch(reason) {
            case "jump":return "Обнаружен скачок положения.";
            case "source_conflict":return "Спутники и сетевое положение противоречат друг другу.";
            case "low_accuracy":return "Недостаточная точность.";
            case "mock":return "Телефон пометил измерение как тестовое.";
            case "historical_only":return "Сохранена только в истории.";
            default:return "Ожидаем пригодное измерение.";
        }
    }
    public synchronized void heartbeat(long time) throws Exception {
        JSONObject s=session();s.put("heartbeatAt",time);save(s);
    }
    public synchronized void attempt(long time) throws Exception {
        JSONObject s=session();s.put("attemptAt",time);save(s);
    }
    public synchronized void backgroundEvent(boolean sent,long time) throws Exception {
        JSONObject s=session();s.put(sent?"backgroundSentAt":"backgroundFixAt",time);save(s);
    }
    public synchronized void fixMetadata(float accuracy,long measured,String provider) throws Exception {
        JSONObject s=session();
        s.put("accuracy",accuracy);s.put("measuredAt",measured);s.put("provider",provider);
        s.put("message",accuracy>50?"Координата пока приблизительная. Уточняем по спутникам.":"Координата получена.");
        save(s);
    }
}
