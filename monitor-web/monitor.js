'use strict';
const $=id=>document.getElementById(id),node=(tag,text,cls)=>{const e=document.createElement(tag);e.textContent=text;if(cls)e.className=cls;return e;};
let paused=false,latest=null,loading=false;
const clock=value=>new Date(typeof value==='number'?value*1000:value).toLocaleTimeString('ru-RU',{timeZone:'Europe/Moscow',hour12:false});
function feeds(data){
 const incoming=$('incoming');incoming.replaceChildren();
 for(const item of data.requests){const row=node('div','','row'),who=node('span',clock(item.time));who.append(node('small',item.user));row.append(who,node('span',item.command),node('span',({processed:'Обработано',received:'Получено',error:'Ошибка',blocked:'Заблокировано'})[item.result]||item.result,item.result==='error'?'bad':item.result==='processed'?'good':'pending'));incoming.append(row);}
 if(!data.requests.length)incoming.append(node('div','Команд пока нет','empty'));
 const activity=$('activity');activity.replaceChildren();for(const item of data.actions){const row=node('div','','row activity');row.append(node('span',clock(item.time)),node('span',item.title),node('span',item.state,item.state==='ошибка'||item.state==='пропущено'?'bad':'good'));activity.append(row);}if(!data.actions.length)activity.append(node('div','Событий журнала пока нет','empty'));
}
function render(data){
 if(!data.available){$('notice').hidden=false;$('notice').textContent=data.error||'Ожидание показателей';return;}
 $('notice').hidden=data.journal_ok&&data.log_ok;if(!$('notice').hidden)$('notice').textContent='Часть журналов пока недоступна. Показатели могут быть неполными.';
 $('status').textContent=data.process.running?'● Бот работает':'○ Бот остановлен';$('status').className=data.process.running?'good':'bad';
 $('cpu').textContent=data.process.cpu===null?'—':data.process.cpu.toFixed(1)+'%';$('memory').textContent=data.process.memory_mb===null?'—':data.process.memory_mb+' МБ';
 $('rpm').textContent=data.counts.minute;$('pending').textContent='Получено за минуту без отметки завершения: '+data.counts.pending;
 let sec=data.process.uptime;$('uptime').textContent=sec===null?'—':Math.floor(sec/3600)+'ч '+Math.floor(sec%3600/60)+'м';$('threads').textContent='Потоков: '+(data.process.threads??'—');
 $('host').textContent='Весь ПК: CPU '+data.host_cpu.toFixed(0)+'% · RAM '+data.host_ram.toFixed(0)+'%';
 $('users').textContent='Активных сегодня: '+data.counts.users_today;$('latency').textContent='Среднее последних обработок: '+(data.latency_ms===null?'—':data.latency_ms+' мс');$('errors').textContent='Ошибок обработки сегодня: '+data.counts.errors_today;
 const points=data.history.filter(p=>p.cpu!==null),max=Math.max(100,...points.map(p=>p.cpu));const path=points.map((p,i)=>(i?'L':'M')+(i*1000/149).toFixed(1)+','+(150-p.cpu/max*140).toFixed(1)).join(' ');$('line').setAttribute('d',path);$('area').setAttribute('d',points.length?path+' L'+((points.length-1)*1000/149).toFixed(1)+',160 L0,160 Z':'');
 $('jobs').replaceChildren();for(const j of data.jobs){const row=node('div','','job'),name=node('div',j.title);name.append(node('small',clock(j.time)));row.append(name,node('span',j.state));$('jobs').append(row);}if(!data.jobs.length)$('jobs').append(node('div','Ожидаем запуск задач','empty'));
 if(!paused)feeds(data);$('updated').textContent='Данные: '+clock(data.updated)+(paused?' · ленты на паузе':'');
}
async function refresh(){if(loading)return;loading=true;try{const r=await fetch('/api/stats');if(!r.ok)throw Error();latest=await r.json();render(latest);}catch{$('notice').hidden=false;$('notice').textContent='Нет связи с локальной панелью. Повторяем подключение…';$('status').textContent='○ Нет связи';}finally{loading=false;}}
$('pause').onclick=()=>{paused=!paused;$('pause').textContent=paused?'Продолжить ленту':'Пауза ленты';if(latest&&!paused)render(latest);};
$('fullscreen').onclick=async()=>{try{if(document.fullscreenElement)await document.exitFullscreen();else await document.documentElement.requestFullscreen();}catch{}};
setInterval(()=>{$('clock').textContent=clock(Date.now()/1000);},1000);refresh();setInterval(refresh,2000);
