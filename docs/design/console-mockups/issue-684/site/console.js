(() => {
'use strict';
const root=document.getElementById('harness-console-demo');
const scenario=document.getElementById('hc-scenario');
const screens=[...root.querySelectorAll('[data-hc-screen]')];
let view='overview',fixed=false;
const unknown=()=>scenario.value==='unknown';
function show(next,focus=true){
 view=next;
 screens.forEach(el=>{el.hidden=el.dataset.hcScreen!==next;});
 root.querySelectorAll('[data-hc-case]').forEach(el=>{el.hidden=el.dataset.hcCase!==scenario.value;});
 root.querySelectorAll('[data-hc-notice]').forEach(el=>el.textContent='');
 const labels={overview:'Оркестрация',diagnostic:'Восстановление',fix:'Выбор исправления',diff:'Просмотр изменений',ready:'Проверка продолжения',confirm:'Подтверждение',resumed:'Продолжение разрешено',exit:'Выход'};
 document.getElementById('hc-crumb').textContent='Главная › Оркестрация'+(next==='overview'?'':' › batch-demo-681 › '+labels[next]);
 document.getElementById('hc-problem-title').textContent=unknown()?'Неизвестен результат отправки задания':'Недопустимое значение в конфигурации';
 if(focus)document.getElementById('hc-'+next).focus({preventScroll:true});
}
function notice(text){document.getElementById('hc-'+view).querySelector('[data-hc-notice]').textContent=text;}
function action(a){
 if(a==='restart'){fixed=false;document.getElementById('hc-canonical').value='';show('overview');return;}
 if(a==='abort'&& !['overview','resumed'].includes(view)){show('exit');return;}
 if(['open','back','reopen'].includes(a)){show(fixed?'ready':'diagnostic');return;}
 if(a==='fix'&&!unknown()&&['diagnostic','diff'].includes(view)){show('fix');return;}
 if(a==='preview'&&view==='fix'){if(document.getElementById('hc-canonical').value==='manual_all')show('diff');else notice('Сначала явно выберите точное значение. Неизвестное слово не исправляется автоматически.');return;}
 if(a==='apply'&&view==='diff'&&!unknown()){fixed=true;show('ready');return;}
 if(a==='reload'&&['diagnostic','ready'].includes(view)&&!unknown()){notice(fixed?'Конфигурация проверена повторно. Worker не запускается.':'Ошибка manul_all остаётся. Перечитывание не меняет файл и никого не запускает.');return;}
 if(a==='probe'&&unknown()&&view==='diagnostic'){notice('Демо: adapter не дал достаточного доказательства. Исход остаётся unknown. Нужна ручная сверка; второй worker не запускается.');return;}
 if(a==='resume'&&view==='ready'&&fixed&&!unknown()){show('confirm');return;}
 if(a==='confirm'&&view==='confirm'&&fixed&&!unknown()){show('resumed');return;}
 if(a==='ready'&&view==='confirm')show('ready');
}
root.addEventListener('click',e=>{const button=e.target.closest('[data-hc-action]');if(button&&root.contains(button)&&!button.disabled)action(button.dataset.hcAction);});
root.addEventListener('keydown',e=>{if(e.altKey||e.ctrlKey||e.metaKey||e.target.closest('select,input,textarea,button'))return;const key=e.key.toLowerCase();if(key==='f'&&view==='diagnostic'&&!unknown()){e.preventDefault();action('fix');}else if(key==='r'&&['diagnostic','ready'].includes(view)&&!unknown()){e.preventDefault();action('reload');}else if(key==='a'&&!['overview','exit','resumed'].includes(view)){e.preventDefault();action('abort');}});
function reset(){fixed=false;document.getElementById('hc-canonical').value='';show('overview',false);}
scenario.addEventListener('change',reset);document.getElementById('hc-reset').addEventListener('click',reset);
show('overview',false);
})();
