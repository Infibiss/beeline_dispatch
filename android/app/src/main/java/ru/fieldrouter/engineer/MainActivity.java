package ru.fieldrouter.engineer;

import android.Manifest;
import android.app.*;
import android.content.*;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.net.Uri;
import android.net.http.SslError;
import android.os.*;
import android.view.*;
import android.webkit.*;
import android.widget.*;
import org.json.JSONObject;

/** Trusted-origin WebView plus a small origin-checked native command bridge. */
public final class MainActivity extends Activity {
    private WebView web;
    private String origin;
    private PrivateStore store;
    private boolean failed;
    private final Handler handler=new Handler(Looper.getMainLooper());
    private final Runnable refresh=new Runnable(){public void run(){publishStatus();handler.postDelayed(this,3000);}};
    @Override public void onCreate(Bundle state) {
        super.onCreate(state);store=PrivateStore.get(this);
        origin=getPreferences(MODE_PRIVATE).getString("origin",getString(R.string.default_server));
        try {origin=TrackingPolicy.origin(origin);openWorkspace();}catch(Exception e){setup();}
    }
    private void setup() {
        LinearLayout box=new LinearLayout(this);box.setOrientation(LinearLayout.VERTICAL);box.setPadding(32,64,32,32);box.setBackgroundColor(Color.WHITE);
        TextView title=new TextView(this);title.setText("Подключение рабочего пространства");title.setTextSize(24);box.addView(title);
        TextView hint=new TextView(this);hint.setText("Адрес выдаёт диспетчер. В рабочей сборке он настроен заранее.");box.addView(hint);
        EditText url=new EditText(this);url.setSingleLine(true);url.setInputType(android.text.InputType.TYPE_CLASS_TEXT|android.text.InputType.TYPE_TEXT_VARIATION_URI);
        url.setHint("https://…");url.setText(origin);box.addView(url);
        Button go=new Button(this);go.setText("Подключиться");box.addView(go);
        go.setOnClickListener(v->{try{
            String next=TrackingPolicy.origin(url.getText().toString());
            if(origin!=null&&!origin.equals(next))stopTracking("Сервер изменён. GPS остановлен.");
            origin=next;getPreferences(MODE_PRIVATE).edit().putString("origin",origin).apply();openWorkspace();
        }catch(Exception e){url.setError(e.getMessage());}});
        setContentView(box);
    }
    private void openWorkspace() {
        failed=false;web=new WebView(this);
        web.setBackgroundColor(Color.WHITE);
        web.setOnApplyWindowInsetsListener((v,insets)->{
            v.setPadding(insets.getSystemWindowInsetLeft(),insets.getSystemWindowInsetTop(),
                insets.getSystemWindowInsetRight(),insets.getSystemWindowInsetBottom());return insets;
        });
        WebSettings settings=web.getSettings();settings.setJavaScriptEnabled(true);settings.setDomStorageEnabled(true);
        settings.setUserAgentString(settings.getUserAgentString()+" FieldRouterAndroid/1");
        settings.setAllowFileAccess(false);settings.setAllowContentAccess(false);settings.setGeolocationEnabled(false);
        settings.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);settings.setMediaPlaybackRequiresUserGesture(true);
        settings.setSupportMultipleWindows(false);CookieManager.getInstance().setAcceptThirdPartyCookies(web,false);
        WebView.setWebContentsDebuggingEnabled(false);
        web.setWebViewClient(new WebViewClient(){
            @Override public boolean shouldOverrideUrlLoading(WebView view,WebResourceRequest request) {
                String url=request.getUrl().toString();
                if(TrackingPolicy.sameOrigin(url,origin))return false;
                // No native bridge on foreign pages. Only explicit user navigation opens an external app.
                if(request.isForMainFrame() && request.hasGesture()) {
                    String scheme=request.getUrl().getScheme();
                    if("https".equals(scheme)||"tel".equals(scheme)||"geo".equals(scheme))
                        try{startActivity(new Intent(Intent.ACTION_VIEW,request.getUrl()));}catch(Exception ignored){}
                }
                return true;
            }
            @Override public void onPageStarted(WebView view,String url,android.graphics.Bitmap icon) {
                failed=false;
                if(TrackingPolicy.sameOrigin(url,origin) && Uri.parse(url).getPath().equals("/login"))
                    stopTracking("Выход из рабочего профиля. GPS остановлен.");
            }
            @Override public void onPageFinished(WebView view,String url) {
                if(!failed && TrackingPolicy.sameOrigin(url,origin)) {
                    CookieManager.getInstance().flush();
                    view.evaluateJavascript("window.FieldRouterNative={call:function(action,data){return JSON.parse(prompt('field-router-native:'+JSON.stringify({action:action,data:data||{}}),'')||'{\"error\":\"Связь с Android недоступна\"}');}};window.dispatchEvent(new Event('field-router-native-ready'));",null);
                    publishStatus();
                }
            }
            @Override public void onReceivedSslError(WebView view,SslErrorHandler response,SslError error) {
                response.cancel();failed=true;showFailure("Не удалось проверить HTTPS-сертификат. Для локального сервера используйте тестовую APK, собранную с его сертификатом. Проверка безопасности не отключается.");
            }
            @Override public void onReceivedError(WebView view,WebResourceRequest request,WebResourceError error) {
                if(request.isForMainFrame()){failed=true;showFailure("Сервер недоступен. Проверьте Wi-Fi и запущенный сервер. GPS во время активной смены продолжает сохранять точки на телефоне.");}
            }
        });
        web.setWebChromeClient(new WebChromeClient(){
            @Override public boolean onJsPrompt(WebView view,String url,String message,String defaultValue,JsPromptResult result) {
                if(!message.startsWith("field-router-native:"))return false;
                if(!TrackingPolicy.sameOrigin(url,origin)||!TrackingPolicy.sameOrigin(view.getUrl(),origin)) {
                    result.confirm("{\"error\":\"Источник запроса недоступен\"}");return true;
                }
                try {
                    if(message.length()>8192)throw new IllegalArgumentException("Слишком большой запрос");
                    JSONObject request=new JSONObject(message.substring("field-router-native:".length()));
                    result.confirm(command(request.getString("action"),request.optJSONObject("data")).toString());
                }catch(Exception e){result.confirm("{\"error\":\"Не удалось выполнить действие GPS. Проверьте разрешения и войдите снова.\"}");}
                return true;
            }
        });
        setContentView(web);web.loadUrl(origin+"/engineer");
    }
    private void showFailure(String text) {
        runOnUiThread(()->{
            LinearLayout box=new LinearLayout(this);box.setOrientation(LinearLayout.VERTICAL);box.setPadding(32,64,32,32);
            TextView label=new TextView(this);label.setText(text);label.setTextSize(18);box.addView(label);
            TextView gps=new TextView(this);try{gps.setText(store.status().optString("message"));}catch(Exception ignored){}box.addView(gps);
            Button retry=new Button(this);retry.setText("Повторить");retry.setOnClickListener(v->openWorkspace());box.addView(retry);
            Button stop=new Button(this);stop.setText("Остановить GPS");stop.setOnClickListener(v->{stopTracking("Передача остановлена.");gps.setText("GPS остановлен на телефоне.");});box.addView(stop);
            Button server=new Button(this);server.setText("Настроить подключение");server.setOnClickListener(v->setup());box.addView(server);
            setContentView(box);
        });
    }
    private boolean hasLocation() {
        return checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION)==PackageManager.PERMISSION_GRANTED
            ||checkSelfPermission(Manifest.permission.ACCESS_COARSE_LOCATION)==PackageManager.PERMISSION_GRANTED;
    }
    private JSONObject localStatus() throws Exception {
        JSONObject status=store.status().put("permission",hasLocation())
            .put("finePermission",checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION)==PackageManager.PERMISSION_GRANTED)
            .put("appVersion","0.1.3");
        android.location.LocationManager manager=(android.location.LocationManager)getSystemService(LOCATION_SERVICE);
        try{status.put("gpsEnabled",manager.isProviderEnabled(android.location.LocationManager.GPS_PROVIDER));}
        catch(SecurityException ignored) { /* Status remains readable before permission is granted. */ }
        PowerManager power=(PowerManager)getSystemService(POWER_SERVICE);
        status.put("batteryExempt",power.isIgnoringBatteryOptimizations(getPackageName()))
            .put("powerSaveMode",power.isPowerSaveMode())
            .put("notificationsEnabled",((NotificationManager)getSystemService(NOTIFICATION_SERVICE)).areNotificationsEnabled());
        if(Build.VERSION.SDK_INT>=28)
            status.put("backgroundRestricted",((ActivityManager)getSystemService(ACTIVITY_SERVICE)).isBackgroundRestricted());
        android.net.ConnectivityManager net=(android.net.ConnectivityManager)getSystemService(CONNECTIVITY_SERVICE);
        android.net.NetworkCapabilities caps=net.getNetworkCapabilities(net.getActiveNetwork());
        status.put("networkConnected",caps!=null);
        if(caps!=null)status.put("networkType",caps.hasTransport(android.net.NetworkCapabilities.TRANSPORT_WIFI)?"wifi":
            caps.hasTransport(android.net.NetworkCapabilities.TRANSPORT_CELLULAR)?"mobile":"other");
        return status;
    }
    private void backgroundSettings() {
        PowerManager power=(PowerManager)getSystemService(POWER_SERVICE);
        if(power.isIgnoringBatteryOptimizations(getPackageName())) {
            startActivity(new Intent(android.provider.Settings.ACTION_APPLICATION_DETAILS_SETTINGS,Uri.parse("package:"+getPackageName())));
            return;
        }
        new AlertDialog.Builder(this).setTitle("GPS при выключенном экране")
            .setMessage("Разрешите фоновую работу, чтобы Android не откладывал передачу координат во время смены. Непрерывный GPS удерживает процессор во время смены и увеличивает расход батареи. Кнопка «Выключить» прекращает сбор и освобождает ресурсы.")
            .setNegativeButton("Позже",null).setPositiveButton("Открыть настройки",(dialog,which)->{
                try {startActivity(new Intent(android.provider.Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,Uri.parse("package:"+getPackageName())));}
                catch(ActivityNotFoundException e) {
                    startActivity(new Intent(android.provider.Settings.ACTION_APPLICATION_DETAILS_SETTINGS,Uri.parse("package:"+getPackageName())));
                }
            }).show();
    }
    private JSONObject command(String action,JSONObject data) throws Exception {
        if(data==null)data=new JSONObject();
        switch(action) {
            case "status":return localStatus();
            case "permission":
                boolean requested=false;
                if(!hasLocation()){requestPermissions(new String[]{Manifest.permission.ACCESS_FINE_LOCATION,Manifest.permission.ACCESS_COARSE_LOCATION},10);requested=true;}
                else if(Build.VERSION.SDK_INT>=33&&!getPreferences(MODE_PRIVATE).getBoolean("notificationAsked",false)) {
                    getPreferences(MODE_PRIVATE).edit().putBoolean("notificationAsked",true).apply();
                    requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS},11);requested=true;
                }
                return new JSONObject().put("granted",hasLocation()).put("requested",requested);
            case "start":
                if(!hasLocation())throw new SecurityException();
                String token=data.getString("token");long expires=data.getLong("expires");
                if(!token.matches("[A-Za-z0-9_-]{40,100}")||expires<=System.currentTimeMillis()
                    ||expires>System.currentTimeMillis()+TrackingPolicy.MAX_SESSION_MS+30000)throw new IllegalArgumentException();
                JSONObject s=new JSONObject().put("origin",origin).put("token",token).put("expires",expires)
                    .put("sessionId",java.util.UUID.randomUUID().toString())
                    .put("engineer",data.getString("engineer")).put("account",data.getString("account"))
                    .put("enabled",true).put("startedAt",System.currentTimeMillis()).put("pendingStop",false)
                    .put("message","Ищем точную координату. Проверьте условия фоновой работы ниже.");
                store.begin(s);
                try{startForegroundService(new Intent(this,TrackingService.class));}
                catch(Exception e){store.stop("Android не разрешил запустить GPS. Повторите из открытого приложения.");throw e;}
                // Prompt only after an explicit start; never silently change OS battery policy.
                if(!((PowerManager)getSystemService(POWER_SERVICE)).isIgnoringBatteryOptimizations(getPackageName()))
                    backgroundSettings();
                return localStatus();
            case "bind":
                JSONObject current=store.session();
                if(current.has("account") && (!current.optString("account").equals(data.optString("account"))
                    || !current.optString("engineer").equals(data.optString("engineer"))
                    || !current.optString("origin").equals(origin)))stopTracking("Рабочий профиль изменён. GPS остановлен.");
                return localStatus();
            case "stop":stopTracking("Передача остановлена. Неотправленные точки удалены.");return localStatus();
            case "background-settings":
                backgroundSettings();return new JSONObject().put("ok",true);
            case "settings":
                startActivity(new Intent(android.provider.Settings.ACTION_APPLICATION_DETAILS_SETTINGS,Uri.parse("package:"+getPackageName())));
                return new JSONObject().put("ok",true);
            default:throw new IllegalArgumentException();
        }
    }
    private void stopTracking(String reason) {
        try {
            JSONObject s=store.session();
            if(s.optBoolean("enabled")||s.optBoolean("pendingStop")) {
                store.stop(reason);
                startService(new Intent(this,TrackingService.class).setAction(TrackingService.STOP).putExtra("session",s.optString("sessionId")));
            }
        } catch(Exception ignored) {stopService(new Intent(this,TrackingService.class));}
    }
    private void publishStatus() {
        if(web==null||!TrackingPolicy.sameOrigin(web.getUrl(),origin))return;
        try{web.evaluateJavascript("window.dispatchEvent(new CustomEvent('field-router-native-status',{detail:"+localStatus().toString()+"}));",null);}catch(Exception ignored){}
    }
    @Override public void onRequestPermissionsResult(int code,String[] permissions,int[] result) {
        super.onRequestPermissionsResult(code,permissions,result);
        if(web!=null)web.evaluateJavascript("window.dispatchEvent(new Event('field-router-native-permission'));",null);
    }
    @Override public void onResume() {
        super.onResume();handler.post(refresh);
        try {
            JSONObject s=store.session();
            if(s.optBoolean("enabled")&&!TrackingService.running && hasLocation())
                startForegroundService(new Intent(this,TrackingService.class));
            else if(s.optBoolean("pendingStop"))startService(new Intent(this,TrackingService.class).setAction(TrackingService.STOP).putExtra("session",s.optString("sessionId")));
        }catch(Exception ignored){}
    }
    @Override public void onPause(){handler.removeCallbacks(refresh);super.onPause();}
    @Override public void onBackPressed(){if(web!=null&&web.canGoBack())web.goBack();else super.onBackPressed();}
    @Override public void onDestroy(){handler.removeCallbacks(refresh);if(web!=null)web.destroy();super.onDestroy();}
}
