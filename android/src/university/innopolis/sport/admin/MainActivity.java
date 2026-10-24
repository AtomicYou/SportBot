package university.innopolis.sport.admin;

import android.app.*;
import android.os.*;
import android.graphics.Color;
import android.graphics.Typeface;
import android.view.*;
import android.widget.*;
import org.json.*;
import java.net.*;
import java.io.*;
import java.nio.charset.StandardCharsets;
import java.util.Iterator;
import java.util.concurrent.*;
import javax.net.ssl.HttpsURLConnection;

/** Native owner console. No bot token, WebView, credential storage or TLS overrides. */
public class MainActivity extends Activity {
    private LinearLayout root, content;
    private TextView status;
    private String base="", token="", database="main", table="", query="";
    private int page=1;
    private long expires=0;
    private boolean busy=false;
    private final ExecutorService worker=Executors.newSingleThreadExecutor();
    private final int ink=Color.rgb(27,49,43), paper=Color.rgb(247,245,238);
    interface Work { JSONObject run() throws Exception; }
    interface Done { void run(JSONObject value) throws Exception; }

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        getWindow().setFlags(WindowManager.LayoutParams.FLAG_SECURE,WindowManager.LayoutParams.FLAG_SECURE);
        login();
    }
    private int dp(int n){return (int)(n*getResources().getDisplayMetrics().density);}
    private TextView text(String value,int size){
        TextView t=new TextView(this); t.setText(value);t.setTextSize(size);t.setTextColor(ink);
        t.setPadding(0,dp(8),0,dp(8));t.setTextIsSelectable(true);return t;
    }
    private void screen(String title,boolean navigation){
        root=new LinearLayout(this);root.setOrientation(1);root.setBackgroundColor(paper);
        root.setPadding(dp(20),dp(24),dp(20),dp(12));
        root.setOnApplyWindowInsetsListener((v,i)->{v.setPadding(dp(20),Math.max(dp(24),i.getSystemWindowInsetTop()),dp(20),Math.max(dp(12),i.getSystemWindowInsetBottom()));return i;});
        setContentView(root);
        TextView heading=text(title,30);heading.setTypeface(Typeface.create("serif",Typeface.NORMAL));root.addView(heading);
        if(navigation){
            HorizontalScrollView navScroll=new HorizontalScrollView(this);
            LinearLayout nav=new LinearLayout(this);navScroll.addView(nav);root.addView(navScroll);
            button(nav,"Люди",()->overview());button(nav,"Функции",()->features());button(nav,"Базы",()->databases());button(nav,"Журнал",()->audit());
            button(nav,"Система",()->system());button(nav,"Устройства",()->audience());button(nav,"Входы APK",()->devices());button(nav,"Копии",()->backups());
            button(nav,"Выйти",()->logout());
        }
        status=text("",14);root.addView(status);
        ScrollView scroll=new ScrollView(this);content=new LinearLayout(this);content.setOrientation(1);
        scroll.addView(content);root.addView(scroll,new LinearLayout.LayoutParams(-1,0,1));
    }
    private void button(LinearLayout parent,String label,Runnable click){
        Button b=new Button(this);b.setText(label);b.setAllCaps(false);b.setTextColor(ink);
        parent.addView(b);b.setOnClickListener(v->{if(!busy)click.run();});
    }
    private EditText input(String hint){EditText e=new EditText(this);e.setHint(hint);e.setTextSize(16);e.setSingleLine(true);content.addView(e);return e;}
    private void login(){
        token="";expires=0;screen("Inno Sport\nAdmin",false);
        content.addView(text("Отправь /moderation боту @sport_inno_bot. Скопируй выданную строку и вставь сюда.",17));
        EditText connection=input("INNOSPORT|https://…|код");
        EditText device=input("Название устройства");
        device.setText(Build.MANUFACTURER+" "+Build.MODEL+" · Android "+Build.VERSION.RELEASE);
        connection.setInputType(android.text.InputType.TYPE_CLASS_TEXT|android.text.InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS);
        button(content,"Подключиться",()->{
            String[] parts=connection.getText().toString().trim().split("\\|",-1);
            try {
                if(parts.length!=3||!parts[0].equals("INNOSPORT"))throw new Exception();
                URI url=new URI(parts[1]);
                if(!"https".equals(url.getScheme())||url.getHost()==null||url.getRawUserInfo()!=null||url.getPort()!=-1||
                   url.getRawQuery()!=null||url.getRawFragment()!=null||!(url.getPath().isEmpty()||url.getPath().equals("/")))throw new Exception();
                base="https://"+url.getHost();
                JSONObject data=new JSONObject().put("code",parts[2]).put("device",device.getText().toString().trim());connection.setText("");
                request(()->api("/api/moderator/login","POST",data),r->{token=r.getString("token");expires=System.currentTimeMillis()+r.getLong("expires_in")*1000;overview();});
            }catch(Exception e){status.setText("Вставь строку целиком из сообщения бота.");}
        });
        content.addView(text("Код действует 5 минут. Сессия — 30 минут. Для отзыва всех сессий: /modrevoke в Telegram.",14));
    }
    private void request(Work job,Done done){
        if(busy)return;busy=true;status.setText("Загрузка…");
        worker.execute(()->{
            try{JSONObject value=job.run();runOnUiThread(()->{busy=false;if(isDestroyed())return;status.setText("");try{done.run(value);}catch(Exception e){status.setText("Не удалось прочитать ответ сервера.");}});}
            catch(Exception error){final String message=error.getMessage();runOnUiThread(()->{busy=false;if(isDestroyed())return;
                if("AUTH".equals(message)){login();status.setText("Сессия истекла. Получи новый код в Telegram.");}
                else status.setText(message==null?"Ошибка подключения. Проверь сеть и сервер.":message);
            });}
        });
    }
    private JSONObject api(String path,String method,JSONObject body)throws Exception{
        if(!token.isEmpty()&&System.currentTimeMillis()>=expires)throw new Exception("AUTH");
        HttpsURLConnection c=(HttpsURLConnection)new URL(base+path).openConnection();
        c.setInstanceFollowRedirects(false);c.setConnectTimeout(15000);c.setReadTimeout(45000);c.setRequestMethod(method);
        c.setRequestProperty("Accept","application/json");
        if(!token.isEmpty())c.setRequestProperty("Authorization","Bearer "+token);
        try{
            if(body!=null){c.setDoOutput(true);c.setRequestProperty("Content-Type","application/json; charset=utf-8");try(OutputStream out=c.getOutputStream()){out.write(body.toString().getBytes(StandardCharsets.UTF_8));}}
            int code=c.getResponseCode();if(code==401&&!token.isEmpty())throw new Exception("AUTH");
            InputStream stream=code>=400?c.getErrorStream():c.getInputStream();
            if(stream==null)throw new Exception("Сервер недоступен. Получи свежую строку через /moderation.");
            ByteArrayOutputStream bytes=new ByteArrayOutputStream();byte[] buffer=new byte[8192];int n;
            try(InputStream in=stream){while((n=in.read(buffer))!=-1){if(bytes.size()+n>32*1024*1024)throw new Exception("Ответ слишком большой. Сузь поиск.");bytes.write(buffer,0,n);}}
            JSONObject result;
            try{result=new JSONObject(bytes.toString("UTF-8"));}catch(JSONException e){throw new Exception("Сервер недоступен. Проверь туннель и получи свежий код /moderation.");}
            if(code>=300)throw new Exception(result.optString("error","Ошибка сервера: "+code));return result;
        }catch(javax.net.ssl.SSLException e){throw new Exception("Ошибка защищённого соединения. Проверь сеть и дату телефона.");}
        catch(java.net.SocketTimeoutException e){throw new Exception("Сервер не ответил вовремя. Попробуй ещё раз.");}
        catch(java.net.UnknownHostException e){throw new Exception("Адрес сервера недоступен. Получи свежую строку: /moderation.");}
        finally{c.disconnect();}
    }
    private static String enc(String s)throws Exception{return URLEncoder.encode(s,"UTF-8").replace("+","%20");}
    private void overview(){
        screen("Пользователи",true);
        button(content,"Обновить",()->overview());
        request(()->api("/api/admin/overview","GET",null),r->{
            JSONArray users=r.getJSONArray("users");content.addView(text("Подключено: "+users.length(),16));
            for(int i=0;i<users.length();i++){JSONObject user=users.getJSONObject(i);String code=user.getString("code");
                button(content,code+" · целей: "+user.getInt("wish_count")+(user.getBoolean("paused")?" · пауза":""),()->user(code));}
        });
    }
    private void user(String code){
        screen(code,true);button(content,"Обновить",()->user(code));
        request(()->api("/api/admin/users/"+enc(code),"GET",null),r->{
            content.addView(text(r.getBoolean("paused")?"Автозапись на паузе":"Автозапись включена",16));
            button(content,"Приостановить автозапись",()->confirm("Пользователь попросил приостановить автозапись?",()->mutation("/api/admin/users/"+code+"/pause","POST",()->user(code))));
            content.addView(text("Автозаписи",22));JSONArray wishes=r.getJSONArray("wishlist");
            if(wishes.length()==0)content.addView(text("Целей нет",16));
            String[] days={"пн","вт","ср","чт","пт","сб","вс"};
            for(int i=0;i<wishes.length();i++){
                JSONObject w=wishes.getJSONObject(i);StringBuilder ds=new StringBuilder();JSONArray wd=w.getJSONArray("weekdays");
                for(int j=0;j<wd.length();j++)ds.append(days[wd.getInt(j)]).append(" ");
                content.addView(text(w.getString("sport")+"\n"+ds+" · "+w.getInt("time_from")+":00–"+w.getInt("time_to")+":00",17));
                String id=w.getString("id");button(content,"Удалить цель",()->confirm("Пользователь попросил удалить эту цель?",()->{try{mutation("/api/admin/users/"+code+"/wishes/"+enc(id),"DELETE",()->user(code));}catch(Exception e){status.setText("Некорректный ID");}}));
            }
            content.addView(text("Будущие записи",22));
            if(!r.getBoolean("portal_available"))content.addView(text("Портал временно недоступен",16));
            JSONArray bookings=r.getJSONArray("bookings");if(bookings.length()==0)content.addView(text("Записей нет",16));
            for(int i=0;i<bookings.length();i++){JSONObject b=bookings.getJSONObject(i);content.addView(text(b.getString("title")+"\n"+b.getString("start"),17));
                String id=b.getString("id");button(content,"Отменить запись",()->confirm("Пользователь попросил отменить эту запись?",()->{try{mutation("/api/admin/users/"+code+"/bookings/"+enc(id)+"/cancel","POST",()->user(code));}catch(Exception e){status.setText("Некорректный ID");}}));}
        });
    }
    private void confirm(String message,Runnable yes){new AlertDialog.Builder(this).setTitle("Подтверждение").setMessage(message).setNegativeButton("Назад",null).setPositiveButton("Да, по его просьбе",(d,w)->yes.run()).show();}
    private void mutation(String path,String method,Runnable done){request(()->api(path,method,new JSONObject().put("requested_by_user",true)),r->done.run());}
    private void features(){
        screen("Функции бота",true);
        request(()->api("/api/admin/overview","GET",null),r->{JSONObject flags=r.getJSONObject("features");
            String[] keys={"auto_booking","manual_booking","new_wishes","new_logins"};
            String[] labels={"Автоматическая запись","Разовая запись","Новые цели","Новые подключения"};
            Switch[] switches=new Switch[keys.length];
            for(int i=0;i<keys.length;i++){Switch s=new Switch(this);s.setText(labels[i]);s.setChecked(flags.getBoolean(keys[i]));s.setPadding(0,dp(14),0,dp(14));content.addView(s);switches[i]=s;}
            button(content,"Сохранить",()->{new AlertDialog.Builder(this).setTitle("Изменить функции для всех пользователей?").setNegativeButton("Назад",null).setPositiveButton("Сохранить",(d,w)->request(()->{JSONObject data=new JSONObject();for(int i=0;i<keys.length;i++)data.put(keys[i],switches[i].isChecked());return api("/api/admin/features","PUT",data);},v->{features();})).show();});
        });
    }
    private void audit(){screen("Журнал модерации",true);button(content,"Обновить",()->audit());request(()->api("/api/admin/overview","GET",null),r->{
        JSONArray rows=r.getJSONArray("audit");for(int i=0;i<rows.length();i++){JSONObject row=rows.getJSONObject(i);content.addView(text(row.getString("time")+"\n"+row.getString("action")+" · "+row.optString("subject")+"\n"+row.optString("details"),15));}
    });}
    private void databases(){
        screen("Базы данных",true);LinearLayout tabs=new LinearLayout(this);content.addView(tabs);
        button(tabs,"bot.sqlite3",()->{database="main";table="";query="";page=1;databases();});
        button(tabs,"journal.sqlite3",()->{database="journal";table="";query="";page=1;databases();});
        content.addView(text(database.equals("main")?"bot.sqlite3":"journal.sqlite3",22));
        request(()->api("/api/admin/databases/"+database,"GET",null),r->{JSONArray tables=r.getJSONArray("tables");
            for(int i=0;i<tables.length();i++){JSONObject t=tables.getJSONObject(i);String name=t.getString("name");button(content,name+" · "+t.getLong("total"),()->{table=name;page=1;query="";rows();});}
        });
    }
    private void rows(){
        screen(table,true);button(content,"К таблицам",()->databases());EditText search=input("Поиск по всем столбцам");search.setText(query);
        button(content,"Найти / обновить",()->{query=search.getText().toString();page=1;rows();});
        request(()->api("/api/admin/databases/"+database+"/"+enc(table)+"?page="+page+"&q="+enc(query),"GET",null),r->{
            long total=r.getLong("total");content.addView(text("Страница "+page+" · строк: "+total,16));LinearLayout pager=new LinearLayout(this);content.addView(pager);
            if(page>1)button(pager,"← Назад",()->{page--;rows();});if((long)page*50<total)button(pager,"Далее →",()->{page++;rows();});
            JSONArray cols=r.getJSONArray("columns"),values=r.getJSONArray("rows");
            for(int i=0;i<values.length();i++){
                JSONObject row=values.getJSONObject(i);content.addView(text("Строка "+(((long)page-1)*50+i+1),18));
                for(int j=0;j<cols.length();j++){String name=cols.getString(j);Object value=row.get(name);content.addView(text(name,13));TextView cell=text(value==JSONObject.NULL?"NULL":value.toString(),15);cell.setTypeface(Typeface.MONOSPACE);content.addView(cell);}
                View line=new View(this);line.setBackgroundColor(0xFFCCD5CD);content.addView(line,new LinearLayout.LayoutParams(-1,dp(1)));
            }
        });
    }
    private void logout(){request(()->api("/api/admin/logout","POST",new JSONObject()),r->login());}
    private String stamp(Object value){
        if(value==null||value==JSONObject.NULL)return "ещё нет данных";
        try{return new java.text.SimpleDateFormat("dd.MM.yyyy HH:mm:ss",java.util.Locale.getDefault()).format(new java.util.Date((long)(Double.parseDouble(value.toString())*1000)));}
        catch(Exception e){return "нет данных";}
    }
    private String state(JSONObject data){return data.isNull("ok")?"нет данных":data.optBoolean("ok")?"доступен":"ошибка";}
    private void system(){
        screen("Состояние системы",true);button(content,"Обновить",()->system());
        request(()->api("/api/admin/system","GET",null),r->{
            long uptime=r.getLong("uptime_seconds");
            content.addView(text("Сервер работает\nВремя работы: "+uptime/3600+" ч "+(uptime%3600)/60+" мин\nЗапущен: "+stamp(r.get("started")),18));
            content.addView(text("Планировщик: "+(r.getBoolean("scheduler_running")?"работает":"остановлен")+"\nПользователей: "+r.getInt("users"),16));
            JSONObject tg=r.getJSONObject("telegram"),portal=r.getJSONObject("portal"),scan=r.getJSONObject("scanner");
            content.addView(text("Telegram API: "+state(tg)+"\nПроверка: "+stamp(tg.opt("checked")),17));
            content.addView(text("Спортивный портал: "+state(portal)+"\nПоследний запрос: "+stamp(portal.opt("checked"))+"\nHTTP: "+(portal.isNull("http_status")?"—":portal.optInt("http_status")),17));
            content.addView(text("Доступность портала показана по последнему рабочему запросу; время проверки указано выше.",14));
            content.addView(text("Поиск занятий: "+(scan.getBoolean("running")?"выполняется":scan.isNull("ok")?"ещё не запускался":scan.getBoolean("ok")?"завершён":"завершён с ошибками")+"\nНачало: "+stamp(scan.opt("started"))+"\nЗавершение: "+stamp(scan.opt("finished")),17));
            if(!r.isNull("backup_error_at"))content.addView(text("Ошибка резервного копирования: "+stamp(r.get("backup_error_at")),17));
            content.addView(text("Последние ошибки",22));JSONArray errors=r.getJSONArray("recent_errors");
            if(errors.length()==0)content.addView(text("За время работы процесса ошибок не зарегистрировано.",16));
            for(int i=errors.length()-1;i>=0;i--){JSONObject e=errors.getJSONObject(i);content.addView(text(stamp(e.get("time"))+" · "+e.getString("category"),15));}
        });
    }
    private void devices(){
        screen("Входы в APK",true);button(content,"Обновить",()->devices());
        content.addView(text("Активные сессии приложения. Название задаётся при входе и помогает отличать устройства.",15));
        button(content,"Завершить остальные сессии",()->new AlertDialog.Builder(this).setTitle("Выйти на остальных устройствах?").setMessage("Неиспользованные коды входа тоже будут отозваны.").setNegativeButton("Назад",null).setPositiveButton("Завершить",(d,w)->request(()->api("/api/admin/devices/revoke-others","POST",new JSONObject()),r->devices())).show());
        request(()->api("/api/admin/devices","GET",null),r->{JSONArray list=r.getJSONArray("devices");
            for(int i=0;i<list.length();i++){JSONObject dev=list.getJSONObject(i);String id=dev.getString("id");boolean current=dev.getBoolean("current");
                content.addView(text(dev.getString("device")+(current?" · это устройство":""),20));
                content.addView(text("Вход: "+stamp(dev.opt("created"))+"\nАктивность: "+stamp(dev.opt("last_seen"))+"\nДействует до: "+stamp(dev.get("expires")),15));
                button(content,current?"Выйти на этом устройстве":"Завершить сессию",()->new AlertDialog.Builder(this).setTitle("Завершить эту сессию?").setNegativeButton("Назад",null).setPositiveButton("Завершить",(d,w)->request(()->api("/api/admin/devices/"+enc(id),"DELETE",null),v->{if(current)login();else devices();})).show());
            }
        });
    }
    private void backups(){
        screen("Резервные копии",true);button(content,"Обновить",()->backups());
        content.addView(text("Обе базы копируются на ноутбуке раз в сутки. Хранятся последние 14 ежедневных и 10 ручных копий. Ключ шифрования и привязка владельца сохраняются вместе с базами.",16));
        button(content,"Создать копию сейчас",()->request(()->api("/api/admin/backups","POST",new JSONObject()),r->backups()));
        request(()->api("/api/admin/backups","GET",null),r->{
            if(r.getBoolean("running"))content.addView(text("Создаётся копия…",17));
            if(!r.isNull("last_error"))content.addView(text("Последняя попытка завершилась ошибкой: "+stamp(r.get("last_error")),16));
            JSONArray list=r.getJSONArray("backups");if(list.length()==0)content.addView(text("Копий пока нет",16));
            for(int i=0;i<list.length();i++){JSONObject b=list.getJSONObject(i);content.addView(text(stamp(b.get("created"))+" · "+(b.getString("kind").equals("daily")?"автоматическая":"ручная"),19));
                content.addView(text(b.getString("id")+"\n"+String.format(java.util.Locale.getDefault(),"%.1f МБ",b.getLong("bytes")/1048576.0)+" · целостность SQLite проверена",14));}
            content.addView(text("Папка: data/backups/automatic. Копии хранятся на том же ноутбуке; для защиты от поломки диска нужна дополнительная копия на NAS или другом носителе. Восстановление выполняется на сервере при остановленном боте.",14));
        });
    }
    private void audience(){
        screen("Пользователи Mini App",true);button(content,"Обновить",()->audience());
        content.addView(text("Блокировка аккаунта закрывает доступ к боту и Mini App, останавливает автозапись и напоминания. Существующие записи на портале сохраняются.",16));
        request(()->api("/api/admin/audience","GET",null),r->{
            JSONArray users=r.getJSONArray("users"),allDevices=r.getJSONArray("devices");
            for(int i=0;i<users.length();i++){
                JSONObject u=users.getJSONObject(i);String uid=u.getString("uid");boolean banned=u.getBoolean("blocked"),owner=u.getBoolean("owner");
                String username=u.isNull("username")?u.getString("code"):"@"+u.getString("username").replace("@","");
                content.addView(text(username+(owner?" · владелец":"")+(banned?" · заблокирован":""),22));
                content.addView(text("Telegram ID: "+uid+"\nАктивность: "+stamp(u.opt("last_seen"))+"\nСпортивный аккаунт: "+(u.getBoolean("connected")?"подключён":"не подключён"),15));
                if(!owner)button(content,banned?"Разблокировать пользователя":"Заблокировать в боте",()->new AlertDialog.Builder(this).setTitle(banned?"Открыть доступ к боту?":"Заблокировать аккаунт в боте и Mini App?").setNegativeButton("Назад",null).setPositiveButton(banned?"Разблокировать":"Заблокировать",(d,w)->request(()->api("/api/admin/audience/users/"+enc(uid),"PUT",new JSONObject().put("blocked",!banned)),v->audience())).show());
                int count=0;
                for(int j=0;j<allDevices.length();j++){
                    JSONObject dev=allDevices.getJSONObject(j);if(!uid.equals(dev.getString("uid")))continue;count++;
                    String id=dev.getString("id");boolean deviceBanned=dev.getBoolean("blocked");
                    content.addView(text("Mini App · "+dev.getString("platform")+" · "+id.substring(0,8)+(deviceBanned?" · заблокировано":""),18));
                    content.addView(text("Первое открытие: "+stamp(dev.get("first_seen"))+"\nАктивность: "+stamp(dev.get("last_seen"))+"\n"+dev.getString("agent"),14));
                    if(!owner)button(content,deviceBanned?"Разблокировать устройство":"Закрыть Mini App на устройстве",()->new AlertDialog.Builder(this).setTitle(deviceBanned?"Разблокировать устройство?":"Заблокировать это устройство Mini App?").setMessage("Очистка данных или смена адреса сервера может изменить ID устройства. Для полного запрета заблокируй пользователя.").setNegativeButton("Назад",null).setPositiveButton(deviceBanned?"Разблокировать":"Заблокировать",(d,w)->request(()->api("/api/admin/audience/devices/"+enc(id),"PUT",new JSONObject().put("blocked",!deviceBanned)),v->audience())).show());
                }
                if(count==0)content.addView(text("Устройства ещё не зарегистрированы. Они появятся после открытия обновлённого Mini App.",14));
            }
            content.addView(text("Здесь показаны наблюдавшиеся установки Mini App, а не список активных устройств Telegram. Платформа и браузер сообщаются клиентом и могут быть изменены.",14));
        });
    }
    @Override protected void onDestroy(){token="";worker.shutdownNow();super.onDestroy();}
}
